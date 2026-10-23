using AiApproval.Ai;
using AiApproval.Api;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Services;
using AiApproval.Tools;

//找静态页面根目录
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

//配置与密钥
var cfg = AppConfig.Load(builder.Configuration, builder.Environment.ContentRootPath);
builder.WebHost.UseUrls(cfg.ListenUrl);

//限制请求体大小
builder.WebHost.ConfigureKestrel(options =>
{
    options.Limits.MaxRequestBodySize = cfg.MaxRequestBodyBytes;
    options.Limits.RequestHeadersTimeout = TimeSpan.FromSeconds(15);
    options.Limits.KeepAliveTimeout = TimeSpan.FromSeconds(60);
    options.AddServerHeader = false;   //不泄露服务器版本
});

//注册服务
builder.Services.AddSingleton(cfg);
builder.Services.AddSingleton(new JsonStore(cfg.DataRoot));
builder.Services.AddSingleton<AuditLog>();
builder.Services.AddSingleton<JwtService>();
builder.Services.AddSingleton<ApprovalSigner>();
builder.Services.AddSingleton(new SecurityGuard(cfg.Limits, cfg.NonceWindowSeconds, cfg.AllowlistIps));
builder.Services.AddSingleton<FeishuApi>();
builder.Services.AddSingleton<FeishuMirror>();
builder.Services.AddSingleton<InvoiceService>();
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
builder.Services.AddSingleton<InvoiceAiService>();
builder.Services.AddSingleton<AgentOrchestrator>();
builder.Services.AddHostedService<MaintenanceService>();

builder.Services.AddResponseCompression();

//只用控制台日志
builder.Logging.ClearProviders();
builder.Logging.AddSimpleConsole(options =>
{
    options.SingleLine = true;
    options.TimestampFormat = "yyyy-MM-dd HH:mm:ss ";
});
builder.Logging.SetMinimumLevel(LogLevel.Information);

var app = builder.Build();

//启动期初始化
var users = app.Services.GetRequiredService<UserService>();
var datasets = app.Services.GetRequiredService<DatasetStore>();
var audit = app.Services.GetRequiredService<AuditLog>();

var seedNotes = await users.EnsureSeedAsync();
//先播种演示账号再播种超管
var bootstrapNote = await users.SeedBootstrapSuperAdminAsync();
EnsureSharedWorkspace(cfg);

//飞书有本机无的账号只记审计
if (cfg.FeishuConfigured && cfg.Feishu.DetectForeignAccounts)
{
    var mirror = app.Services.GetRequiredService<FeishuMirror>();
    var rows = await mirror.ListUsersAsync();
    if (rows.Count > 0)
    {
        var local = (await users.AllAsync())
            .Select(u => u.UserName)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        var foreign = rows.Where(r => !string.IsNullOrWhiteSpace(r.UserName) && !local.Contains(r.UserName)).ToList();
        foreach (var row in foreign)
        {
            await audit.WriteAsync("system", "system", "system", "", "feishu.mirror.foreign-account",
                row.UserName, "IGNORED",
                $"飞书用户表存在但本机账号库没有：{row.UserName}（{row.RealName}，工号 {row.EmployeeId}，对面角色 {row.RoleLabel}）；本系统登录只读本机 JSON，该账号无法登录");
        }
        if (foreign.Count > 0)
            Console.WriteLine($" [镜像] 发现 {foreign.Count} 个仅存在于飞书用户表的账号，已记审计并忽略（它们无法登录本系统）");
    }
}

//建共享工作区
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

//中间件管道（安全最先）
app.UseMiddleware<SecurityMiddleware>();

