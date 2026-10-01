using AiApproval.Ai;
using AiApproval.Api;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Services;
using AiApproval.Tools;

// 内容根目录解析：优先"自带 wwwroot 的目录"（发布目录 / 构建输出目录），
// 其次当前工作目录（dotnet run 场景）。这样无论怎么启动，静态页面都能被找到。
static string ResolveAppRoot()
{
    var candidates = new[]
    {
        AppContext.BaseDirectory,
        Directory.GetCurrentDirectory(),
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..")),
    };

    foreach (var candidate in candidates)
    {
        if (Directory.Exists(Path.Combine(candidate, "wwwroot"))) return candidate;
    }
    return AppContext.BaseDirectory;
}

var appRoot = ResolveAppRoot();

var builder = WebApplication.CreateBuilder(new WebApplicationOptions
{
    Args = args,
    ContentRootPath = appRoot,
    WebRootPath = Path.Combine(appRoot, "wwwroot"),
});

// ---------------------------------------------------------------- 配置与密钥
var cfg = AppConfig.Load(builder.Configuration, builder.Environment.ContentRootPath);
builder.WebHost.UseUrls(cfg.ListenUrl);

// 请求体上限再收一层（Kestrel 层直接拒绝超大请求，减少中间件消耗）
builder.WebHost.ConfigureKestrel(options =>
{
    options.Limits.MaxRequestBodySize = cfg.MaxRequestBodyBytes;
    options.Limits.RequestHeadersTimeout = TimeSpan.FromSeconds(15);
    options.Limits.KeepAliveTimeout = TimeSpan.FromSeconds(60);
    options.AddServerHeader = false;   // 不泄露服务器软件与版本
});

// ---------------------------------------------------------------- 依赖注入
builder.Services.AddSingleton(cfg);
builder.Services.AddSingleton(new JsonStore(cfg.DataRoot));
builder.Services.AddSingleton<AuditLog>();
builder.Services.AddSingleton<JwtService>();
builder.Services.AddSingleton<ApprovalSigner>();
builder.Services.AddSingleton(new SecurityGuard(cfg.Limits, cfg.NonceWindowSeconds));
builder.Services.AddSingleton<UserService>();
builder.Services.AddSingleton<PathGuard>();
builder.Services.AddSingleton<DatasetStore>();
builder.Services.AddSingleton<VersionStore>();
builder.Services.AddSingleton<Outbox>();
builder.Services.AddSingleton<Harness>();
builder.Services.AddSingleton<RuleEngine>();
builder.Services.AddSingleton<TaskService>();
builder.Services.AddSingleton<ChatSessionStore>();
builder.Services.AddSingleton<AiClient>();
builder.Services.AddSingleton<AgentOrchestrator>();
builder.Services.AddSingleton<BusinessSeed>();
builder.Services.AddHostedService<MaintenanceService>();

builder.Services.AddResponseCompression();

// 只用控制台日志：
//   1) Windows 上默认还会挂一个 EventLog provider，普通账号写系统事件日志会被拒绝（Win32 5），
//      而"写日志失败"会变成 AggregateException 冒泡到 Kestrel —— 直接导致连接被重置；
//   2) 服务日志里不应出现系统事件日志这种跨进程副作用，运维用控制台/重定向文件收集即可。
builder.Logging.ClearProviders();
builder.Logging.AddSimpleConsole(options =>
{
    options.SingleLine = true;
    options.TimestampFormat = "yyyy-MM-dd HH:mm:ss ";
});
builder.Logging.SetMinimumLevel(LogLevel.Information);

var app = builder.Build();

// ---------------------------------------------------------------- 启动期初始化
var users = app.Services.GetRequiredService<UserService>();
var datasets = app.Services.GetRequiredService<DatasetStore>();
var audit = app.Services.GetRequiredService<AuditLog>();

var seedNotes = await users.EnsureSeedAsync();
await datasets.EnsureSeedAsync();
var businessSeed = app.Services.GetRequiredService<BusinessSeed>();
var businessNotes = await businessSeed.EnsureAsync();
EnsureSharedWorkspace(cfg);

// 共享文件区：员工可读写、普通用户不可见（用于演示"角色权限差异"）
static void EnsureSharedWorkspace(AppConfig cfg)
{
    var shareRoot = Path.Combine(cfg.WorkspacesDir, "share");
    Directory.CreateDirectory(shareRoot);

    var sample = Path.Combine(shareRoot, "运营协作须知.md");
    if (!File.Exists(sample))
    {
        File.WriteAllText(sample,
            "# 运营协作须知\n\n"
            + "1. 共享区文件由运营工程师维护，普通用户无权访问。\n"
            + "2. 所有写入共享区的动作都会被强制转人工审批（影响面超出个人工作区）。\n"
            + "3. 每次写入前系统都会保存快照，管理员可在后台一键回退。\n", new System.Text.UTF8Encoding(false));
    }
}

// ---------------------------------------------------------------- 管道
// 顺序：安全中间件（含 WAF/蜜罐/签名/身份）→ 读接口限流 → 静态文件 → 业务端点
app.UseMiddleware<SecurityMiddleware>();

