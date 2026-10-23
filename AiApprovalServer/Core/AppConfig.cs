using System.Security.Cryptography;
using System.Text.Json;

namespace AiApproval.Core;

public sealed class AiOptions
{
    public string ApiKey { get; set; } = "";
    public string BaseUrl { get; set; } = "https://api.deepseek.com/chat/completions";
    //出站主机白名单
    public string AllowedHosts { get; set; } = "";
    public string Model { get; set; } = "deepseek-chat";
    public int TimeoutSeconds { get; set; } = 25;
    public int MaxRetries { get; set; } = 1;
    public int MaxConcurrentCalls { get; set; } = 4;
    public int DailyCallsPerUser { get; set; } = 200;
    public int MaxOutputTokens { get; set; } = 900;

    //审核不可用时强制转人工审批
    public bool RequireApprovalWhenUnavailable { get; set; } = true;

    //多步代理循环最大步数
    public int MaxAgentSteps { get; set; } = 5;

    //多步循环总超时（秒）
    public int AgentTimeoutSeconds { get; set; } = 60;
}

//报销单 AI 预审配置（只出建议，永不执行）
public sealed class InvoiceAiOptions
{
    public bool Enabled { get; set; } = true;

    //一次 AI 调用打包多少张单
    public int BatchSize { get; set; } = 20;

    //一次"AI 选单"最多返回多少条
    public int MaxFindItems { get; set; } = 50;

    //一次批量审批最多多少单
    public int MaxBatchReview { get; set; } = 50;

    //评分缓存有效期（分钟）
    public int CacheTtlMinutes { get; set; } = 720;

    //报销审核的独立每日 AI 配额，与 Agent 对话配额分开
    public int DailyCallsPerUser { get; set; } = 400;

    //空 = 复用 Ai.Model
    public string Model { get; set; } = "";

    public int TimeoutSeconds { get; set; } = 40;
}

public sealed class LimitOptions
{
    public int LoginPerFiveMinutesPerIp { get; set; } = 120;
    public int LoginPerFifteenMinutesPerAccount { get; set; } = 5;
    public int AccountLockMinutes { get; set; } = 15;
    public int AgentChatPerFiveMinutesPerUser { get; set; } = 20;
    public int AgentChatPerFiveMinutesPerIp { get; set; } = 200;
    public int ReadPerMinutePerUser { get; set; } = 600;
    public int AdminWritePerMinutePerAdmin { get; set; } = 60;

    public int WafStrikesBeforeBan { get; set; } = 60;
    public int HoneypotStrikesBeforeBan { get; set; } = 50;
    public int BanMinutes { get; set; } = 10;
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

//飞书多维表格集成配置
public sealed class FeishuOptions
{
    public string AppId { get; set; } = "";
    public string AppSecret { get; set; } = "";
    public string AppToken { get; set; } = "";

    //报销单主表 ID
    public string TableId { get; set; } = "";

    //用户表 ID
    public string UserTableId { get; set; } = "";

    //出站主机白名单
    public string AllowedHosts { get; set; } = "open.feishu.cn";

    //飞书 API 基地址
    public string BaseUrl { get; set; } = "https://open.feishu.cn";

    //单次调用超时（毫秒）：报销单读写走这个值，飞书偶发慢时不能再 3 秒就判失败
    public int TimeoutMs { get; set; } = 30_000;

    //账号镜像的独立短超时（毫秒）：镜像是 best-effort，绝不能拖慢注册/登录
    public int MirrorTimeoutMs { get; set; } = 3_000;

    //失败重试次数
    public int MaxRetries { get; set; } = 1;

    //镜像口令策略
    public string MirrorPasswordMode { get; set; } = "demo";

    //演示口令长度
    public int DemoPasswordLength { get; set; } = 12;

    //镜像 PBKDF2 迭代次数
    public int MirrorPbkdf2Iterations { get; set; } = 100_000;

    //启动时检测外部账号
    public bool DetectForeignAccounts { get; set; } = true;

    //报销单主表网页链接
    public string TableUrl { get; set; } = "";

    //用户表网页链接
    public string UserTableUrl { get; set; } = "";

