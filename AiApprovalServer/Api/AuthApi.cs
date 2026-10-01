using AiApproval.Ai;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Services;
using AiApproval.Tools;

namespace AiApproval.Api;

/// <summary>
/// 接入鉴权层：健康检查 / 注册 / 登录 / 自身信息 / 改密。
///
/// 反暴力破解组合拳（针对"爆破 admin 后台"这类攻击）：
///   * 每 IP 5 分钟 6 次登录尝试（超限 429）；
///   * 每账号连续 5 次失败锁定 15 分钟（持久化，重启进程也依然锁定）；
///   * 账号不存在时同样执行一次等价 PBKDF2 计算，消除用户名字典枚举的时间差；
///   * 错误文案统一，不泄露"这个用户名是否存在"；
///   * 所有失败写入审计；达阈值由 WAF 另行封禁 IP；
///   * 非管理员越权访问 /api/admin/* 一次记 3 分（几乎立刻触发封禁）。
/// </summary>
public static class AuthApi
{
    public static void Map(WebApplication app)
    {
        app.MapGet("/api/public/health", (AppConfig cfg, AiClient ai) => Api.Ok(new
        {
            status = "ok",
            serverTime = DateTime.UtcNow,
            aiConfigured = ai.IsConfigured,
            aiModel = ai.IsConfigured ? ai.Model : null,
            // off / degraded / online —— 只有"最近调用成功"才算 online，避免界面给出误导性状态
            aiStatus = ai.Status,
            aiLastSuccessAt = ai.LastSuccessAt,
            aiLastFailureAt = ai.LastFailureAt,
            aiLastError = ai.IsConfigured ? ai.LastError : "",
            mode = ai.Status switch
            {
                "online" => "AI 意图解析 + AI 安全审核（物理隔离）",
                "degraded" => "AI 已配置但调用失败 → 已降级为本地意图解析，变更类操作全部转人工审批",
                _ => "本地意图解析（AI 未配置）+ 审核不可用 → 变更类操作全部转人工审批",
            },
            selfRegistration = cfg.AllowSelfRegistration,
        }));

        app.MapPost("/api/auth/login", LoginAsync);
        app.MapPost("/api/auth/register", RegisterAsync);

        app.MapPost("/api/auth/logout", async (HttpContext context, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                "auth.logout", identity.User.UserName, "SUCCESS", "客户端主动退出");
            return Api.Ok(new { message = "已退出登录" });
        });