app.Use(async (context, next) =>
{
    if (context.Request.Path.StartsWithSegments("/api") && HttpMethods.IsGet(context.Request.Method))
    {
        var guard = context.RequestServices.GetRequiredService<SecurityGuard>();
        var identity = context.Items[SecurityMiddleware.IdentityKey] as RequestIdentity;
        var key = identity is null
            ? $"read:ip:{SecurityGuard.ClientIp(context)}"
            : $"read:user:{identity.User.Id}";

        if (!guard.TryConsume(key, cfg.Limits.ReadPerMinutePerUser, TimeSpan.FromMinutes(1)))
        {
            context.Response.StatusCode = 429;
            context.Response.ContentType = "application/json; charset=utf-8";
            await context.Response.WriteAsync("{\"ok\":false,\"code\":\"RATE_LIMITED\",\"message\":\"读取过于频繁，请稍后再试\"}");
            return;
        }
    }

    await next();
});

app.UseResponseCompression();
app.UseDefaultFiles();
app.UseStaticFiles(new StaticFileOptions
{
    OnPrepareResponse = ctx =>
    {
        // 前端页面不做缓存：改版后用户刷新即可拿到新版本，避免"旧 JS 打新接口"
        if (ctx.File.Name.EndsWith(".html", StringComparison.OrdinalIgnoreCase)
            || ctx.File.Name.EndsWith(".js", StringComparison.OrdinalIgnoreCase)
            || ctx.File.Name.EndsWith(".css", StringComparison.OrdinalIgnoreCase))
            ctx.Context.Response.Headers.CacheControl = "no-store";
    },
});

// ---------------------------------------------------------------- 接口
AuthApi.Map(app);
AgentApi.Map(app);
BizApi.Map(app);
AdminApi.Map(app);

// 页面入口显式映射（固定文件，不接受任何路径参数，杜绝路径穿越）。
// 不依赖 DefaultFiles 的隐式重写行为：显式映射的响应行为可预测，也便于叠加安全响应头。
var indexFile = Path.Combine(app.Environment.WebRootPath, "index.html");
var adminFile = Path.Combine(app.Environment.WebRootPath, "admin.html");
var bizFile = Path.Combine(app.Environment.WebRootPath, "biz.html");

app.MapGet("/", () => Results.File(indexFile, "text/html; charset=utf-8"));
app.MapGet("/index.html", () => Results.File(indexFile, "text/html; charset=utf-8"));
app.MapGet("/admin", () => Results.File(adminFile, "text/html; charset=utf-8"));
app.MapGet("/biz", () => Results.File(bizFile, "text/html; charset=utf-8"));
// 注意：路由模板 "/admin/" 与 "/admin" 等价，重复注册会触发 AmbiguousMatchException，
// 因此带斜杠/子路径的请求统一 302 到固定地址（目标是硬编码常量，不存在开放重定向）。
app.MapGet("/admin/{*rest}", () => Results.Redirect("/admin"));
app.MapGet("/biz/{*rest}", () => Results.Redirect("/biz"));

// 统一 404：不区分"路径不存在"与"路径被安全策略忽略"，不给攻击者反馈
app.MapFallback(async context =>
{
    context.Response.StatusCode = 404;
    context.Response.ContentType = "application/json; charset=utf-8";
    await context.Response.WriteAsync("{\"ok\":false,\"code\":\"NOT_FOUND\",\"message\":\"未找到资源\"}");
});

// ---------------------------------------------------------------- 启动横幅
var baseUrl = cfg.ListenUrl.Replace("0.0.0.0", "127.0.0.1").Replace("+", "127.0.0.1");
Console.WriteLine("==============================================================");
Console.WriteLine(" AI Agent 安全执行与审批系统（星云游安全执行平台）");
Console.WriteLine("==============================================================");
Console.WriteLine($" 用户前端   : {baseUrl}/");
Console.WriteLine($" 业务对接台 : {baseUrl}/biz");
Console.WriteLine($" 管理后台   : {baseUrl}/admin");
Console.WriteLine($" 数据目录   : {cfg.DataRoot}");
Console.WriteLine($" AI 状态    : {(cfg.AiConfigured ? "已配置（" + cfg.Ai.Model + "），异步调用 + 物理隔离审核" : "未配置 → 本地意图解析 + 变更类操作全部转人工审批")}");
Console.WriteLine($" 审批通知   : {(cfg.Mail.Enabled ? "SMTP 已启用" : "SMTP 未启用 → 通知写入出站箱（后台可查看/撤回）")}");
Console.WriteLine("--------------------------------------------------------------");

foreach (var warning in cfg.StartupWarnings) Console.WriteLine($" [提示] {warning}");

if (seedNotes.Count > 0)
{
    Console.WriteLine(" [演示账号] 首次启动已播种以下账号，请在生产环境立即改密或删除：");
    foreach (var note in seedNotes) Console.WriteLine($"   - {note}");
}

foreach (var note in businessNotes) Console.WriteLine($" [业务数据] {note}");

Console.WriteLine("--------------------------------------------------------------");
Console.WriteLine(" 安全基线：JWT+请求签名(防重放) | WAF+蜜罐+自动封禁 | 规则引擎硬拦截");
Console.WriteLine("           AI 审核物理隔离 | 人工审批签名链接 | 快照+Diff 一键回退 | 审计哈希链");
Console.WriteLine("==============================================================");

await audit.WriteAsync("system", "system", "system", "", "system.startup", "server", "SUCCESS",
    $"服务启动于 {cfg.ListenUrl}；AI={(cfg.AiConfigured ? "on" : "off")}；数据目录={cfg.DataRoot}");

app.Run();
