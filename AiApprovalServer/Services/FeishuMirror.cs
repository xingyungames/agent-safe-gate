using System.Security.Cryptography;
using System.Text.Json;
using AiApproval.Core;
using AiApproval.Security;

namespace AiApproval.Services;

//镜像结果
public sealed record MirrorResult(bool Ok, bool Skipped, string Error = "", string RecordId = "")
{
    public static MirrorResult Skip(string reason) => new(true, true, reason);
    public static MirrorResult Success(string recordId) => new(true, false, "", recordId);
    public static MirrorResult Fail(string error) => new(false, false, error);
}

//用户表一行（外部账号检测用）
public sealed record MirrorRow(string RecordId, string UserName, string EmployeeId, string RealName, string RoleLabel);

//用户表一行全量字段（账号导入用）
public sealed record MirrorAccountRow(
    string RecordId,
    string EmployeeId,
    string DisplayName,
    string UserName,
    string RoleLabel,
    string StatusLabel,
    string HashHex,
    string SaltHex,
    DateTime? CreatedAt,
    DateTime? LastLoginAt)
{
    //外部已停用
    public bool Disabled => StatusLabel.Contains("停用", StringComparison.Ordinal);

    //有可用口令材料
    public bool HasPassword => HashHex.Length >= 64 && SaltHex.Length >= 32;
}

//用户表镜像
public sealed class FeishuMirror
{
    private readonly AppConfig _cfg;
    private readonly AuditLog _audit;
    private readonly ILogger<FeishuMirror> _logger;
    private readonly FeishuApi _api;

    public FeishuMirror(AppConfig cfg, AuditLog audit, FeishuApi api, ILogger<FeishuMirror> logger)
    {
        _cfg = cfg;
        _audit = audit;
        _api = api;
        _logger = logger;
    }

    public bool Enabled => _cfg.FeishuConfigured;

    //写镜像行（不存在则新建）
    public async Task<MirrorResult> SyncAsync(UserRecord user, string? realPassword, CancellationToken ct = default)
    {
        if (!Enabled) return MirrorResult.Skip("飞书未配置");

        //镜像是 best-effort：用独立短超时，绝不能因为飞书慢而拖住注册/登录（报销单读写才用 30 秒）
        using var mirrorCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        mirrorCts.CancelAfter(TimeSpan.FromMilliseconds(Math.Max(500, _cfg.Feishu.MirrorTimeoutMs)));
        ct = mirrorCts.Token;

        var prepared = PrepareCredentials(user, realPassword);
        try
        {
            var fields = BuildFields(user, prepared);

            var result = string.IsNullOrEmpty(user.MirrorRecordId)
                ? await CreateAsync(fields, ct).ConfigureAwait(false)
                : await UpdateAsync(user.MirrorRecordId, fields, ct).ConfigureAwait(false);

            if (result.Ok)
            {
                user.MirrorState = "synced";
                user.MirrorError = "";
                user.MirrorAt = DateTime.UtcNow;
                user.MirrorRecordId = result.RecordId;
                if (prepared is not null)
                {
                    user.MirrorHashHex = prepared.HashHex;
                    user.MirrorSaltHex = prepared.SaltHex;
                    if (prepared.DemoPasswordCipher is not null)
                        user.MirrorPasswordCipher = prepared.DemoPasswordCipher;
                }
                await _audit.WriteAsync(user.Id, user.UserName, user.Role, "", "feishu.mirror.sync", "用户表",
                    "SUCCESS", $"已镜像账号 {user.UserName}（{Roles.Label(user.Role)}）record={result.RecordId}").ConfigureAwait(false);
                return result;
            }

            //更新失败则重建一行
            if (!string.IsNullOrEmpty(user.MirrorRecordId))
            {
                var retry = await CreateAsync(fields, ct).ConfigureAwait(false);
                if (retry.Ok)
                {
                    user.MirrorState = "synced";
                    user.MirrorError = "";
                    user.MirrorAt = DateTime.UtcNow;
                    user.MirrorRecordId = retry.RecordId;
                    return retry;
                }
                result = retry;
            }

            MarkFailure(user, result.Error);
            return result;
        }
        catch (Exception ex)
        {
            //失败静默，只写审计
            MarkFailure(user, Describe(ex));
            return MirrorResult.Fail(user.MirrorError);
        }
    }