//读接口限流
app.Use(async (context, next) =>
{
    if (context.Request.Path.StartsWithSegments("/api") && HttpMethods.IsGet(context.Request.Method))
    {
        var guard = context.RequestServices.GetRequiredService<SecurityGuard>();
        var identity = context.Items[SecurityMiddleware.IdentityKey] as RequestIdentity;
        var clientIp = SecurityGuard.ClientIp(context);
        var key = identity is null
            ? $"read:ip:{clientIp}"
            : $"read:user:{identity.User.Id}";

        if (!guard.TryConsume(key, cfg.Limits.ReadPerMinutePerUser, TimeSpan.FromMinutes(1), clientIp))
        {
            //超限直接拒绝
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
//静态文件
app.UseStaticFiles(new StaticFileOptions
{
    OnPrepareResponse = ctx =>
    {
        //前端页面不缓存
        if (ctx.File.Name.EndsWith(".html", StringComparison.OrdinalIgnoreCase)
            || ctx.File.Name.EndsWith(".js", StringComparison.OrdinalIgnoreCase)
            || ctx.File.Name.EndsWith(".css", StringComparison.OrdinalIgnoreCase))
            ctx.Context.Response.Headers.CacheControl = "no-store";
    },
});

//注册接口
AuthApi.Map(app);
AgentApi.Map(app);
InvoiceApi.Map(app);
AdminApi.Map(app);

//页面入口固定映射
var indexFile = Path.Combine(app.Environment.WebRootPath, "index.html");
var adminFile = Path.Combine(app.Environment.WebRootPath, "admin.html");
var financeFile = Path.Combine(app.Environment.WebRootPath, "finance.html");

//页面不缓存，改完 html 刷新就生效
static IResult Page(HttpContext ctx, string path)
{
    ctx.Response.Headers.CacheControl = "no-store";
    return Results.File(path, "text/html; charset=utf-8");
}

app.MapGet("/", (HttpContext ctx) => Page(ctx, indexFile));
app.MapGet("/index.html", (HttpContext ctx) => Page(ctx, indexFile));
app.MapGet("/admin", (HttpContext ctx) => Page(ctx, adminFile));
app.MapGet("/finance", (HttpContext ctx) => Page(ctx, financeFile));
//带斜杠的路径重定向
app.MapGet("/admin/{*rest}", () => Results.Redirect("/admin"));
app.MapGet("/finance/{*rest}", () => Results.Redirect("/finance"));

//统一 404
app.MapFallback(async context =>
{
    context.Response.StatusCode = 404;
    context.Response.ContentType = "application/json; charset=utf-8";
    await context.Response.WriteAsync("{\"ok\":false,\"code\":\"NOT_FOUND\",\"message\":\"未找到资源\"}");
});

//启动横幅
var baseUrl = cfg.ListenUrl.Replace("0.0.0.0", "127.0.0.1").Replace("+", "127.0.0.1");
Console.WriteLine("==============================================================");
Console.WriteLine(" 报销系统主服务器（AI Agent 安全执行与审批平台）");
Console.WriteLine("==============================================================");
Console.WriteLine($" 用户前端   : {baseUrl}/");
Console.WriteLine($" 财务工作台 : {baseUrl}/finance");
Console.WriteLine($" 管理后台   : {baseUrl}/admin");
Console.WriteLine($" 数据目录   : {cfg.DataRoot}");
Console.WriteLine($" AI 状态    : {(cfg.AiConfigured ? "已配置（" + cfg.Ai.Model + "），异步调用 + 物理隔离审核" : "未配置 → 本地意图解析 + 变更类操作全部转人工审批")}");
Console.WriteLine($" 账号权威   : users.json（本机 JSON）；飞书镜像 {(cfg.FeishuConfigured ? $"已启用（{cfg.Feishu.MirrorPasswordMode} 口令模式，失败静默）" : "未配置（跳过镜像，不影响账号功能）")}");
if (cfg.Feishu.TableUrl.Length > 0) Console.WriteLine($" 报销单原表 : {cfg.Feishu.TableUrl}");
if (cfg.Feishu.UserTableUrl.Length > 0) Console.WriteLine($" 用户表原表 : {cfg.Feishu.UserTableUrl}");
Console.WriteLine($" 审批通知   : {(cfg.Mail.Enabled ? "SMTP 已启用" : "SMTP 未启用 → 通知写入出站箱（后台可查看/撤回）")}");
Console.WriteLine("--------------------------------------------------------------");

//打印启动提示
foreach (var warning in cfg.StartupWarnings) Console.WriteLine($" [提示] {warning}");

if (seedNotes.Count > 0)
{
    Console.WriteLine(" [演示账号] 首次启动已播种以下账号，请在生产环境立即改密或删除：");
    foreach (var note in seedNotes) Console.WriteLine($"   - {note}");
}

if (bootstrapNote is not null)
{
    Console.WriteLine(" [初始超管] " + bootstrapNote);
}

Console.WriteLine("--------------------------------------------------------------");
Console.WriteLine(" 安全基线：JWT+请求签名(防重放) | WAF+蜜罐+自动封禁 | 规则引擎硬拦截");
Console.WriteLine("           四角色权限矩阵 | 账号权威在本机 JSON | 审计哈希链 | 审批与快照回退");
Console.WriteLine("==============================================================");

await audit.WriteAsync("system", "system", "system", "", "system.startup", "server", "SUCCESS",
    $"服务启动于 {cfg.ListenUrl}；AI={(cfg.AiConfigured ? "on" : "off")}；飞书镜像={(cfg.FeishuConfigured ? cfg.Feishu.MirrorPasswordMode : "off")}；数据目录={cfg.DataRoot}");

app.Run();
