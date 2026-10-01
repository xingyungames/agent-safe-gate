using System.Security.Cryptography;
using System.Text.Json;

namespace AiApproval.Core;

public sealed class AiOptions
{
    public string ApiKey { get; set; } = "";
    public string BaseUrl { get; set; } = "https://api.deepseek.com/chat/completions";
    /// <summary>出站主机白名单（逗号分隔）。默认只允许官方地址；自建/私有模型网关必须显式放行</summary>
    public string AllowedHosts { get; set; } = "";
    public string Model { get; set; } = "deepseek-chat";
    public int TimeoutSeconds { get; set; } = 25;
    public int MaxRetries { get; set; } = 1;
    public int MaxConcurrentCalls { get; set; } = 4;
    public int DailyCallsPerUser { get; set; } = 200;
    public int MaxOutputTokens { get; set; } = 900;

    /// <summary>
    /// AI 审核不可用（未配置密钥 / 调用失败 / 配额用尽）时，是否强制把所有"有副作用的操作"转人工审批。
    /// 默认 true —— "审核服务挂了就自动放行"是最典型的安全事故来源。
    /// 只有在明确接受该风险的场景（例如纯演练环境）才应设为 false。
    /// </summary>
    public bool RequireApprovalWhenUnavailable { get; set; } = true;

    /// <summary>
    /// 多步代理循环的最大步数（像代码助手那样：先检索 → 再读取 → 再写入）。
    /// 每一步都是一次模型调用，所以这里同时是"成本上限"；达到上限会用已获得的信息给结论。
    /// </summary>
    public int MaxAgentSteps { get; set; } = 5;

    /// <summary>整个多步循环的墙钟上限（秒），防止"助手在后台转圈"把请求拖死</summary>
    public int AgentTimeoutSeconds { get; set; } = 60;
}

public sealed class LimitOptions
{
    public int LoginPerFiveMinutesPerIp { get; set; } = 6;
    public int LoginPerFifteenMinutesPerAccount { get; set; } = 5;
    public int AccountLockMinutes { get; set; } = 15;
    public int AgentChatPerFiveMinutesPerUser { get; set; } = 20;
    public int AgentChatPerFiveMinutesPerIp { get; set; } = 60;
    public int ReadPerMinutePerUser { get; set; } = 180;
    public int AdminWritePerMinutePerAdmin { get; set; } = 60;

    /// <summary>业务对接台写操作限流（按账号 / 按 IP，窗口 5 分钟）。</summary>
    public int BizActionPerFiveMinutesPerUser { get; set; } = 30;
    public int BizActionPerFiveMinutesPerIp { get; set; } = 90;
    public int WafStrikesBeforeBan { get; set; } = 8;
    public int HoneypotStrikesBeforeBan { get; set; } = 3;
    public int BanMinutes { get; set; } = 60;
}

public sealed class MailOptions
{
    public bool Enabled { get; set; }
    public string Host { get; set; } = "";
    public int Port { get; set; } = 587;
    public bool UseSsl { get; set; } = true;
    public string Sender { get; set; } = "";
    public string Password { get; set; } = "";
    public string ApproverAddress { get; set; } = "";
    public string PublicBaseUrl { get; set; } = "http://127.0.0.1:8899";
}

/// <summary>落盘文件/目录名（集中管理，避免各处硬编码字符串）。</summary>
public static class StoreNames
{
    public const string Users = "users.json";
    public const string Tasks = "tasks.json";
    public const string Versions = "versions.json";
    public const string Outbox = "outbox.json";
    public const string Audit = "audit.jsonl";
    public const string Datasets = "datasets";
    public const string Workspaces = "workspaces";
    public const string Blobs = "blobs";
    public const string Secrets = "secrets.json";
}

/// <summary>运行配置 + 密钥管理。</summary>
public sealed class AppConfig
{
    public string ListenUrl { get; private set; } = "http://127.0.0.1:8899";
    public string DataRoot { get; private set; } = "";
    public string JwtSecret { get; private set; } = "";
    public string AesKeyB64 { get; private set; } = "";
    public string SecretPepper { get; private set; } = "";
    public int TokenMinutes { get; set; } = 120;
    public int NonceWindowSeconds { get; set; } = 300;
    public int ClockSkewSeconds { get; set; } = 300;
    public int ApprovalTokenMinutes { get; set; } = 240;
    public bool AllowSelfRegistration { get; set; } = true;
    public int SelfRegistrationPerHourPerIp { get; set; } = 3;
    public int MaxRequestBodyBytes { get; set; } = 262144;
    public int MaxUserInputChars { get; set; } = 2000;

