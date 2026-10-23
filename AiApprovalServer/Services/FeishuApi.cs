using System.Net;
using System.Text;
using System.Text.Json;
using AiApproval.Core;

namespace AiApproval.Services;

//接口错误（不含凭据）
public sealed class FeishuApiException : Exception
{
    public int Code { get; }
    public string Stage { get; }

    /// <summary>
    /// true = 请求可能已经在飞书侧生效（超时 / 连接中断 / 响应读到一半断开）。
    /// 写操作遇到这种情况**绝不能重试**，只能靠调用方回查确认，否则会重复写单。
    /// </summary>
    public bool UnknownState { get; }

    public FeishuApiException(string stage, int code, string msg, Exception? inner = null, bool unknownState = false)
        : base($"{stage}失败 code={code} msg={msg}", inner)
    {
        Stage = stage;
        Code = code;
        UnknownState = unknownState;
    }
}

//多维表格最小客户端
public sealed class FeishuApi
{
    private const string TokenPath = "/open-apis/auth/v3/tenant_access_token/internal";

    private readonly AppConfig _cfg;
    private readonly ILogger<FeishuApi> _logger;
    private readonly HttpClient _http;
    private readonly SemaphoreSlim _tokenLock = new(1, 1);
    private readonly Dictionary<string, (HashSet<string> Names, DateTime At)> _fieldCache = new(StringComparer.Ordinal);

    private string _token = "";
    private DateTime _tokenExpiresAt = DateTime.MinValue;

    public FeishuApi(AppConfig cfg, ILogger<FeishuApi> logger)
    {
        _cfg = cfg;
        _logger = logger;
        _http = new HttpClient(new SocketsHttpHandler
        {
            PooledConnectionLifetime = TimeSpan.FromMinutes(2),
            //建连保持短超时（连不上就是网络问题，不必等满 30 秒）；读写用完整的 TimeoutMs
            ConnectTimeout = TimeSpan.FromMilliseconds(Math.Clamp(cfg.Feishu.TimeoutMs, 500, 5000)),
        })
        {
            Timeout = TimeSpan.FromMilliseconds(Math.Max(500, cfg.Feishu.TimeoutMs)),
        };
    }

    //凭据是否齐备
    public bool Enabled => _cfg.Feishu.Configured;

