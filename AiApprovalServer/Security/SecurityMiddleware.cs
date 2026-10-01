using System.Text;
using AiApproval.Core;
using AiApproval.Services;

namespace AiApproval.Security;

/// <summary>请求身份上下文（由中间件解析后放入 HttpContext.Items）。</summary>
public sealed class RequestIdentity
{
    public required UserRecord User { get; init; }
    public string Ip { get; init; } = "";
    /// <summary>请求签名密钥（登录时随令牌下发；状态变更请求必须用它签名）</summary>
    public string SignKey { get; init; } = "";
    public string Token { get; init; } = "";

    public bool IsAdmin => User.Role == Roles.Admin;
    public bool IsStaffOrAbove => User.Role is Roles.Staff or Roles.Admin;
}

/// <summary>
/// 安全中间件：把"能挡在业务代码之前的事"全部挡在业务代码之前。
/// 处理顺序（顺序本身就是防线设计）：
///   1) 安全响应头（无论如何都要带上）
///   2) IP 封禁检查
///   3) 蜜罐路径探测
///   4) WAF 特征扫描（路径/查询串/关键头）
///   5) 请求体缓冲（带硬上限，防内存打爆）
///   6) 跨站来源校验（Origin/Referer）
///   7) 令牌校验 + 请求签名 / 时间戳 / 一次性随机数（Anti-Replay）
///   8) 未捕获异常兜底：只回统一错误，绝不回栈信息
/// </summary>
public sealed class SecurityMiddleware
{
    public const string IdentityKey = "ai_identity";
    public const string RawBodyKey = "ai_raw_body";

    private static readonly HashSet<string> PublicApi = new(StringComparer.OrdinalIgnoreCase)
    {
        "/api/public/health",
        "/api/auth/login",
        "/api/auth/register",
    };

    private readonly RequestDelegate _next;
    private readonly AppConfig _cfg;
    private readonly SecurityGuard _guard;
    private readonly AuditLog _audit;
    private readonly UserService _users;
    private readonly JwtService _jwt;
    private readonly ILogger<SecurityMiddleware> _logger;

    public SecurityMiddleware(RequestDelegate next, AppConfig cfg, SecurityGuard guard, AuditLog audit,
        UserService users, JwtService jwt, ILogger<SecurityMiddleware> logger)
    {
        _next = next;
        _cfg = cfg;
        _guard = guard;
        _audit = audit;
        _users = users;
        _jwt = jwt;
        _logger = logger;
    }