    //基础凭证是否齐备
    public bool Configured =>
        !string.IsNullOrWhiteSpace(AppId)
        && !string.IsNullOrWhiteSpace(AppSecret)
        && !string.IsNullOrWhiteSpace(AppToken);
}

//初始超级管理员引导配置
public sealed class BootstrapAdminOptions
{
    public bool Enabled { get; set; } = true;
    public string UserName { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string Password { get; set; } = "";
    public string PasswordHash { get; set; } = "";
    public string PasswordSalt { get; set; } = "";
    public int PasswordIterations { get; set; } = 210_000;

    //口令方案标识
    public string PasswordScheme { get; set; } = "";
}

//落盘文件名集中定义
public static class StoreNames
{
    public const string Users = "users.json";
    public const string Tasks = "tasks.json";
    public const string Versions = "versions.json";
    public const string Outbox = "outbox.json";
    public const string Audit = "audit.jsonl";
    public const string InvoiceAi = "invoice_ai.json";
    public const string Datasets = "datasets";
    public const string Workspaces = "workspaces";
    public const string Blobs = "blobs";
    public const string Secrets = "secrets.json";
}

//运行配置与密钥管理
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
    public int MaxRequestBodyBytes { get; set; } = 262144;
    public int MaxUserInputChars { get; set; } = 2000;

    //请求体 WAF 扫描（JSON body 也扫；关掉会让注入特征在 body 里通行）
    public bool WafScanBody { get; set; } = true;

    //口令慢哈希迭代次数
    public int PasswordIterations { get; set; } = Security.Crypto.PasswordIterationsDefault;

    //沙箱低风险写免审批
    public bool AutoApproveSandboxWrites { get; set; } = true;

    //跨站写请求的 Origin 白名单
    public List<string> TrustedOrigins { get; } = new();

    //IP 白名单：只豁免限流与自动封禁，不豁免认证/签名/权限/规则引擎
    public List<string> AllowlistIps { get; } = new();
    public bool AllowlistAuditEveryHit { get; set; } = true;

    //可信代理：只有「TCP 对端就是这些 IP」时，才采信它带来的 X-Client-Ip 头
    //（用于协作方网关服务端代打的场景：把真实用户 IP 还原出来，限流/锁定/审计才按真实来源算）
    public List<string> TrustedProxies { get; } = new();

    public AiOptions Ai { get; private set; } = new();
    public LimitOptions Limits { get; private set; } = new();
    public MailOptions Mail { get; private set; } = new();
    public FeishuOptions Feishu { get; private set; } = new();
    public BootstrapAdminOptions BootstrapAdmin { get; private set; } = new();
    public InvoiceAiOptions InvoiceAi { get; private set; } = new();

    //飞书凭证是否齐备
    public bool Configured =>
        !string.IsNullOrWhiteSpace(Feishu.AppId)
        && !string.IsNullOrWhiteSpace(Feishu.AppSecret)
        && !string.IsNullOrWhiteSpace(Feishu.AppToken);

    //飞书凭证是否齐备（含用户表）
    public bool FeishuConfigured =>
        Configured && !string.IsNullOrWhiteSpace(Feishu.UserTableId);

    //AI 是否可用
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
        cfg.WafScanBody = Bool(config["Security:WafScanBody"], cfg.WafScanBody);
        cfg.MaxRequestBodyBytes = Int(config["Security:MaxRequestBodyBytes"], cfg.MaxRequestBodyBytes);
        cfg.MaxUserInputChars = Int(config["Security:MaxUserInputChars"], cfg.MaxUserInputChars);
        cfg.PasswordIterations = Int(Env("AISERVER_PASSWORD_ITERATIONS") ?? config["Security:PasswordIterations"], cfg.PasswordIterations);
        cfg.AutoApproveSandboxWrites = Bool(config["Security:AutoApproveSandboxWrites"], cfg.AutoApproveSandboxWrites);

        //Origin 白名单：数组或环境变量
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

