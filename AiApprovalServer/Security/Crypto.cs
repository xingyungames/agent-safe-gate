using System.Security.Cryptography;
using System.Text;

namespace AiApproval.Security;

//密码学原语集中封装
public static class Crypto
{
    //口令哈希默认迭代次数
    public const int PasswordIterationsDefault = 1_200_000;

    //当前口令方案
    public const string PasswordSchemeCurrent = "pbkdf2-sha256+pepper";

    //旧口令方案
    public const string PasswordSchemeLegacy = "pbkdf2-sha256";

    //协作方口令方案
    public const string PasswordSchemePartner = "partner-pbkdf2-sha256";

    private const int SaltBytes = 16;
    private const int NonceBytes = 12;
    private const int TagBytes = 16;

    public static string RandomB64(int bytes) => Convert.ToBase64String(RandomNumberGenerator.GetBytes(bytes));

    public static string RandomHex(int bytes) => Convert.ToHexString(RandomNumberGenerator.GetBytes(bytes)).ToLowerInvariant();

    //URL 安全随机串
    public static string RandomUrlSafe(int bytes) =>
        Convert.ToBase64String(RandomNumberGenerator.GetBytes(bytes)).Replace('+', '-').Replace('/', '_').TrimEnd('=');

    public static byte[] Pbkdf2(byte[] password, byte[] salt, int iterations)
        => Rfc2898DeriveBytes.Pbkdf2(password, salt, iterations, HashAlgorithmName.SHA256, 32);

    //服务端密钥参与口令摘要
    public static byte[] Pepper(byte[] pepperKey, string password)
        => HMACSHA256.HashData(pepperKey, Encoding.UTF8.GetBytes(password));

    //生成口令材料
    public static (string hash, string salt, int iterations, string scheme) HashPassword(
        string password, byte[] pepperKey, int iterations = PasswordIterationsDefault)
    {
        var salt = RandomNumberGenerator.GetBytes(SaltBytes);
        var hash = Pbkdf2(Pepper(pepperKey, password), salt, iterations);
        return (Convert.ToBase64String(hash), Convert.ToBase64String(salt), iterations, PasswordSchemeCurrent);
    }

    //恒定时间校验口令；dummy=true 时付出与正常校验完全等价的代价后返回 false
    //（账号不存在 / 已禁用 / 已锁定 都走这里，消除"响应时间差可枚举账号"的侧信道）
    public static bool VerifyPassword(string password, string hashB64, string saltB64, int iterations,
        string scheme, byte[] pepperKey, bool dummy = false)
    {
        if (iterations <= 0) iterations = PasswordIterationsDefault;

        //恒定一次 PBKDF2：不因账号状态而多算或少算
        var material = UsesPepper(scheme) ? Pepper(pepperKey, password) : Encoding.UTF8.GetBytes(password);
        var actual = Pbkdf2(material, TrySalt(saltB64), iterations);

        if (dummy) return false;

        byte[] expected;
        try { expected = Convert.FromBase64String(hashB64); }
        catch { return false; }

        return CryptographicOperations.FixedTimeEquals(expected, actual);
    }

    //该方案是否用服务端密钥
    public static bool UsesPepper(string scheme)
        => string.Equals(scheme, PasswordSchemeCurrent, StringComparison.OrdinalIgnoreCase);

    //参数落后需静默升级
    public static bool NeedsUpgrade(string scheme, int iterations, int targetIterations)
        => !string.Equals(scheme, PasswordSchemeCurrent, StringComparison.OrdinalIgnoreCase)
           || iterations < targetIterations;

    private static byte[] TrySalt(string saltB64)
    {
        try { return Convert.FromBase64String(saltB64); }
        catch { return new byte[SaltBytes]; }
    }

    //AES-256-GCM 加密
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

    //AES-256-GCM 解密
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
            //认证失败一律返回 null
            return null;
        }
    }

    //HMAC-SHA256 十六进制
    public static string HmacHex(string message, byte[] key)
        => Convert.ToHexString(HMACSHA256.HashData(key, Encoding.UTF8.GetBytes(message))).ToLowerInvariant();

    public static string HmacB64(string message, byte[] key)
        => Convert.ToBase64String(HMACSHA256.HashData(key, Encoding.UTF8.GetBytes(message)));

    //恒定时间比较，防计时侧信道
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

    //脱敏展示
    public static string Mask(string? value, int keepHead = 2, int keepTail = 2)
    {
        if (string.IsNullOrEmpty(value)) return "";
        if (value.Length <= keepHead + keepTail) return new string('*', value.Length);
        return value[..keepHead] + new string('*', Math.Min(6, value.Length - keepHead - keepTail)) + value[^keepTail..];
    }
}