    public async Task InvokeAsync(HttpContext context)
    {
        var ip = SecurityGuard.ClientIp(context);
        AddSecurityHeaders(context);

        var path = context.Request.Path.Value ?? "/";

        if (_guard.IsBanned(ip, out var bannedUntil, out var banReason))
        {
            if (_guard.TryConsume($"banlog:{ip}", 3, TimeSpan.FromMinutes(1)))
                await _audit.WriteAsync("", "", "", ip, "security.blocked", Truncate(path, 120), "BANNED",
                    $"{banReason}；解封时间 {bannedUntil:HH:mm:ss}（UTC）").ConfigureAwait(false);

            await WriteJsonAsync(context, 403, new { error = "请求被安全策略拒绝", code = "IP_BANNED" }).ConfigureAwait(false);
            return;
        }

        // 蜜罐：正常用户不会访问这些路径
        if (Honeypot.IsDecoy(path, out var decoyLabel))
        {
            await HandleDecoyAsync(context, ip, path, decoyLabel).ConfigureAwait(false);
            return;
        }

        if (path.Equals("/robots.txt", StringComparison.OrdinalIgnoreCase))
        {
            // 读 robots.txt 多为扫描器行为：记录但不拦截
            await _audit.WriteAsync("", "", "", ip, "recon.robots", "/robots.txt", "OBSERVED", "读取 robots.txt");
            context.Response.ContentType = "text/plain; charset=utf-8";
            await context.Response.WriteAsync(Honeypot.RobotsTxt()).ConfigureAwait(false);
            return;
        }

        var wafHit = WafPatterns.ScanRequestLine(context);
        if (wafHit is not null)
        {
            var banned = _guard.Strike(ip, $"WAF 命中 {wafHit}", weight: 2, waf: true);
            await _audit.WriteAsync("", "", "", ip, "waf.block", Truncate(path + context.Request.QueryString.Value, 200),
                banned ? "BANNED" : "BLOCKED", $"命中特征：{wafHit}").ConfigureAwait(false);
            await WriteJsonAsync(context, 400, new { error = "请求包含非法字符", code = "WAF_BLOCKED" }).ConfigureAwait(false);
            return;
        }

        try
        {
            var body = await BufferBodyAsync(context).ConfigureAwait(false);

            if (IsStateChanging(context) && !IsSameOrigin(context))
            {
                _guard.Strike(ip, "跨站来源请求", weight: 1, waf: true);
                await _audit.WriteAsync("", "", "", ip, "csrf.block", Truncate(path, 200), "BLOCKED",
                    $"Origin={context.Request.Headers.Origin}").ConfigureAwait(false);
                await WriteJsonAsync(context, 403, new { error = "请求来源不被信任", code = "BAD_ORIGIN" }).ConfigureAwait(false);
                return;
            }

            var token = ReadBearer(context);
            RequestIdentity? identity = null;

            if (!string.IsNullOrEmpty(token))
            {
                var payload = _jwt.Validate(token, out var failure);
                if (payload is null)
                {
                    _guard.NoteSignatureRejected();
                    if (_guard.TryConsume($"tokenlog:{ip}", 5, TimeSpan.FromMinutes(1)))
                        await _audit.WriteAsync("", "", "", ip, "auth.token", Truncate(path, 120), "FAIL", failure).ConfigureAwait(false);

                    _guard.Strike(ip, $"令牌校验失败：{failure}", weight: 1, waf: true);
                    await WriteJsonAsync(context, 401, new { error = "登录状态无效，请重新登录", code = "BAD_TOKEN" }).ConfigureAwait(false);
                    return;
                }

                var user = await _users.FindByIdAsync(payload.sub).ConfigureAwait(false);
                if (user is null || user.Disabled || user.TokenEpoch != payload.epo)
                {
                    await _audit.WriteAsync(payload.sub, payload.name, payload.role, ip, "auth.token", Truncate(path, 120),
                        "FAIL", user is null ? "账号不存在" : user.Disabled ? "账号已禁用" : "令牌世代过期（口令或角色已变更）").ConfigureAwait(false);
                    await WriteJsonAsync(context, 401, new { error = "登录状态已失效，请重新登录", code = "TOKEN_STALE" }).ConfigureAwait(false);
                    return;
                }

                identity = new RequestIdentity { User = user, Ip = ip, SignKey = payload.sk, Token = token };
                context.Items[IdentityKey] = identity;

                if (IsStateChanging(context))
                {
                    var (ok, reason) = VerifySignature(context, body, payload.sk, user.Id);
                    if (!ok)
                    {
                        _guard.NoteSignatureRejected();
                        var banned = _guard.Strike(ip, $"请求签名校验失败：{reason}", weight: 2, waf: true);
                        await _audit.WriteAsync(user.Id, user.UserName, user.Role, ip, "request.signature",
                            Truncate(path, 120), banned ? "BANNED" : "FAIL", reason).ConfigureAwait(false);
                        await WriteJsonAsync(context, 401, new { error = "请求签名校验失败", code = "BAD_SIGNATURE" }).ConfigureAwait(false);
                        return;
                    }
                }
            }

            if (path.StartsWith("/api/", StringComparison.OrdinalIgnoreCase) && !PublicApi.Contains(path))
            {
                if (identity is null)
                {
                    await WriteJsonAsync(context, 401, new { error = "请先登录", code = "UNAUTHENTICATED" }).ConfigureAwait(false);
                    return;
                }

                // 越权访问后台接口：非管理员访问 /api/admin/* 直接拒绝并记分（含"爆破后台"信号）
                if (path.StartsWith("/api/admin/", StringComparison.OrdinalIgnoreCase) && !identity.IsAdmin)
                {
                    var banned = _guard.Strike(ip, $"{identity.User.UserName} 越权访问管理接口", weight: 3, waf: true);
                    await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, ip,
                        "authz.admin", Truncate(path, 200), banned ? "BANNED" : "DENIED",
                        "非管理员访问管理接口").ConfigureAwait(false);
                    await WriteJsonAsync(context, 403, new { error = "无权访问该接口", code = "FORBIDDEN" }).ConfigureAwait(false);
                    return;
                }
            }

            await _next(context).ConfigureAwait(false);
        }
        catch (BadHttpRequestException ex)
        {
            await WriteJsonAsync(context, 413, new { error = "请求体过大或被拒绝", code = "PAYLOAD_REJECTED" }).ConfigureAwait(false);
            _logger.LogWarning("请求被拒绝：{Message}", ex.Message);
        }
        catch (OperationCanceledException)
        {
            // 客户端断开：不写错误响应
        }
        catch (Exception ex)
        {
            // 只把细节写进服务端日志/审计，响应体保持"无信息量"
            _logger.LogError(ex, "未处理异常 {Path}", path);
            await _audit.WriteAsync("", "", "", ip, "system.error", Truncate(path, 200), "ERROR",
                ex.GetType().Name + ": " + Truncate(ex.Message, 300)).ConfigureAwait(false);

            if (!context.Response.HasStarted)
                await WriteJsonAsync(context, 500, new { error = "服务内部错误，请联系管理员", code = "INTERNAL" }).ConfigureAwait(false);
        }
    }

    // ------------------------------------------------------------------ 各步骤实现

    private async Task HandleDecoyAsync(HttpContext context, string ip, string path, string decoyLabel)
    {
        var isPost = HttpMethods.IsPost(context.Request.Method);

        if (isPost)
        {
            // 往诱饵后台提交凭据 = 明确的攻击行为：直接封禁并留证
            _guard.Strike(ip, $"蜜罐后台提交凭据（{decoyLabel}）", weight: 3, honeypot: true);
            _guard.Ban(ip, "向蜜罐管理后台提交凭据", _cfg.Limits.BanMinutes);
            await _audit.WriteAsync("", "", "", ip, "honeypot.credential", Truncate(path, 120), "BANNED",
                "攻击者向诱饵后台提交了凭据（凭据未落库，仅记录行为）").ConfigureAwait(false);
            await WriteJsonAsync(context, 403, new { error = "请求被安全策略拒绝", code = "IP_BANNED" }).ConfigureAwait(false);
            return;
        }

        var banned = _guard.Strike(ip, $"蜜罐路径探测（{decoyLabel}）", weight: 1, honeypot: true);
        await _audit.WriteAsync("", "", "", ip, "honeypot.probe", Truncate(path, 120),
            banned ? "BANNED" : "OBSERVED", $"扫描器/攻击者探测诱饵路径 {decoyLabel}").ConfigureAwait(false);

        if (string.Equals(path.TrimEnd('/'), "/admin.php", StringComparison.OrdinalIgnoreCase))
        {
            context.Response.ContentType = "text/html; charset=utf-8";
            await context.Response.WriteAsync(Honeypot.DecoyConsolePage()).ConfigureAwait(false);
            return;
        }

        // 其它诱饵路径与普通 404 表现一致，不给攻击者任何"这里被特殊处理了"的反馈
        await WriteJsonAsync(context, 404, new { error = "未找到资源", code = "NOT_FOUND" }).ConfigureAwait(false);
    }

    private async Task<byte[]?> BufferBodyAsync(HttpContext context)
    {
        if (!IsStateChanging(context)) return null;

        if (context.Request.ContentLength is { } len && len > _cfg.MaxRequestBodyBytes)
            throw new BadHttpRequestException($"请求体超限：{len}");

        context.Request.EnableBuffering();
        using var ms = new MemoryStream();
        var buffer = new byte[8192];
        var total = 0;

        while (true)
        {
            var read = await context.Request.Body.ReadAsync(buffer).ConfigureAwait(false);
            if (read <= 0) break;
            total += read;
            if (total > _cfg.MaxRequestBodyBytes)
                throw new BadHttpRequestException($"请求体超限：>{_cfg.MaxRequestBodyBytes}");
            ms.Write(buffer, 0, read);
        }

        var bytes = ms.ToArray();
        // 回填给后续处理器（业务代码照常读 body）
        context.Request.Body = new MemoryStream(bytes);
        context.Request.ContentLength = bytes.Length;
        context.Items[RawBodyKey] = bytes;
        return bytes;
    }

    private (bool Ok, string Reason) VerifySignature(HttpContext context, byte[]? body, string signKey, string userId)
    {
        if (string.IsNullOrEmpty(signKey)) return (false, "令牌缺少签名密钥，请重新登录");

        var timestampRaw = context.Request.Headers["X-Timestamp"].ToString();
        var nonce = context.Request.Headers["X-Nonce"].ToString();
        var signature = context.Request.Headers["X-Signature"].ToString();

        if (string.IsNullOrWhiteSpace(timestampRaw) || string.IsNullOrWhiteSpace(nonce) || string.IsNullOrWhiteSpace(signature))
            return (false, "缺少 X-Timestamp / X-Nonce / X-Signature 请求头");

        if (!long.TryParse(timestampRaw, out var timestamp))
            return (false, "时间戳格式非法");

        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        if (Math.Abs(nowMs - timestamp) > _cfg.ClockSkewSeconds * 1000L)
            return (false, $"时间戳超出 {_cfg.ClockSkewSeconds} 秒有效期");

        var bodyHash = Crypto.Sha256Hex(body ?? Array.Empty<byte>());
        var canonical = string.Join('\n',
            context.Request.Method.ToUpperInvariant(),
            context.Request.Path.Value + context.Request.QueryString.Value,
            timestampRaw,
            nonce,
            bodyHash);

        var expected = Crypto.HmacB64(canonical, Encoding.UTF8.GetBytes(signKey));
        if (!Crypto.FixedEquals(expected, signature))
            return (false, "签名不匹配（请求被篡改或密钥不同）");

        if (!_guard.TryUseNonce(userId, nonce, out var nonceReason))
            return (false, nonceReason);

        return (true, "");
    }

    private static bool IsStateChanging(HttpContext context)
        => HttpMethods.IsPost(context.Request.Method)
           || HttpMethods.IsPut(context.Request.Method)
           || HttpMethods.IsPatch(context.Request.Method)
           || HttpMethods.IsDelete(context.Request.Method);

    /// <summary>
    /// 跨站来源校验：浏览器发起的跨站写请求必然带 Origin。
    /// 判定顺序：① `Security:TrustedOrigins` 里显式白名单 → 放行（反向代理终止 TLS 的场景）
    ///           ② 与"协议+主机+端口"严格同源 → 放行
    ///           ③ 其余一律拒绝（CSRF 双保险，主保险是"不用 Cookie 认证"）。
    /// 白名单为空时行为与之前完全一致：只有严格同源才放行。
    /// </summary>
    private bool IsSameOrigin(HttpContext context)
    {
        var origin = context.Request.Headers.Origin.ToString();
        if (string.IsNullOrWhiteSpace(origin)) return true; // 非浏览器客户端（curl/脚本），由签名机制负责

        if (!Uri.TryCreate(origin, UriKind.Absolute, out var uri)) return false;

        var actual = uri.GetLeftPart(UriPartial.Authority);
        if (_cfg.TrustedOrigins.Any(t => string.Equals(t, actual, StringComparison.OrdinalIgnoreCase)))
            return true;

        var expected = $"{context.Request.Scheme}://{context.Request.Host}";
        return string.Equals(actual, expected, StringComparison.OrdinalIgnoreCase);
    }

    private static string ReadBearer(HttpContext context)
    {
        var raw = context.Request.Headers.Authorization.ToString();
        if (string.IsNullOrWhiteSpace(raw)) return "";
        const string prefix = "Bearer ";
        return raw.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) ? raw[prefix.Length..].Trim() : "";
    }

    private void AddSecurityHeaders(HttpContext context)
    {
        var headers = context.Response.Headers;
        headers["X-Content-Type-Options"] = "nosniff";
        headers["X-Frame-Options"] = "DENY";
        headers["Referrer-Policy"] = "no-referrer";
        headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=(), usb=(), magnetometer=()";
        headers["Cross-Origin-Opener-Policy"] = "same-origin";
        headers["Cross-Origin-Resource-Policy"] = "same-origin";
        headers["X-XSS-Protection"] = "0"; // 旧浏览器 XSS Auditor 自身存在信息泄露问题，显式关闭
        headers["Content-Security-Policy"] =
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
            "connect-src 'self'; font-src 'self'; media-src 'none'; object-src 'none'; frame-ancestors 'none'; " +
            "base-uri 'none'; form-action 'self'";
        headers.Remove("Server");

        if (context.Request.IsHttps)
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains";
    }

    private static async Task WriteJsonAsync(HttpContext context, int status, object payload)
    {
        if (context.Response.HasStarted) return;
        context.Response.Clear();
        context.Response.StatusCode = status;
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.Headers.CacheControl = "no-store";
        await context.Response.WriteAsync(AppJson.Serialize(payload)).ConfigureAwait(false);
    }

    private static string Truncate(string value, int max)
        => string.IsNullOrEmpty(value) ? "" : value.Length <= max ? value : value[..max];
}