        //IP 白名单：只接受字面 IP，拒绝通配与 CIDR（防误配成后门）
        cfg.AllowlistAuditEveryHit = Bool(config["Security:AllowlistAuditEveryHit"], true);
        var allowFromEnv = Env("AISERVER_ALLOWLIST_IPS");
        var allowRaw = !string.IsNullOrWhiteSpace(allowFromEnv)
            ? allowFromEnv.Split(new[] { ';', ',', ' ' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            : config.GetSection("Security:AllowlistIps").GetChildren().Select(c => c.Value ?? "").Where(v => v.Length > 0);
        foreach (var item in allowRaw)
        {
            var raw = item.Trim();
            if (raw.Length == 0) continue;
            if (raw.Contains('*') || raw.Contains('/') || !System.Net.IPAddress.TryParse(raw, out var parsedIp))
            {
                cfg._warnings.Add($"Security:AllowlistIps 里的“{raw}”不是可解析的字面 IP（不支持 * 与 CIDR），已忽略。");
                continue;
            }
            var canon = parsedIp.MapToIPv4().ToString();
            if (!cfg.AllowlistIps.Contains(canon, StringComparer.Ordinal)) cfg.AllowlistIps.Add(canon);
        }

        //可信代理：同样只接受字面 IP；只有 TCP 对端命中这里，才采信 X-Client-Ip
        var proxyFromEnv = Env("AISERVER_TRUSTED_PROXIES");
        var proxyRaw = !string.IsNullOrWhiteSpace(proxyFromEnv)
            ? proxyFromEnv.Split(new[] { ';', ',', ' ' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            : config.GetSection("Security:TrustedProxies").GetChildren().Select(c => c.Value ?? "").Where(v => v.Length > 0);
        foreach (var item in proxyRaw)
        {
            var raw = item.Trim();
            if (raw.Length == 0) continue;
            if (raw.Contains('*') || raw.Contains('/') || !System.Net.IPAddress.TryParse(raw, out var proxyIp))
            {
                cfg._warnings.Add($"Security:TrustedProxies 里的“{raw}”不是可解析的字面 IP（不支持 * 与 CIDR），已忽略。");
                continue;
            }
            var proxyCanon = proxyIp.MapToIPv4().ToString();
            if (!cfg.TrustedProxies.Contains(proxyCanon, StringComparer.Ordinal)) cfg.TrustedProxies.Add(proxyCanon);
        }

        //私密配置只读一次，三段共用
        var secrets = ReadSecretsFile(contentRoot, cfg._warnings);

        cfg.Ai = new AiOptions
        {
            //密钥来源优先级
            ApiKey = FirstNonEmpty(Env("AISERVER_AI_KEY"), SecretsValue(secrets, "Ai", "ApiKey"), config["Ai:ApiKey"], "").Trim(),
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
            AgentTimeoutSeconds = Int(config["Ai:AgentTimeoutSeconds"], cfg.Ai.AgentTimeoutSeconds),
        };

        cfg.Limits = new LimitOptions
        {
            LoginPerFiveMinutesPerIp = Int(config["Limits:LoginPerFiveMinutesPerIp"], cfg.Limits.LoginPerFiveMinutesPerIp),
            LoginPerFifteenMinutesPerAccount = Int(config["Limits:LoginPerFifteenMinutesPerAccount"], cfg.Limits.LoginPerFifteenMinutesPerAccount),
            AccountLockMinutes = Int(config["Limits:AccountLockMinutes"], cfg.Limits.AccountLockMinutes),
            AgentChatPerFiveMinutesPerUser = Int(config["Limits:AgentChatPerFiveMinutesPerUser"], cfg.Limits.AgentChatPerFiveMinutesPerUser),
            AgentChatPerFiveMinutesPerIp = Int(config["Limits:AgentChatPerFiveMinutesPerIp"], cfg.Limits.AgentChatPerFiveMinutesPerIp),
            ReadPerMinutePerUser = Int(config["Limits:ReadPerMinutePerUser"], cfg.Limits.ReadPerMinutePerUser),
            AdminWritePerMinutePerAdmin = Int(config["Limits:AdminWritePerMinutePerAdmin"], cfg.Limits.AdminWritePerMinutePerAdmin),

            WafStrikesBeforeBan = Int(config["Limits:WafStrikesBeforeBan"], cfg.Limits.WafStrikesBeforeBan),
            HoneypotStrikesBeforeBan = Int(config["Limits:HoneypotStrikesBeforeBan"], cfg.Limits.HoneypotStrikesBeforeBan),
            BanMinutes = Int(config["Limits:BanMinutes"], cfg.Limits.BanMinutes),
        };

        cfg.InvoiceAi = new InvoiceAiOptions
        {
            Enabled = Bool(Env("AISERVER_INVOICE_AI_ENABLED") ?? config["InvoiceAi:Enabled"], true),
            BatchSize = Int(config["InvoiceAi:BatchSize"], cfg.InvoiceAi.BatchSize),
            MaxFindItems = Int(config["InvoiceAi:MaxFindItems"], cfg.InvoiceAi.MaxFindItems),
            MaxBatchReview = Int(config["InvoiceAi:MaxBatchReview"], cfg.InvoiceAi.MaxBatchReview),
            CacheTtlMinutes = Int(config["InvoiceAi:CacheTtlMinutes"], cfg.InvoiceAi.CacheTtlMinutes),
            DailyCallsPerUser = Int(config["InvoiceAi:DailyCallsPerUser"], cfg.InvoiceAi.DailyCallsPerUser),
            Model = config["InvoiceAi:Model"] ?? cfg.InvoiceAi.Model,
            TimeoutSeconds = Int(config["InvoiceAi:TimeoutSeconds"], cfg.InvoiceAi.TimeoutSeconds),
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

        cfg.Feishu = LoadFeishu(config, secrets, cfg._warnings);
        cfg.BootstrapAdmin = LoadBootstrapAdmin(config, secrets, cfg._warnings);

        cfg.ResolveSecrets(config);
        return cfg;
    }

    //定位私密配置文件
    private static string ResolveSecretsPath(string contentRoot)
    {
        var explicitPath = Env("AISERVER_SECRETS_FILE");
        if (!string.IsNullOrWhiteSpace(explicitPath)) return explicitPath;

        var local = Path.Combine(contentRoot, "config", "secrets.local.json");
        if (File.Exists(local)) return local;

        var dir = new DirectoryInfo(contentRoot);
        for (var depth = 0; depth < 4 && dir?.Parent is not null; depth++)
        {
            dir = dir.Parent;
            //只找带 wwwroot 的祖目录
            if (!Directory.Exists(Path.Combine(dir.FullName, "wwwroot"))) continue;
            var candidate = Path.Combine(dir.FullName, "config", "secrets.local.json");
            if (File.Exists(candidate)) return candidate;
        }

        return "";
    }

    //读私密配置里的一个值
    private static string SecretsValue(Dictionary<string, JsonElement> secrets, string section, string key)
    {
        if (!secrets.TryGetValue(section, out var node) || node.ValueKind != JsonValueKind.Object) return "";
        if (!node.TryGetProperty(key, out var value)) return "";
        return value.ValueKind == JsonValueKind.String ? (value.GetString() ?? "") : "";
    }

    //把私密配置某段摊平成字典
    private static Dictionary<string, string> SecretsSection(Dictionary<string, JsonElement> secrets, string section)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!secrets.TryGetValue(section, out var node) || node.ValueKind != JsonValueKind.Object) return result;

        foreach (var p in node.EnumerateObject())
            result[p.Name] = p.Value.ValueKind == JsonValueKind.String ? (p.Value.GetString() ?? "") : p.Value.ToString();
        return result;
    }

    //读本机私密配置
    private static Dictionary<string, JsonElement> ReadSecretsFile(string contentRoot, List<string> warnings)
    {
        var result = new Dictionary<string, JsonElement>(StringComparer.OrdinalIgnoreCase);
        var secretPath = ResolveSecretsPath(contentRoot);
        if (secretPath.Length == 0 || !File.Exists(secretPath)) return result;

        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(secretPath));
            foreach (var p in doc.RootElement.EnumerateObject())
                result[p.Name] = p.Value.Clone();
        }
        catch (Exception ex)
        {
            warnings.Add($"config/secrets.local.json 解析失败（已忽略）：{ex.Message}");
        }
        return result;
    }