    //新建镜像行
    private async Task<MirrorResult> CreateAsync(Dictionary<string, object?> fields, CancellationToken ct)
    {
        var recordId = await _api.CreateRecordAsync(_cfg.Feishu.UserTableId, fields, ct).ConfigureAwait(false);
        return string.IsNullOrEmpty(recordId)
            ? MirrorResult.Fail("写入成功但未返回 record_id")
            : MirrorResult.Success(recordId);
    }

    //更新镜像行
    private async Task<MirrorResult> UpdateAsync(string recordId, Dictionary<string, object?> fields, CancellationToken ct)
    {
        await _api.UpdateRecordAsync(_cfg.Feishu.UserTableId, recordId, fields, ct).ConfigureAwait(false);
        return MirrorResult.Success(recordId);
    }

    //标记镜像失败
    private void MarkFailure(UserRecord user, string msg)
    {
        user.MirrorState = "failed";
        user.MirrorError = msg.Length > 300 ? msg[..300] : msg;
        user.MirrorAt = DateTime.UtcNow;
        _logger.LogWarning("飞书镜像失败（不影响业务）：{User} {Error}", user.UserName, user.MirrorError);
    }

    //失败写审计
    public async Task AuditFailureAsync(UserRecord user, string ip = "")
    {
        if (user.MirrorState != "failed") return;
        await _audit.WriteAsync(user.Id, user.UserName, user.Role, ip, "feishu.mirror.failed", "用户表",
            "FAILED", $"账号 {user.UserName} 镜像失败（已静默处理）：{user.MirrorError}").ConfigureAwait(false);
    }

