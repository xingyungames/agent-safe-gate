using System.Diagnostics;
using System.Net;
using System.Text;
using AiApproval.Core;

namespace AiApproval.Ai;

public sealed class AiCallResult
{
    public bool Ok { get; init; }
    public string Content { get; init; } = "";
    public string Error { get; init; } = "";
    public long LatencyMs { get; init; }
    public int PromptTokens { get; init; }
    public int CompletionTokens { get; init; }
}

/// <summary>
/// 异步 AI API 客户端（对照 StarGame 的 AIHelper / AITool 思路重写）：
///   * 全程 async/await + CancellationToken，不阻塞请求线程；
///   * 失败隔离：任何异常都转成"AI 不可用"这一确定结论，绝不让"审核服务挂了"变成"操作被放行"；
///   * 经济型 DoS 防护：全局并发信号量 + 每用户每日配额（在 UserService 里扣减）；
///   * SSRF 防护：只允许 https、只允许配置里写死的 API 主机、拒绝私网/回环/元数据地址；
///   * 密钥只出现在请求头里，任何日志/响应/审计都不落密钥。
/// </summary>
public sealed class AiClient
{
    private static readonly string[] DefaultAllowedHosts = { "api.deepseek.com" };

    private readonly AppConfig _cfg;
    private readonly HttpClient _http;
    private readonly SemaphoreSlim _concurrency;
    private readonly ILogger<AiClient> _logger;