        app.MapGet("/api/auth/me", (HttpContext context) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            return Api.Ok(new
            {
                id = identity.User.Id,
                userName = identity.User.UserName,
                displayName = identity.User.DisplayName,
                role = identity.User.Role,
                roleLabel = Roles.Label(identity.User.Role),
                emailMasked = identity.User.EmailMasked,
                permissions = PermissionList(identity.User.Role),
                lastLoginAt = identity.User.LastLoginAt,
            });
        });

        app.MapPost("/api/auth/password", async (HttpContext context, UserService users, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            if (!guard.TryConsume($"password:{identity.User.Id}", 5, TimeSpan.FromMinutes(15)))
                return Api.Error(429, "RATE_LIMITED", "修改口令过于频繁，请稍后再试");

            var body = Api.ReadBody<ChangePasswordRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var error = UserService.ValidatePassword(body.NewPassword, identity.User.UserName);
            if (error is not null) return Api.Error(400, "WEAK_PASSWORD", error);

            var ok = await users.ChangePasswordAsync(identity.User.Id, body.NewPassword, identity.User.UserName, Api.Ip(context));
            if (!ok) return Api.Error(500, "FAILED", "口令修改失败");

            // 令牌世代 +1：旧令牌立即失效，必须重新登录
            return Api.Ok(new { message = "口令已更新，请使用新口令重新登录（旧令牌已全部失效）", relogin = true });
        });
    }

    private static List<object> PermissionList(string role)
        => ToolCatalog.PermissionsOf(role)
            .Select(p => (object)new { key = p, label = Permissions.Label(p) })
            .ToList();

    private static async Task<IResult> LoginAsync(HttpContext context, UserService users, JwtService jwt,
        AuditLog audit, SecurityGuard guard, AppConfig cfg)
    {
        var ip = Api.Ip(context);

        if (guard.IsBanned(ip, out _, out _))
            return Api.Error(403, "IP_BANNED", "请求被安全策略拒绝");

        if (!guard.TryConsume($"login:ip:{ip}", cfg.Limits.LoginPerFiveMinutesPerIp, TimeSpan.FromMinutes(5)))
        {
            guard.Strike(ip, "登录接口请求过频", weight: 1, loginFail: true);
            await audit.WriteAsync("", "", "", ip, "auth.rate-limit", "login", "BLOCKED", "登录频率超限");
            return Api.Error(429, "RATE_LIMITED", "登录尝试过于频繁，请稍后再试");
        }

        var body = Api.ReadBody<LoginRequest>(context);
        if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

        var userName = Api.Clean(body.UserName, 64);
        var password = body.Password ?? "";
        if (userName.Length == 0 || password.Length is 0 or > 128)
            return Api.Error(400, "BAD_REQUEST", "请输入用户名与口令");

        var result = await users.LoginAsync(userName, password, ip);

        if (!result.Success)
        {
            guard.Strike(ip, $"登录失败（{Api.Clean(userName, 32)}）", weight: 1, loginFail: true);

            if (result.Locked)
                await audit.WriteAsync("", userName, "", ip, "auth.lockout", userName, "LOCKED",
                    $"连续失败触发账号锁定至 {result.LockedUntil:HH:mm:ss}（UTC）");

            await Task.Delay(result.AccountNotFound ? 120 : 60);
            return Api.Error(401, "AUTH_FAILED", result.Error);
        }

        var user = result.User!;
        var token = jwt.Issue(user.Id, user.UserName, user.Role, user.TokenEpoch, out var signKey, out var expiresAt);

        await audit.WriteAsync(user.Id, user.UserName, user.Role, ip, "auth.login", user.UserName,
            "SUCCESS", $"角色={Roles.Label(user.Role)}");

        return Api.Ok(new
        {
            token,
            signKey,
            expiresAt,
            user = new
            {
                id = user.Id,
                userName = user.UserName,
                displayName = user.DisplayName,
                role = user.Role,
                roleLabel = Roles.Label(user.Role),
                emailMasked = user.EmailMasked,
                permissions = PermissionList(user.Role),
            },
            // 前端据此对写请求做 HMAC 签名（Anti-Replay 的前置条件）
            signing = new
            {
                algorithm = "HMAC-SHA256",
                canonical = "METHOD\\nPATH?QUERY\\nX-Timestamp\\nX-Nonce\\nSHA256_HEX(rawBody)",
                headers = new[] { "X-Timestamp", "X-Nonce", "X-Signature" },
                requiredFor = new[] { "POST", "PUT", "PATCH", "DELETE" },
            },
        });
    }

    private static async Task<IResult> RegisterAsync(HttpContext context, UserService users, AuditLog audit,
        SecurityGuard guard, AppConfig cfg)
    {
        var ip = Api.Ip(context);

        if (!cfg.AllowSelfRegistration)
            return Api.Error(403, "REGISTER_DISABLED", "本系统已关闭自助注册，请联系管理员开户");

        if (!guard.TryConsume($"register:ip:{ip}", cfg.SelfRegistrationPerHourPerIp, TimeSpan.FromHours(1)))
        {
            guard.Strike(ip, "注册接口请求过频", weight: 1, waf: true);
            return Api.Error(429, "RATE_LIMITED", "注册请求过于频繁");
        }

        var body = Api.ReadBody<RegisterRequest>(context);
        if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

        // 自助注册只能拿到最低角色：角色不由客户端决定（防"注册即管理员"）
        var (user, error) = await users.CreateAsync(
            Api.Clean(body.UserName, 32), body.Password ?? "", Api.Clean(body.DisplayName, 32),
            Roles.User, Api.Clean(body.Email, 128), "self-register");

        if (user is null)
        {
            await audit.WriteAsync("", Api.Clean(body.UserName, 32), "", ip, "auth.register",
                Api.Clean(body.UserName, 32), "FAIL", error);
            return Api.Error(400, "REGISTER_FAILED", error);
        }

        await audit.WriteAsync(user.Id, user.UserName, user.Role, ip, "auth.register", user.UserName,
            "SUCCESS", "自助注册（角色固定为“用户”）");

        return Api.Ok(new
        {
            message = "注册成功，请登录。自助注册只提供“用户”角色，员工与管理员由管理员在后台分配。",
            userName = user.UserName,
        });
    }
}

public sealed class ChangePasswordRequest
{
    public string NewPassword { get; set; } = "";
}