    /// <summary>沙箱内低风险写是否免人工审批（见同名字段的说明）</summary>
    public bool AutoApproveSandboxWrites { get; set; } = true;

    /// <summary>
    /// 允许的跨站写请求来源（Origin 白名单）。
    /// 为什么需要它：服务通常只监听回环、由反向代理终止 TLS，此时浏览器发来的 Origin 是
    /// <c>https://你的域名</c>，而应用看到的请求是 <c>http://内网地址</c>，严格同源比较会误伤写请求。
    /// 默认**为空 = 保持严格同源校验**；配置后才放行这些具体来源（不支持通配符，避免变成万能放行）。
    /// </summary>
    public List<string> TrustedOrigins { get; } = new();

    public AiOptions Ai { get; private set; } = new();
    public LimitOptions Limits { get; private set; } = new();
    public MailOptions Mail { get; private set; } = new();

    /// <summary>AI 是否可用。未配置密钥时全系统进入"降级但更安全"的模式。</summary>
    public bool AiConfigured =>
        !string.IsNullOrWhiteSpace(Ai.ApiKey)
        && Ai.ApiKey.StartsWith("sk-", StringComparison.OrdinalIgnoreCase)
        && Ai.ApiKey.Length > 20;

    private readonly List<string> _warnings = new();
    public IReadOnlyList<string> StartupWarnings => _warnings;

    public string WorkspacesDir => Path.Combine(DataRoot, StoreNames.Workspaces);
    public string BlobsDir => Path.Combine(DataRoot, StoreNames.Blobs);
    public string DatasetsDir => Path.Combine(DataRoot, StoreNames.Datasets);