    //初始超级管理员配置解析
    private static BootstrapAdminOptions LoadBootstrapAdmin(IConfiguration config,
        Dictionary<string, JsonElement> secrets, List<string> warnings)
    {
        var opts = new BootstrapAdminOptions();
        var file = SecretsSection(secrets, "BootstrapAdmin");

        string Pick(string key, string fallback) =>
            FirstNonEmpty(Env($"AISERVER_BOOTSTRAP_{key.ToUpperInvariant()}"),
                file.GetValueOrDefault(key), config[$"Bootstrap:Admin:{key}"], fallback);

        opts.Enabled = Bool(Pick("Enabled", "true"), true);
        opts.UserName = Pick("UserName", opts.UserName).Trim();
        opts.DisplayName = Pick("DisplayName", opts.UserName).Trim();
        opts.Password = Pick("Password", "");
        opts.PasswordHash = Pick("PasswordHash", "").Trim();
        opts.PasswordSalt = Pick("PasswordSalt", "").Trim();
        opts.PasswordIterations = Int(Pick("PasswordIterations", opts.PasswordIterations.ToString()), opts.PasswordIterations);
        opts.PasswordScheme = Pick("PasswordScheme", "").Trim();

        var hasHash = IsBase64(opts.PasswordHash) && IsBase64(opts.PasswordSalt);
        if (opts.PasswordHash.Length > 0 && !hasHash)
        {
            warnings.Add("Bootstrap:Admin 的 PasswordHash/PasswordSalt 不是合法 base64，已忽略该哈希。");
            opts.PasswordHash = "";
            opts.PasswordSalt = "";
        }

        if (opts.Enabled && opts.UserName.Length == 0)
        {
            warnings.Add("Bootstrap:Admin:Enabled=true 但 UserName 为空，已跳过初始超管播种。");
            opts.Enabled = false;
        }

        if (opts.Enabled && !hasHash && opts.Password.Length == 0)
        {
            warnings.Add("Bootstrap:Admin 未提供 PasswordHash/PasswordSalt 也未提供 Password，已跳过初始超管播种。");
            opts.Enabled = false;
        }

        //明文口令会触发启动告警
        if (opts.Enabled && !hasHash && opts.Password.Length > 0)
            warnings.Add("Bootstrap:Admin 用的是明文 Password：首次播种会算成哈希写入 users.json，"
                       + "但请确认这段配置不在会提交/交付的文件里（推荐改用 tools/hash-password.mjs 生成 PasswordHash 与 PasswordSalt）。");

        return opts;
    }