    //拉取用户表全部账号
    public async Task<List<MirrorRow>> ListUsersAsync(CancellationToken ct = default)
    {
        var rows = new List<MirrorRow>();
        if (!Enabled) return rows;
        try
        {
            var items = await _api.ListRecordsAsync(_cfg.Feishu.UserTableId, 200, ct).ConfigureAwait(false);
            foreach (var item in items)
            {
                var fields = item.TryGetProperty("fields", out var f) ? f : default;
                rows.Add(new MirrorRow(
                    item.TryGetProperty("record_id", out var rid) ? rid.GetString() ?? "" : "",
                    FieldText(fields, "用户名"),
                    FieldText(fields, "工号"),
                    FieldText(fields, "姓名"),
                    FieldText(fields, "角色")));
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning("读取飞书用户表失败（已忽略）：{Error}", Describe(ex));
        }
        return rows;
    }

    //拉取全量字段（含口令材料）
    public async Task<List<MirrorAccountRow>> ListAccountRowsAsync(CancellationToken ct = default)
    {
        var rows = new List<MirrorAccountRow>();
        if (!Enabled) return rows;
        try
        {
            var items = await _api.ListRecordsAsync(_cfg.Feishu.UserTableId, 200, ct).ConfigureAwait(false);
            foreach (var item in items)
            {
                var fields = item.TryGetProperty("fields", out var f) ? f : default;
                rows.Add(new MirrorAccountRow(
                    item.TryGetProperty("record_id", out var rid) ? rid.GetString() ?? "" : "",
                    FieldText(fields, "工号").Trim(),
                    FieldText(fields, "姓名").Trim(),
                    FieldText(fields, "用户名").Trim(),
                    FieldText(fields, "角色").Trim(),
                    FieldText(fields, "账号状态").Trim(),
                    FieldText(fields, "密码哈希").Trim().ToLowerInvariant(),
                    FieldText(fields, "盐").Trim().ToLowerInvariant(),
                    FieldTime(fields, "创建时间"),
                    FieldTime(fields, "最后登录时间")));
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning("读取飞书用户表全量字段失败（已忽略）：{Error}", Describe(ex));
        }
        return rows;
    }

    //准备好的口令材料
    private sealed record Prepared(string HashHex, string SaltHex, string? DemoPasswordCipher);

    //按模式准备口令材料
    private Prepared? PrepareCredentials(UserRecord user, string? realPassword)
    {
        var mode = _cfg.Feishu.MirrorPasswordMode;
        if (mode == "none") return new Prepared("", "", null);

        if (mode == "real" && !string.IsNullOrEmpty(realPassword))
        {
            var (saltHex, hashHex) = HashForFeishu(realPassword);
            return new Prepared(hashHex, saltHex, null);
        }

        //演示口令哈希
        if (!string.IsNullOrEmpty(user.MirrorHashHex) && !string.IsNullOrEmpty(user.MirrorSaltHex))
            return new Prepared(user.MirrorHashHex, user.MirrorSaltHex, null);

        var demo = GenerateDemoPassword(_cfg.Feishu.DemoPasswordLength);
        var (dSalt, dHash) = HashForFeishu(demo);
        var cipher = Crypto.AesGcmEncrypt(demo, DemoCipherKey(), "mirror-demo-password:" + user.Id);
        return new Prepared(dHash, dSalt, cipher);
    }

    //生成口令哈希
    private (string SaltHex, string HashHex) HashForFeishu(string password)
    {
        var salt = RandomNumberGenerator.GetBytes(16);
        var hash = Rfc2898DeriveBytes.Pbkdf2(password, salt,
            Math.Max(1, _cfg.Feishu.MirrorPbkdf2Iterations), HashAlgorithmName.SHA256, 32);
        return (Convert.ToHexString(salt).ToLowerInvariant(), Convert.ToHexString(hash).ToLowerInvariant());
    }

    //生成演示口令
    private static string GenerateDemoPassword(int length)
    {
        //去掉易混淆字符（0/O/1/l/I）
        const string alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
        var chars = new char[Math.Clamp(length, 8, 32)];
        for (var i = 0; i < chars.Length; i++)
            chars[i] = alphabet[RandomNumberGenerator.GetInt32(alphabet.Length)];
        return new string(chars);
    }

    //组装字段
    private Dictionary<string, object?> BuildFields(UserRecord user, Prepared? prepared)
    {
        var fields = new Dictionary<string, object?>
        {
            ["工号"] = user.EmployeeId,
            ["姓名"] = user.DisplayName,
            ["用户名"] = user.UserName,
            ["角色"] = Roles.ToFeishuLabel(user.Role),
            ["账号状态"] = user.Disabled ? "停用" : "启用",
            ["创建时间"] = ToFeishuMs(user.CreatedAt),
        };
        if (user.LastLoginAt.HasValue) fields["最后登录时间"] = ToFeishuMs(user.LastLoginAt.Value);

        if (prepared is not null)
        {
            fields["密码哈希"] = prepared.HashHex;
            fields["盐"] = prepared.SaltHex;
        }
        return fields;
    }

    private static long ToFeishuMs(DateTime utc) =>
        new DateTimeOffset(DateTime.SpecifyKind(utc, DateTimeKind.Utc)).ToUnixTimeMilliseconds();

    //取字段文本
    private static string FieldText(JsonElement fields, string name)
    {
        if (fields.ValueKind != JsonValueKind.Object || !fields.TryGetProperty(name, out var v)) return "";
        return v.ValueKind switch
        {
            JsonValueKind.String => v.GetString() ?? "",
            JsonValueKind.Number => v.ToString(),
            _ => "",
        };
    }

    //读取日期列（毫秒或 ISO 文本）
    private static DateTime? FieldTime(JsonElement fields, string name)
    {
        if (fields.ValueKind != JsonValueKind.Object || !fields.TryGetProperty(name, out var v)) return null;
        if (v.ValueKind == JsonValueKind.Number && v.TryGetInt64(out var ms) && ms > 0)
        {
            try { return DateTimeOffset.FromUnixTimeMilliseconds(ms).UtcDateTime; }
            catch { return null; }
        }
        if (v.ValueKind == JsonValueKind.String && DateTime.TryParse(v.GetString(), out var parsed))
            return parsed.ToUniversalTime();
        return null;
    }

    //把异常链压成一行
    private static string Describe(Exception ex)
    {
        var sb = new System.Text.StringBuilder();
        for (var e = ex; e is not null; e = e.InnerException)
        {
            if (sb.Length > 0) sb.Append(" <- ");
            sb.Append(e.GetType().Name).Append('(').Append(e.Message).Append(')');
        }
        return sb.ToString();
    }

    //解密演示口令
    public string? RevealDemoPassword(UserRecord user)
    {
        if (string.IsNullOrEmpty(user.MirrorPasswordCipher)) return null;
        return Crypto.AesGcmDecrypt(user.MirrorPasswordCipher, DemoCipherKey(), "mirror-demo-password:" + user.Id);
    }

    //演示口令派生密钥
    private byte[] DemoCipherKey() => _cfg.DeriveKey("mirror-demo-password");
}