    public AiClient(AppConfig cfg, ILogger<AiClient> logger)
    {
        _cfg = cfg;
        _logger = logger;
        _concurrency = new SemaphoreSlim(Math.Max(1, cfg.Ai.MaxConcurrentCalls), Math.Max(1, cfg.Ai.MaxConcurrentCalls));
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(Math.Clamp(cfg.Ai.TimeoutSeconds + 5, 5, 120)) };
        _http.DefaultRequestHeaders.Add("User-Agent", "AiApprovalServer/1.0");
    }

    public bool IsConfigured => _cfg.AiConfigured;

    public string Model => _cfg.Ai.Model;

    /// <summary>最近一次成功调用时间（用于对外如实展示"AI 到底通不通"）</summary>
    public DateTime? LastSuccessAt { get; private set; }

    /// <summary>最近一次失败时间与原因（脱敏，只含异常类型与简短描述）</summary>
    public DateTime? LastFailureAt { get; private set; }
    public string LastError { get; private set; } = "";

    /// <summary>
    /// 对外状态：off（未配置）/ degraded（配置了但最近调用失败）/ online（最近调用成功或尚未调用）。
    /// 注意：仅"配置了密钥"不等于"能用"，所以这里区分开，避免界面给出误导性的"AI 在线"。
    /// </summary>
    public string Status
    {
        get
        {
            if (!IsConfigured) return "off";
            if (LastFailureAt is { } failed && (LastSuccessAt is null || failed > LastSuccessAt)) return "degraded";
            return "online";
        }
    }

    /// <summary>
    /// 出站主机白名单：默认只允许官方的 api.deepseek.com。
    /// 要接自建/私有模型网关（vLLM / Ollama / 内部网关）必须在 Ai:AllowedHosts 中**显式**放行——
    /// 这样既支持私有部署，也不会因为"配置里能改个 URL"就把服务变成 SSRF 跳板。
    /// </summary>
    private HashSet<string> AllowedHosts()
    {
        var configured = (_cfg.Ai.AllowedHosts ?? "")
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(h => h.ToLowerInvariant())
            .ToHashSet(StringComparer.OrdinalIgnoreCase);

        return configured.Count > 0 ? configured : DefaultAllowedHosts.ToHashSet(StringComparer.OrdinalIgnoreCase);
    }

    public bool IsUrlAllowed(out string reason)
    {
        reason = "";
        if (!Uri.TryCreate(_cfg.Ai.BaseUrl, UriKind.Absolute, out var uri))
        {
            reason = "AI 接口地址非法";
            return false;
        }

        if (!AllowedHosts().Contains(uri.Host))
        {
            reason = $"AI 接口主机不在白名单：{uri.Host}（自建网关请在 Ai:AllowedHosts 中显式放行）";
            return false;
        }

        if (string.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase)) return true;

        // 明文 http 只放给"已显式白名单 + 本机/内网"的自建网关
        if (string.Equals(uri.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase)
            && (uri.IsLoopback
                || string.Equals(uri.Host, "localhost", StringComparison.OrdinalIgnoreCase)
                || (IPAddress.TryParse(uri.Host, out var httpIp) && IsPrivate(httpIp))))
            return true;

        reason = "AI 接口必须使用 https（仅本机/内网自建网关可用明文 http）";
        return false;
    }

    /// <summary>
    /// 单轮对话（不做会话记忆；记忆由调用方维护，便于隔离不同用户/不同视角的上下文）。
    /// </summary>
    public async Task<AiCallResult> CompleteAsync(string systemPrompt, string userContent,
        double temperature = 0.2, int? maxTokens = null, bool jsonMode = true, CancellationToken ct = default)
    {
        if (!IsConfigured)
            return new AiCallResult { Ok = false, Error = "AI 未配置（缺少 ApiKey）" };

        if (!IsUrlAllowed(out var urlReason))
            return new AiCallResult { Ok = false, Error = urlReason };

        var payload = new Dictionary<string, object?>
        {
            ["model"] = _cfg.Ai.Model,
            ["messages"] = new object[]
            {
                new { role = "system", content = systemPrompt },
                new { role = "user", content = userContent },
            },
            ["temperature"] = temperature,
            ["stream"] = false,
            ["max_tokens"] = maxTokens ?? _cfg.Ai.MaxOutputTokens,
        };

        if (jsonMode)
            payload["response_format"] = new { type = "json_object" };

        var json = AppJson.Serialize(payload);
        var attempts = Math.Clamp(_cfg.Ai.MaxRetries, 0, 3) + 1;
        var stopwatch = Stopwatch.StartNew();
        var lastError = "";

        await _concurrency.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            for (var attempt = 1; attempt <= attempts; attempt++)
            {
                using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                timeoutCts.CancelAfter(TimeSpan.FromSeconds(Math.Clamp(_cfg.Ai.TimeoutSeconds, 5, 120)));

                try
                {
                    using var request = new HttpRequestMessage(HttpMethod.Post, _cfg.Ai.BaseUrl);
                    request.Content = new StringContent(json, Encoding.UTF8, "application/json");
                    request.Headers.TryAddWithoutValidation("Authorization", "Bearer " + _cfg.Ai.ApiKey);

                    using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseContentRead, timeoutCts.Token)
                        .ConfigureAwait(false);

                    var text = await response.Content.ReadAsStringAsync(timeoutCts.Token).ConfigureAwait(false);

                    if (!response.IsSuccessStatusCode)
                    {
                        // 只保留状态码，绝不把上游返回体（可能含密钥/内部信息）抛给调用方
                        lastError = $"AI 接口返回 {(int)response.StatusCode}";
                        if ((int)response.StatusCode is >= 400 and < 500 && (int)response.StatusCode != 429)
                            break;
                        continue;
                    }

                    var (content, promptTokens, completionTokens) = ParseResponse(text);
                    if (content is null)
                    {
                        lastError = "AI 返回结构无法解析";
                        continue;
                    }

                    LastSuccessAt = DateTime.UtcNow;
                    LastError = "";

                    return new AiCallResult
                    {
                        Ok = true,
                        Content = content,
                        LatencyMs = stopwatch.ElapsedMilliseconds,
                        PromptTokens = promptTokens,
                        CompletionTokens = completionTokens,
                    };                }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                {
                    lastError = $"AI 调用超时（{_cfg.Ai.TimeoutSeconds}s）";
                }
                catch (Exception ex)
                {
                    // 记录足够定位问题、但不含密钥的信息（异常文本里不会带 Authorization 头）
                    lastError = "AI 调用异常：" + Describe(ex);
                }

                if (attempt < attempts)
                    await Task.Delay(TimeSpan.FromMilliseconds(200 * attempt + Random.Shared.Next(0, 150)), ct).ConfigureAwait(false);
            }
        }
        finally
        {
            _concurrency.Release();
        }

        _logger.LogWarning("AI 调用失败：{Error}", lastError);
        LastFailureAt = DateTime.UtcNow;
        LastError = lastError;
        return new AiCallResult { Ok = false, Error = lastError, LatencyMs = stopwatch.ElapsedMilliseconds };
    }

    /// <summary>把异常链压成一行短描述（排障够用，且不含任何密钥/请求头内容）。</summary>
    private static string Describe(Exception ex)
    {
        var parts = new List<string>();
        var current = ex;
        var depth = 0;
        while (current is not null && depth < 3)
        {
            parts.Add($"{current.GetType().Name}({Trim(current.Message)})");
            current = current.InnerException;
            depth++;
        }
        return string.Join(" <- ", parts);

        static string Trim(string message)
            => string.IsNullOrEmpty(message) ? "" : message.Length <= 120 ? message : message[..120];
    }

    private static (string? Content, int PromptTokens, int CompletionTokens) ParseResponse(string raw)    {
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(raw);
            var root = doc.RootElement;

            var content = root.TryGetProperty("choices", out var choices) && choices.GetArrayLength() > 0
                ? choices[0].TryGetProperty("message", out var message) && message.TryGetProperty("content", out var c)
                    ? c.GetString()
                    : null
                : null;

            var promptTokens = 0;
            var completionTokens = 0;
            if (root.TryGetProperty("usage", out var usage))
            {
                if (usage.TryGetProperty("prompt_tokens", out var p)) promptTokens = p.GetInt32();
                if (usage.TryGetProperty("completion_tokens", out var q)) completionTokens = q.GetInt32();
            }

            return (content, promptTokens, completionTokens);
        }
        catch
        {
            return (null, 0, 0);
        }
    }

    private static bool IsPrivate(IPAddress ip)
    {
        if (IPAddress.IsLoopback(ip)) return true;
        if (ip.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork)
        {
            var b = ip.GetAddressBytes();
            return b[0] == 10
                || (b[0] == 172 && b[1] >= 16 && b[1] <= 31)
                || (b[0] == 192 && b[1] == 168)
                || (b[0] == 169 && b[1] == 254)
                || b[0] == 127
                || b[0] == 0
                || (b[0] == 100 && b[1] >= 64 && b[1] <= 127)
                || b[0] >= 224;
        }
        if (ip.AddressFamily == System.Net.Sockets.AddressFamily.InterNetworkV6)
        {
            if (ip.IsIPv6LinkLocal || ip.IsIPv6SiteLocal || ip.IsIPv6Multicast) return true;
            var b = ip.GetAddressBytes();
            if (b.All(x => x == 0)) return true;
            if ((b[0] & 0xFE) == 0xFC) return true;
            if (b[0] == 0xFE && (b[1] & 0xC0) == 0x80) return true;
        }
        return false;
    }
}