    private static bool IsBase64(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return false;
        Span<byte> buffer = new byte[value.Length];
        return Convert.TryFromBase64String(value, buffer, out _);
    }

    //飞书凭证解析
    private static FeishuOptions LoadFeishu(IConfiguration config,
        Dictionary<string, JsonElement> secrets, List<string> warnings)
    {
        var opts = new FeishuOptions();
        var file = SecretsSection(secrets, "Feishu");

        string Pick(string key, string fallback) =>
            FirstNonEmpty(Env($"AISERVER_FEISHU_{key.ToUpperInvariant()}"), file.GetValueOrDefault(key), config[$"Feishu:{key}"], fallback);

        opts.AppId = Pick("AppId", "");
        opts.AppSecret = Pick("AppSecret", "");
        opts.AppToken = Pick("AppToken", "");
        opts.TableId = Pick("TableId", "");
        opts.UserTableId = Pick("UserTableId", config["Feishu:UserTableId"] ?? "");
        opts.TableUrl = Pick("TableUrl", "");
        opts.UserTableUrl = Pick("UserTableUrl", "");
        opts.AllowedHosts = config["Feishu:AllowedHosts"] ?? opts.AllowedHosts;
        opts.BaseUrl = (config["Feishu:BaseUrl"] ?? opts.BaseUrl).TrimEnd('/');
        opts.TimeoutMs = Int(config["Feishu:TimeoutMs"], opts.TimeoutMs);
        opts.MirrorTimeoutMs = Int(config["Feishu:MirrorTimeoutMs"], opts.MirrorTimeoutMs);
        opts.MaxRetries = Int(config["Feishu:MaxRetries"], opts.MaxRetries);
        opts.MirrorPasswordMode = (config["Feishu:MirrorPasswordMode"] ?? opts.MirrorPasswordMode).Trim().ToLowerInvariant();
        opts.DemoPasswordLength = Int(config["Feishu:DemoPasswordLength"], opts.DemoPasswordLength);
        opts.MirrorPbkdf2Iterations = Int(config["Feishu:MirrorPbkdf2Iterations"], opts.MirrorPbkdf2Iterations);
        opts.DetectForeignAccounts = Bool(config["Feishu:DetectForeignAccounts"], opts.DetectForeignAccounts);

        //从飞书地址里抠出 ID
        NormalizeFeishuTargets(opts, warnings);

        if (opts.MirrorPasswordMode is not ("demo" or "real" or "none"))
        {
            warnings.Add($"Feishu:MirrorPasswordMode 取值非法（{opts.MirrorPasswordMode}），已按 demo 处理。");
            opts.MirrorPasswordMode = "demo";
        }

        return opts;
    }

