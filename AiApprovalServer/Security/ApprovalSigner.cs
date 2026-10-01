using AiApproval.Core;

namespace AiApproval.Security;

/// <summary>
/// 审批链接签名（防 IDOR 越权审批）。
///
/// 邮件里给出的链接形如 /admin?task=&lt;id&gt;&amp;token=&lt;sig&gt;：
/// 令牌 = base64url(过期时间戳).base64url(HMAC-SHA256("approve|taskId|过期时间戳"))。
/// 攻击者拿到链路后：
///   * 改 taskId → 签名不匹配；
///   * 改过期时间 → 签名不匹配；
///   * 重放旧链接 → 服务端还会校验"该任务是否已被使用过令牌/已过期"。
/// 另外令牌在库里只存 SHA-256 摘要，即使 tasks.json 泄露也无法还原可用链接。
/// </summary>
public sealed class ApprovalSigner
{
    private readonly byte[] _key;

    public ApprovalSigner(AppConfig cfg) => _key = cfg.DeriveMacKey("approval-link");

    public string CreateToken(string taskId, DateTime expiresAtUtc)
    {
        var exp = new DateTimeOffset(expiresAtUtc).ToUnixTimeSeconds().ToString();
        var sig = System.Security.Cryptography.HMACSHA256.HashData(_key, System.Text.Encoding.UTF8.GetBytes(Payload(taskId, exp)));
        return Base64Url(System.Text.Encoding.UTF8.GetBytes(exp)) + "." + Base64Url(sig);
    }

    public string HashToken(string token) => Crypto.Sha256Hex(token);

    public bool Verify(string taskId, string? token, out DateTime expiresAt, out string reason)
    {
        expiresAt = default;
        reason = "";

        if (string.IsNullOrWhiteSpace(token)) { reason = "缺少审批令牌"; return false; }

        var parts = token.Split('.');
        if (parts.Length != 2) { reason = "审批令牌格式错误"; return false; }

        string expRaw;
        byte[] providedSig;
        try
        {
            expRaw = System.Text.Encoding.UTF8.GetString(Convert.FromBase64String(Pad(parts[0])));
            providedSig = Convert.FromBase64String(Pad(parts[1]));
        }
        catch
        {
            reason = "审批令牌格式错误";
            return false;
        }

        if (!long.TryParse(expRaw, out var expUnix)) { reason = "审批令牌格式错误"; return false; }

        // 签名按字节恒定时间比较（把 HMAC 字节转成字符串再比较会因非 UTF-8 非法序列而误判）
        var expectedSig = System.Security.Cryptography.HMACSHA256.HashData(_key, System.Text.Encoding.UTF8.GetBytes(Payload(taskId, expRaw)));
        if (!System.Security.Cryptography.CryptographicOperations.FixedTimeEquals(expectedSig, providedSig))
        {
            reason = "审批令牌签名无效（可能被篡改）";
            return false;
        }

        expiresAt = DateTimeOffset.FromUnixTimeSeconds(expUnix).UtcDateTime;
        if (expiresAt <= DateTime.UtcNow) { reason = "审批令牌已过期"; return false; }

        return true;
    }

    private static string Payload(string taskId, string exp) => "approve|" + taskId + "|" + exp;

    private static string Base64Url(byte[] data)
        => Convert.ToBase64String(data).Replace('+', '-').Replace('/', '_').TrimEnd('=');

    private static string Pad(string input)
    {
        var s = input.Replace('-', '+').Replace('_', '/');
        return (s.Length % 4) switch
        {
            2 => s + "==",
            3 => s + "=",
            _ => s,
        };
    }
}