    public static AppConfig Load(IConfiguration config, string contentRoot)
    {
        var cfg = new AppConfig();

        cfg.ListenUrl = Env("AISERVER_LISTEN_URL") ?? config["Server:ListenUrl"] ?? cfg.ListenUrl;

        var dataRoot = Env("AISERVER_DATA_ROOT") ?? config["Security:DataRoot"] ?? "App_Data";
        cfg.DataRoot = Path.IsPathRooted(dataRoot) ? dataRoot : Path.Combine(contentRoot, dataRoot);
        Directory.CreateDirectory(cfg.DataRoot);

        cfg.TokenMinutes = Int(config["Security:TokenMinutes"], cfg.TokenMinutes);
        cfg.NonceWindowSeconds = Int(config["Security:NonceWindowSeconds"], cfg.NonceWindowSeconds);
        cfg.ClockSkewSeconds = Int(config["Security:ClockSkewSeconds"], cfg.ClockSkewSeconds);
        cfg.ApprovalTokenMinutes = Int(config["Security:ApprovalTokenMinutes"], cfg.ApprovalTokenMinutes);
        cfg.AllowSelfRegistration = Bool(config["Security:AllowSelfRegistration"], cfg.AllowSelfRegistration);
        cfg.SelfRegistrationPerHourPerIp = Int(config["Security:SelfRegistrationPerHourPerIp"], cfg.SelfRegistrationPerHourPerIp);
        cfg.MaxRequestBodyBytes = Int(config["Security:MaxRequestBodyBytes"], cfg.MaxRequestBodyBytes);
        cfg.MaxUserInputChars = Int(config["Security:MaxUserInputChars"], cfg.MaxUserInputChars);
        cfg.AutoApproveSandboxWrites = Bool(config["Security:AutoApproveSandboxWrites"], cfg.AutoApproveSandboxWrites);

        // 反向代理场景的 Origin 白名单：appsettings 数组（Security:TrustedOrigins:0/1/2…）与环境变量都支持
        var trustedFromEnv = Env("AISERVER_TRUSTED_ORIGINS");
        var trustedRaw = !string.IsNullOrWhiteSpace(trustedFromEnv)
            ? trustedFromEnv.Split(new[] { ';', ',', ' ' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            : config.GetSection("Security:TrustedOrigins").GetChildren().Select(c => c.Value ?? "").Where(v => v.Length > 0);
        foreach (var item in trustedRaw)
        {
            if (Uri.TryCreate(item, UriKind.Absolute, out var u))
                cfg.TrustedOrigins.Add(u.GetLeftPart(UriPartial.Authority));
            else
                cfg._warnings.Add($"Security:TrustedOrigins 里的“{item}”不是合法的完整来源（应形如 https://ops.example.com），已忽略。");
        }
        cfg.SecretPepper = Env("AISERVER_PEPPER") ?? config["Security:SecretPepper"] ?? cfg.SecretPepper;

        cfg.Ai = new AiOptions
        {
            ApiKey = (Env("AISERVER_AI_KEY") ?? config["Ai:ApiKey"] ?? "").Trim(),
            BaseUrl = config["Ai:BaseUrl"] ?? cfg.Ai.BaseUrl,
            AllowedHosts = Env("AISERVER_AI_ALLOWED_HOSTS") ?? config["Ai:AllowedHosts"] ?? "",
            Model = config["Ai:Model"] ?? cfg.Ai.Model,
            TimeoutSeconds = Int(config["Ai:TimeoutSeconds"], cfg.Ai.TimeoutSeconds),
            MaxRetries = Int(config["Ai:MaxRetries"], cfg.Ai.MaxRetries),
            MaxConcurrentCalls = Int(config["Ai:MaxConcurrentCalls"], cfg.Ai.MaxConcurrentCalls),
            DailyCallsPerUser = Int(config["Ai:DailyCallsPerUser"], cfg.Ai.DailyCallsPerUser),
            MaxOutputTokens = Int(config["Ai:MaxOutputTokens"], cfg.Ai.MaxOutputTokens),
            RequireApprovalWhenUnavailable = Bool(config["Ai:RequireApprovalWhenReviewUnavailable"], true),
            MaxAgentSteps = Int(config["Ai:MaxAgentSteps"], cfg.Ai.MaxAgentSteps),
            AgentTimeoutSeconds = Int(config["Ai:AgentTimeoutSeconds"], cfg.Ai.AgentTimeoutSeconds),        };

        cfg.Limits = new LimitOptions
        {
            LoginPerFiveMinutesPerIp = Int(config["Limits:LoginPerFiveMinutesPerIp"], cfg.Limits.LoginPerFiveMinutesPerIp),
            LoginPerFifteenMinutesPerAccount = Int(config["Limits:LoginPerFifteenMinutesPerAccount"], cfg.Limits.LoginPerFifteenMinutesPerAccount),
            AccountLockMinutes = Int(config["Limits:AccountLockMinutes"], cfg.Limits.AccountLockMinutes),
            AgentChatPerFiveMinutesPerUser = Int(config["Limits:AgentChatPerFiveMinutesPerUser"], cfg.Limits.AgentChatPerFiveMinutesPerUser),
            AgentChatPerFiveMinutesPerIp = Int(config["Limits:AgentChatPerFiveMinutesPerIp"], cfg.Limits.AgentChatPerFiveMinutesPerIp),
            ReadPerMinutePerUser = Int(config["Limits:ReadPerMinutePerUser"], cfg.Limits.ReadPerMinutePerUser),
            AdminWritePerMinutePerAdmin = Int(config["Limits:AdminWritePerMinutePerAdmin"], cfg.Limits.AdminWritePerMinutePerAdmin),
            BizActionPerFiveMinutesPerUser = Int(config["Limits:BizActionPerFiveMinutesPerUser"], cfg.Limits.BizActionPerFiveMinutesPerUser),
            BizActionPerFiveMinutesPerIp = Int(config["Limits:BizActionPerFiveMinutesPerIp"], cfg.Limits.BizActionPerFiveMinutesPerIp),
            WafStrikesBeforeBan = Int(config["Limits:WafStrikesBeforeBan"], cfg.Limits.WafStrikesBeforeBan),
            HoneypotStrikesBeforeBan = Int(config["Limits:HoneypotStrikesBeforeBan"], cfg.Limits.HoneypotStrikesBeforeBan),
            BanMinutes = Int(config["Limits:BanMinutes"], cfg.Limits.BanMinutes),
        };

        cfg.Mail = new MailOptions
        {
            Enabled = Bool(config["Mail:Enabled"], false),
            Host = config["Mail:Host"] ?? "",
            Port = Int(config["Mail:Port"], 587),
            UseSsl = Bool(config["Mail:UseSsl"], true),
            Sender = config["Mail:Sender"] ?? "",
            Password = Env("AISERVER_MAIL_PASSWORD") ?? config["Mail:Password"] ?? "",
            ApproverAddress = config["Mail:ApproverAddress"] ?? "",
            PublicBaseUrl = (config["Mail:PublicBaseUrl"] ?? cfg.ListenUrl).TrimEnd('/'),
        };

        cfg.ResolveSecrets(config);
        return cfg;
    }

    /// <summary>
    /// 密钥解析优先级：环境变量 → appsettings → 已持久化的 secrets.json → 首次生成并持久化。
    /// 生成而非固定值，保证"零配置也能跑"，同时避免仓库里出现硬编码密钥。
    /// </summary>
    private void ResolveSecrets(IConfiguration config)
    {
        var strict = Bool(config["Security:StrictSecrets"], false);
        var secretsPath = Path.Combine(DataRoot, StoreNames.Secrets);
        var persisted = new Dictionary<string, string>(StringComparer.Ordinal);

        if (File.Exists(secretsPath))
        {
            try
            {
                var raw = File.ReadAllText(secretsPath);
                var dict = JsonSerializer.Deserialize<Dictionary<string, string>>(raw);
                if (dict is not null) persisted = dict;
            }
            catch (Exception ex)
            {
                _warnings.Add($"secrets.json 读取失败，将重新生成：{ex.Message}");
            }
        }

        var envJwt = Env("AISERVER_JWT_SECRET");
        var envAes = Env("AISERVER_AES_KEY");
        var cfgJwt = config["Security:JwtSecret"];
        var cfgAes = config["Security:AesKey"];

        if (strict && string.IsNullOrWhiteSpace(envJwt) && string.IsNullOrWhiteSpace(cfgJwt) && !persisted.ContainsKey("jwt"))
            throw new InvalidOperationException("Security_StrictSecrets=true 但未提供 JWT 密钥（环境变量 AISERVER_JWT_SECRET 或 Security:JwtSecret）");

        JwtSecret = FirstNonEmpty(envJwt, cfgJwt, persisted.GetValueOrDefault("jwt"), RandomB64(48));
        AesKeyB64 = FirstNonEmpty(envAes, cfgAes, persisted.GetValueOrDefault("aes"), Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)));

        // AES 密钥必须是 32 字节
        try
        {
            if (Convert.FromBase64String(AesKeyB64).Length != 32)
                throw new CryptographicException("长度非 32 字节");
        }
        catch
        {
            _warnings.Add("AES 密钥非法，已重新生成 32 字节随机密钥（历史密文将无法解密）");
            AesKeyB64 = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32));
        }

        var changed = persisted.Count == 0
                      || !string.Equals(persisted.GetValueOrDefault("jwt"), JwtSecret, StringComparison.Ordinal)
                      || !string.Equals(persisted.GetValueOrDefault("aes"), AesKeyB64, StringComparison.Ordinal);

        if (changed && string.IsNullOrWhiteSpace(envJwt) && string.IsNullOrWhiteSpace(cfgJwt))
        {
            try
            {
                File.WriteAllText(secretsPath, JsonSerializer.Serialize(new Dictionary<string, string>
                {
                    ["jwt"] = JwtSecret,
                    ["aes"] = AesKeyB64,
                }, AppJson.Disk));
                TryRestrictFile(secretsPath);
                _warnings.Add("已自动生成服务端密钥并写入 App_Data/secrets.json；生产环境请改用环境变量注入。");
            }
            catch (Exception ex)
            {
                _warnings.Add($"密钥持久化失败（重启后需重新登录）：{ex.Message}");
            }
        }
    }

    private static void TryRestrictFile(string path)
    {
        try
        {
            if (!OperatingSystem.IsWindows())
                File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        }
        catch { /* 权限收紧失败不影响功能 */ }
    }

    /// <summary>把最长有效期的密钥材料派生出用途隔离的子密钥（不同用途不共用同一把密钥）。</summary>
    public byte[] DeriveKey(string purpose)
    {
        using var hmac = new HMACSHA256(Convert.FromBase64String(AesKeyB64));
        return hmac.ComputeHash(System.Text.Encoding.UTF8.GetBytes("aiapproval:" + purpose + ":" + SecretPepper));
    }

    public byte[] DeriveMacKey(string purpose)
    {
        using var hmac = new HMACSHA256(System.Text.Encoding.UTF8.GetBytes(JwtSecret));
        return hmac.ComputeHash(System.Text.Encoding.UTF8.GetBytes("aiapproval-mac:" + purpose));
    }

    private static string FirstNonEmpty(params string?[] values)
        => values.First(v => !string.IsNullOrWhiteSpace(v))!;

    private static string RandomB64(int bytes) => Convert.ToBase64String(RandomNumberGenerator.GetBytes(bytes));

    private static string? Env(string name)
    {
        var v = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrWhiteSpace(v) ? null : v;
    }

    private static int Int(string? raw, int fallback) => int.TryParse(raw, out var v) && v > 0 ? v : fallback;

    private static bool Bool(string? raw, bool fallback) => bool.TryParse(raw, out var v) ? v : fallback;
}