    //换取访问令牌（带缓存）
    public async Task<string> TokenAsync(CancellationToken ct)
    {
        if (!string.IsNullOrEmpty(_token) && DateTime.UtcNow < _tokenExpiresAt) return _token;

        await _tokenLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (!string.IsNullOrEmpty(_token) && DateTime.UtcNow < _tokenExpiresAt) return _token;

            var json = await JsonAsync(() => new HttpRequestMessage(HttpMethod.Post, _cfg.Feishu.BaseUrl + TokenPath)
            {
                Content = JsonBody(new { app_id = _cfg.Feishu.AppId, app_secret = _cfg.Feishu.AppSecret }),
            }, "换取 tenant_access_token", "", ct).ConfigureAwait(false);

            _token = json.TryGetProperty("tenant_access_token", out var t) ? t.GetString() ?? "" : "";
            var expire = json.TryGetProperty("expire", out var e) ? e.GetInt32() : 7200;
            _tokenExpiresAt = DateTime.UtcNow.AddSeconds(Math.Max(30, expire - 60));
            if (string.IsNullOrEmpty(_token)) throw new FeishuApiException("换取 tenant_access_token", -1, "响应中没有 tenant_access_token");
            return _token;
        }
        finally
        {
            _tokenLock.Release();
        }
    }

    //清空票缓存
    public void InvalidateToken()
    {
        _token = "";
        _tokenExpiresAt = DateTime.MinValue;
    }

    //列出记录（自动翻页）
    public async Task<List<JsonElement>> ListRecordsAsync(string tableId, int pageSize, CancellationToken ct)
    {
        var token = await TokenAsync(ct).ConfigureAwait(false);
        var size = Math.Clamp(pageSize, 1, 500);
        var items = new List<JsonElement>();
        var pageToken = "";

        //单次最多 500 行，靠 page_token 翻到底，最多 20 页
        for (var page = 0; page < 20; page++)
        {
            var url = $"{_cfg.Feishu.BaseUrl}/open-apis/bitable/v1/apps/{_cfg.Feishu.AppToken}" +
                      $"/tables/{tableId}/records?page_size={size}" +
                      (pageToken.Length > 0 ? $"&page_token={Uri.EscapeDataString(pageToken)}" : "");
            var json = await JsonAsync(() => Authorized(HttpMethod.Get, url, token), "列出记录", token, ct).ConfigureAwait(false);

            if (!json.TryGetProperty("data", out var data)) break;
            if (data.TryGetProperty("items", out var arr))
                foreach (var item in arr.EnumerateArray()) items.Add(item.Clone());

            var hasMore = data.TryGetProperty("has_more", out var more) && more.ValueKind == JsonValueKind.True;
            pageToken = data.TryGetProperty("page_token", out var next) ? (next.GetString() ?? "") : "";
            if (!hasMore || pageToken.Length == 0) break;
        }

        return items;
    }

    //新增记录
    public async Task<string> CreateRecordAsync(string tableId, IDictionary<string, object?> fields, CancellationToken ct)
    {
        var token = await TokenAsync(ct).ConfigureAwait(false);
        var url = $"{_cfg.Feishu.BaseUrl}/open-apis/bitable/v1/apps/{_cfg.Feishu.AppToken}" +
                  $"/tables/{tableId}/records";
        var json = await JsonAsync(() => Authorized(HttpMethod.Post, url, token, new { fields }), "新增记录", token, ct, mutating: true).ConfigureAwait(false);

        if (json.TryGetProperty("data", out var data) && data.TryGetProperty("record", out var rec)
            && rec.TryGetProperty("record_id", out var rid))
            return rid.GetString() ?? "";
        return "";
    }

    //读取单条记录
    public async Task<string> GetRecordAsync(string tableId, string recordId, CancellationToken ct)
    {
        var token = await TokenAsync(ct).ConfigureAwait(false);
        var url = $"{_cfg.Feishu.BaseUrl}/open-apis/bitable/v1/apps/{_cfg.Feishu.AppToken}" +
                  $"/tables/{tableId}/records/{recordId}";
        var json = await JsonAsync(() => Authorized(HttpMethod.Get, url, token), "读取单条记录", token, ct).ConfigureAwait(false);
        return json.GetRawText();
    }

    //更新记录
    public async Task<bool> UpdateRecordAsync(string tableId, string recordId, IDictionary<string, object?> fields, CancellationToken ct)
    {
        var token = await TokenAsync(ct).ConfigureAwait(false);
        var url = $"{_cfg.Feishu.BaseUrl}/open-apis/bitable/v1/apps/{_cfg.Feishu.AppToken}" +
                  $"/tables/{tableId}/records/{recordId}";
        await JsonAsync(() => Authorized(HttpMethod.Put, url, token, new { fields }), "更新记录", token, ct, mutating: true).ConfigureAwait(false);
        return true;
    }

    //删除记录
    public async Task<bool> DeleteRecordAsync(string tableId, string recordId, CancellationToken ct)
    {
        var token = await TokenAsync(ct).ConfigureAwait(false);
        var url = $"{_cfg.Feishu.BaseUrl}/open-apis/bitable/v1/apps/{_cfg.Feishu.AppToken}" +
                  $"/tables/{tableId}/records/{recordId}";
        await JsonAsync(() => Authorized(HttpMethod.Delete, url, token), "删除记录", token, ct, mutating: true).ConfigureAwait(false);
        return true;
    }

    //表字段名集合（带缓存）
    public async Task<HashSet<string>> FieldNamesAsync(string tableId, CancellationToken ct)
    {
        lock (_fieldCache)
        {
            if (_fieldCache.TryGetValue(tableId, out var cached) && DateTime.UtcNow - cached.At < TimeSpan.FromMinutes(10))
                return cached.Names;
        }

        var token = await TokenAsync(ct).ConfigureAwait(false);
        var url = $"{_cfg.Feishu.BaseUrl}/open-apis/bitable/v1/apps/{_cfg.Feishu.AppToken}" +
                  $"/tables/{tableId}/fields?page_size=100";
        var json = await JsonAsync(() => Authorized(HttpMethod.Get, url, token), "列出字段", token, ct).ConfigureAwait(false);

        var names = new HashSet<string>(StringComparer.Ordinal);
        if (json.TryGetProperty("data", out var data) && data.TryGetProperty("items", out var arr))
            foreach (var f in arr.EnumerateArray())
                if (f.TryGetProperty("field_name", out var n) && n.GetString() is { Length: > 0 } name)
                    names.Add(name);

        lock (_fieldCache) { _fieldCache[tableId] = (names, DateTime.UtcNow); }
        return names;
    }

    //带鉴权的请求
    private static HttpRequestMessage Authorized(HttpMethod method, string url, string token, object? body = null)
    {
        var req = new HttpRequestMessage(method, url);
        if (body is not null) req.Content = JsonBody(body);
        req.Headers.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return req;
    }

    //JSON 请求体
    private static StringContent JsonBody(object body)
        => new(AppJson.Serialize(body), Encoding.UTF8, "application/json");

    //发送并校验响应（重试时重建请求对象）
    //mutating=true 表示写操作（新增/修改/删除）：这类请求不幂等，
    //一旦出现超时或连接中断就「可能已经生效」，此时重试会直接造成重复写单，因此一律不重试。
    private async Task<JsonElement> JsonAsync(Func<HttpRequestMessage> factory, string stage, string token,
        CancellationToken ct, bool mutating = false)
    {
        using (var probe = factory())
        {
            var host = probe.RequestUri?.Host ?? "";
            if (!HostAllowed(host)) throw new FeishuApiException(stage, -1, $"出站主机不在白名单内：{host}");
        }

        var attempts = Math.Max(0, _cfg.Feishu.MaxRetries) + 1;
        HttpResponseMessage? resp = null;
        Exception? last = null;
        for (var i = 0; i < attempts; i++)
        {
            using var req = factory();
            try
            {
                resp = await _http.SendAsync(req, HttpCompletionOption.ResponseContentRead, ct).ConfigureAwait(false);
                break;
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or IOException)
            {
                last = ex;

                //写操作：结果未知，立即放弃并标记，交给上层回查对账（绝不重试）
                if (mutating)
                    throw new FeishuApiException(stage, -1, Describe(ex), ex, unknownState: true);

                if (i < attempts - 1) await Task.Delay(200, ct).ConfigureAwait(false);
            }
        }

        if (resp is null)
            throw new FeishuApiException(stage, -1, Describe(last), last);

        using (resp)
        {
            var text = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (string.IsNullOrWhiteSpace(text))
                throw new FeishuApiException(stage, (int)resp.StatusCode, "飞书返回空响应");

            JsonElement json;
            try
            {
                using var doc = JsonDocument.Parse(text);
                json = doc.RootElement.Clone();
            }
            catch (JsonException ex)
            {
                throw new FeishuApiException(stage, (int)resp.StatusCode, $"响应不是合法 JSON：{text[..Math.Min(160, text.Length)]}", ex);
            }

            var code = json.TryGetProperty("code", out var c) && c.ValueKind == JsonValueKind.Number ? c.GetInt32() : -1;
            if (code != 0)
            {
                var msg = json.TryGetProperty("msg", out var m) ? m.GetString() ?? "" : "";
                //token 失效则清缓存
                if (code is 99991663 or 99991664 or 99991661) InvalidateToken();
                throw new FeishuApiException(stage, code, msg);
            }
            return json;
        }
    }

    //出站主机白名单
    private bool HostAllowed(string host)
    {
        var list = (_cfg.Feishu.AllowedHosts ?? "")
            .Split(new[] { ',', ';', ' ' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (list.Length == 0) list = new[] { "open.feishu.cn" };
        if (list.Any(h => string.Equals(h, host, StringComparison.OrdinalIgnoreCase))) return true;

        //本地联调放行回环
        return host is "127.0.0.1" or "localhost" or "::1";
    }

    //异常链压成一行
    private static string Describe(Exception? ex)
    {
        if (ex is null) return "请求失败";
        var sb = new StringBuilder();
        for (var e = ex; e is not null; e = e.InnerException)
        {
            if (sb.Length > 0) sb.Append(" <- ");
            sb.Append(e.GetType().Name).Append('(').Append(e.Message).Append(')');
        }
        return sb.ToString();
    }
}
