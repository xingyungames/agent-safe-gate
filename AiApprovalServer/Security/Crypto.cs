using System.Security.Cryptography;
using System.Text;

namespace AiApproval.Security;

/// <summary>
/// 密码学原语集中封装：密码摘要、对称加密、MAC、恒定时间比较。
/// 全部使用 .NET 内置实现，不自行发明算法，也不使用已被淘汰的 DES/MD5/SHA1。
/// </summary>
public static class Crypto
{
    public const int Pbkdf2Iterations = 210_000;
    private const int SaltBytes = 16;
    private const int NonceBytes = 12;
    private const int TagBytes = 16;

    public static string RandomB64(int bytes) => Convert.ToBase64String(RandomNumberGenerator.GetBytes(bytes));

    public static string RandomHex(int bytes) => Convert.ToHexString(RandomNumberGenerator.GetBytes(bytes)).ToLowerInvariant();

    /// <summary>URL 安全随机串（用于 nonce、任务 ID、审批令牌）。</summary>
    public static string RandomUrlSafe(int bytes) =>
        Convert.ToBase64String(RandomNumberGenerator.GetBytes(bytes)).Replace('+', '-').Replace('/', '_').TrimEnd('=');

    public static byte[] Pbkdf2(string password, byte[] salt, int iterations = Pbkdf2Iterations)
        => Rfc2898DeriveBytes.Pbkdf2(password, salt, iterations, HashAlgorithmName.SHA256, 32);

    public static (string hash, string salt, int iterations) HashPassword(string password)
    {
        var salt = RandomNumberGenerator.GetBytes(SaltBytes);
        var hash = Pbkdf2(password, salt);
        return (Convert.ToBase64String(hash), Convert.ToBase64String(salt), Pbkdf2Iterations);
    }

    /// <summary>
    /// 恒定时间口令校验。即使账号不存在也调用一次（调用方传 dummy=true），
    /// 让"用户名不存在"与"密码错误"的耗时一致，避免用户名枚举。
    /// </summary>
    public static bool VerifyPassword(string password, string hashB64, string saltB64, int iterations, bool dummy = false)
    {
        byte[] expected;
        try
        {
            expected = dummy
                ? Pbkdf2("dummy-password-for-timing", Convert.FromBase64String(saltB64), iterations)
                : Convert.FromBase64String(hashB64);
        }
        catch
        {
            return false;
        }

        var actual = Pbkdf2(password, TrySalt(saltB64), iterations);
        var equal = CryptographicOperations.FixedTimeEquals(expected, actual);
        return !dummy && equal;
    }

    private static byte[] TrySalt(string saltB64)
    {
        try { return Convert.FromBase64String(saltB64); }
        catch { return new byte[SaltBytes]; }
    }

    /// <summary>AES-256-GCM 加密。输出 base64(nonce | tag | cipher)。AAD 绑定用途，防止密文被跨场景搬用。</summary>
    public static string AesGcmEncrypt(string plaintext, byte[] key, string aad)
    {
        var nonce = RandomNumberGenerator.GetBytes(NonceBytes);
        var plain = Encoding.UTF8.GetBytes(plaintext);
        var cipher = new byte[plain.Length];
        var tag = new byte[TagBytes];

        using var aes = new AesGcm(key, TagBytes);
        aes.Encrypt(nonce, plain, cipher, tag, Encoding.UTF8.GetBytes(aad));

        var output = new byte[NonceBytes + TagBytes + cipher.Length];
        Buffer.BlockCopy(nonce, 0, output, 0, NonceBytes);
        Buffer.BlockCopy(tag, 0, output, NonceBytes, TagBytes);
        Buffer.BlockCopy(cipher, 0, output, NonceBytes + TagBytes, cipher.Length);
        return Convert.ToBase64String(output);
    }

    public static string? AesGcmDecrypt(string cipherB64, byte[] key, string aad)
    {
        try
        {
            var blob = Convert.FromBase64String(cipherB64);
            if (blob.Length < NonceBytes + TagBytes) return null;

            var nonce = blob.AsSpan(0, NonceBytes);
            var tag = blob.AsSpan(NonceBytes, TagBytes);
            var cipher = blob.AsSpan(NonceBytes + TagBytes);
            var plain = new byte[cipher.Length];

            using var aes = new AesGcm(key, TagBytes);
            aes.Decrypt(nonce, cipher, tag, plain, Encoding.UTF8.GetBytes(aad));
            return Encoding.UTF8.GetString(plain);
        }
        catch
        {
            // 认证失败 = 密文被篡改或密钥不匹配，一律返回 null，绝不返回部分明文
            return null;
        }
    }

    public static string HmacHex(string message, byte[] key)
        => Convert.ToHexString(HMACSHA256.HashData(key, Encoding.UTF8.GetBytes(message))).ToLowerInvariant();

    public static string HmacB64(string message, byte[] key)
        => Convert.ToBase64String(HMACSHA256.HashData(key, Encoding.UTF8.GetBytes(message)));

    /// <summary>恒定时间字符串比较（长度不同也不提前返回，避免计时侧信道）。</summary>
    public static bool FixedEquals(string? a, string? b)
    {
        if (a is null || b is null) return false;
        var ba = Encoding.UTF8.GetBytes(a);
        var bb = Encoding.UTF8.GetBytes(b);
        var len = Math.Max(ba.Length, bb.Length);
        var padA = new byte[len];
        var padB = new byte[len];
        Buffer.BlockCopy(ba, 0, padA, 0, ba.Length);
        Buffer.BlockCopy(bb, 0, padB, 0, bb.Length);
        return CryptographicOperations.FixedTimeEquals(padA, padB) && ba.Length == bb.Length;
    }

    public static string Sha256Hex(byte[] data) => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();

    public static string Sha256Hex(string text) => Sha256Hex(Encoding.UTF8.GetBytes(text));

    /// <summary>脱敏展示（日志/界面用），保留首尾少量字符。</summary>
    public static string Mask(string? value, int keepHead = 2, int keepTail = 2)
    {
        if (string.IsNullOrEmpty(value)) return "";
        if (value.Length <= keepHead + keepTail) return new string('*', value.Length);
        return value[..keepHead] + new string('*', Math.Min(6, value.Length - keepHead - keepTail)) + value[^keepTail..];
    }
}
