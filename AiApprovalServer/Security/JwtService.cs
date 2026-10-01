using System.Text;
using System.Text.Json;
using AiApproval.Core;

namespace AiApproval.Security;

/// <summary>JWT 载荷（自实现紧凑 JWT，避免引入第三方算法库带来的算法混淆面）。</summary>
public sealed class TokenPayload
{
    public string sub { get; set; } = "";
    public string name { get; set; } = "";
    public string role { get; set; } = "";
    public string sk { get; set; } = "";
    public string jti { get; set; } = "";
    public int epo { get; set; }
    public long iat { get; set; }
    public long exp { get; set; }
    public string iss { get; set; } = "";
    public string aud { get; set; } = "";
}

/// <summary>
/// 令牌签发与校验。
///
/// 安全要点：
///   * 只接受 HS256，显式拒绝 alg=none / 其它算法（算法混淆攻击）；
///   * 签名恒定时间比较；
///   * 校验 iss / aud / exp / iat（且 iat 不能在未来）；
///   * 载荷带 epo（令牌世代）：改密码、改角色、禁用账号都会 +1，使旧令牌立刻失效；
///   * 载荷带 sk（请求签名密钥）：所有"状态变更"请求必须用它做 HMAC 签名，防重放与篡改。
/// </summary>
public sealed class JwtService
{
    private const string Issuer = "aiapproval";
    private const string Audience = "aiapproval-web";
    private static readonly string[] AllowedAlgs = { "HS256" };

    private readonly byte[] _key;
    private readonly AppConfig _cfg;

    public JwtService(AppConfig cfg)
    {
        _cfg = cfg;
        _key = cfg.DeriveMacKey("jwt");
    }

    public string Issue(string userId, string userName, string role, int epoch, out string signKey, out DateTime expiresAt)
    {
        signKey = Crypto.RandomUrlSafe(32);
        expiresAt = DateTime.UtcNow.AddMinutes(_cfg.TokenMinutes);

        var payload = new TokenPayload
        {
            sub = userId,
            name = userName,
            role = role,
            sk = signKey,
            jti = Crypto.RandomUrlSafe(9),
            epo = epoch,
            iat = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            exp = new DateTimeOffset(expiresAt).ToUnixTimeSeconds(),
            iss = Issuer,
            aud = Audience,
        };

        var header = Base64Url(Encoding.UTF8.GetBytes("{\"alg\":\"HS256\",\"typ\":\"JWT\"}"));
        var body = Base64Url(JsonSerializer.SerializeToUtf8Bytes(payload, AppJson.Wire));
        var signing = header + "." + body;
        var sig = Base64Url(System.Security.Cryptography.HMACSHA256.HashData(_key, Encoding.UTF8.GetBytes(signing)));
        return signing + "." + sig;
    }

    /// <summary>校验并返回载荷；任何一项不满足都返回 null，不抛出（避免把校验细节泄露给调用方）。</summary>
    public TokenPayload? Validate(string? token, out string failure)
    {
        failure = "";
        if (string.IsNullOrWhiteSpace(token)) { failure = "缺少令牌"; return null; }

        var parts = token.Split('.');
        if (parts.Length != 3) { failure = "令牌格式错误"; return null; }

        // 头部必须先判定算法，任何非 HS256 一律拒绝
        string alg;
        try
        {
            using var headerDoc = JsonDocument.Parse(Base64UrlDecode(parts[0]));
            alg = headerDoc.RootElement.TryGetProperty("alg", out var algEl) ? algEl.GetString() ?? "" : "";
            var typ = headerDoc.RootElement.TryGetProperty("typ", out var typEl) ? typEl.GetString() ?? "" : "";
            if (!AllowedAlgs.Contains(alg, StringComparer.Ordinal) || !string.Equals(typ, "JWT", StringComparison.Ordinal))
            {
                failure = "不支持的签名算法";
                return null;
            }
        }
        catch { failure = "令牌头部非法"; return null; }

        var expected = Base64Url(System.Security.Cryptography.HMACSHA256.HashData(_key, Encoding.UTF8.GetBytes(parts[0] + "." + parts[1])));
        if (!Crypto.FixedEquals(expected, parts[2])) { failure = "令牌签名无效"; return null; }

        TokenPayload? payload;
        try
        {
            payload = JsonSerializer.Deserialize<TokenPayload>(Base64UrlDecode(parts[1]), AppJson.Wire);
        }
        catch { failure = "令牌载荷非法"; return null; }

        if (payload is null || string.IsNullOrWhiteSpace(payload.sub)) { failure = "令牌载荷不完整"; return null; }
        if (!string.Equals(payload.iss, Issuer, StringComparison.Ordinal) ||
            !string.Equals(payload.aud, Audience, StringComparison.Ordinal))
        {
            failure = "令牌签发方不匹配";
            return null;
        }

        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        if (payload.exp <= now) { failure = "令牌已过期"; return null; }
        if (payload.iat > now + 60) { failure = "令牌签发时间异常"; return null; }

        return payload;
    }

    private static string Base64Url(byte[] data)
        => Convert.ToBase64String(data).Replace('+', '-').Replace('/', '_').TrimEnd('=');

    private static byte[] Base64UrlDecode(string input)
    {
        var s = input.Replace('-', '+').Replace('_', '/');
        switch (s.Length % 4)
        {
            case 2: s += "=="; break;
            case 3: s += "="; break;
        }
        return Convert.FromBase64String(s);
    }
}