    //把飞书地址还原成 ID，幂等
    private static void NormalizeFeishuTargets(FeishuOptions opts, List<string> warnings)
    {
        var token = ExtractBaseToken(opts.AppToken);
        if (token.Length > 0 && token != opts.AppToken)
        {
            warnings.Add("Feishu:AppToken 看起来是一条飞书地址，已自动取出其中的 base 标识。");
            opts.AppToken = token;
        }

        var tableId = ExtractTableId(opts.TableId, out var tableFromUrl);
        if (tableId.Length > 0 && tableId != opts.TableId)
        {
            warnings.Add("Feishu:TableId 看起来是一条飞书地址，已自动取出其中的 table 标识。");
            opts.TableId = tableId;
        }
        if (opts.TableUrl.Length == 0) opts.TableUrl = tableFromUrl;

        var userTableId = ExtractTableId(opts.UserTableId, out var userTableFromUrl);
        if (userTableId.Length > 0 && userTableId != opts.UserTableId)
        {
            warnings.Add("Feishu:UserTableId 后面带了视图参数，已自动截断（table_id 不含 &view=…）。");
            opts.UserTableId = userTableId;
        }
        if (opts.UserTableUrl.Length == 0) opts.UserTableUrl = userTableFromUrl;

        //只配 ID 时补全网页地址
        if (opts.TableUrl.Length == 0 && opts.AppToken.Length > 0 && opts.TableId.Length > 0)
            opts.TableUrl = $"https://my.feishu.cn/base/{opts.AppToken}?table={opts.TableId}";
        if (opts.UserTableUrl.Length == 0 && opts.AppToken.Length > 0 && opts.UserTableId.Length > 0)
            opts.UserTableUrl = $"https://my.feishu.cn/base/{opts.AppToken}?table={opts.UserTableId}";
    }

    //从地址里取 base 标识
    private static string ExtractBaseToken(string raw)
    {
        var value = (raw ?? "").Trim();
        var marker = value.IndexOf("/base/", StringComparison.OrdinalIgnoreCase);
        if (marker < 0) return value;

        var rest = value[(marker + "/base/".Length)..];
        var cut = rest.IndexOfAny(new[] { '?', '&', '/', '#' });
        return cut >= 0 ? rest[..cut] : rest;
    }

    //从地址或 ID 里取 table 标识
    private static string ExtractTableId(string raw, out string sourceUrl)
    {
        var value = (raw ?? "").Trim();
        sourceUrl = value.Contains("://", StringComparison.Ordinal) ? value : "";

        var marker = value.IndexOf("table=", StringComparison.OrdinalIgnoreCase);
        if (marker >= 0) value = value[(marker + "table=".Length)..];

        var cut = value.IndexOfAny(new[] { '?', '&', '/', '#', ' ' });
        return (cut >= 0 ? value[..cut] : value).Trim();
    }

    //密钥解析顺序
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

        //AES 密钥必须 32 字节
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
        catch { /* 收紧权限失败忽略 */ }
    }

    //按用途派生子密钥
    public byte[] DeriveKey(string purpose)
    {
        using var hmac = new HMACSHA256(Convert.FromBase64String(AesKeyB64));
        return hmac.ComputeHash(System.Text.Encoding.UTF8.GetBytes("aiapproval:" + purpose + ":" + SecretPepper));
    }

    //按用途派生 MAC 子密钥
    public byte[] DeriveMacKey(string purpose)
    {
        using var hmac = new HMACSHA256(System.Text.Encoding.UTF8.GetBytes(JwtSecret));
        return hmac.ComputeHash(System.Text.Encoding.UTF8.GetBytes("aiapproval-mac:" + purpose));
    }

    //取第一个非空值
    private static string FirstNonEmpty(params string?[] values)
        => values.FirstOrDefault(v => !string.IsNullOrWhiteSpace(v)) ?? "";

    private static string RandomB64(int bytes) => Convert.ToBase64String(RandomNumberGenerator.GetBytes(bytes));

    private static string? Env(string name)
    {
        var v = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrWhiteSpace(v) ? null : v;
    }

    private static int Int(string? raw, int fallback) => int.TryParse(raw, out var v) && v > 0 ? v : fallback;

    private static bool Bool(string? raw, bool fallback) => bool.TryParse(raw, out var v) ? v : fallback;
}
