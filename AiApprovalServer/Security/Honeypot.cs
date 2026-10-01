namespace AiApproval.Security;

/// <summary>
/// 蜜罐层。攻击者拿到一个域名后最典型的动作就是"扫后台"：
///   /.env、/admin.php、/wp-login.php、/phpmyadmin、/actuator/env、/.git/config ...
/// 正常用户永远不会请求这些路径，所以命中即是攻击信号。
///
/// 策略：命中即记分（权重高于普通 WAF 命中），累计到阈值自动封禁；
/// 并且对 /admin.php 提供一个"看起来像老后台"的诱饵登录页：
/// 任何提交动作立刻封禁该 IP 并写入审计（真实密码只会落到攻击者自己的手里）。
/// </summary>
public static class Honeypot
{
    public static readonly string[] DecoyPaths =
    {
        "/admin.php", "/admin/login.php", "/wp-login.php", "/wp-admin", "/wp-admin/",
        "/phpmyadmin", "/pma", "/.env", "/.env.local", "/.git/config", "/config.php",
        "/backup.sql", "/db.sql", "/dump.sql", "/administrator", "/manager/html",
        "/actuator/env", "/actuator/health", "/druid/index.html", "/console", "/jenkins",
        "/api/v1/admin/users", "/api/v1/admin/login", "/swagger/index.html", "/server-status",
        "/owa/auth/logon.aspx", "/hudson", "/solr/admin", "/.svn/entries", "/web.config",
    };

    public static bool IsDecoy(string? path, out string label)
    {
        var normalized = (path ?? "").TrimEnd('/');
        if (normalized.Length == 0) normalized = "/";

        foreach (var decoy in DecoyPaths)
        {
            var d = decoy.TrimEnd('/');
            if (string.Equals(normalized, d, StringComparison.OrdinalIgnoreCase))
            {
                label = decoy;
                return true;
            }
        }

        // 目录型探测：/wp-admin/xxx、/.git/xxx
        foreach (var prefix in new[] { "/wp-admin/", "/.git/", "/.svn/", "/phpmyadmin/" })
        {
            if ((path ?? "").StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                label = prefix + "*";
                return true;
            }
        }

        label = "";
        return false;
    }

    /// <summary>诱饵页：刻意做成"上一个时代"的后台样式，不含任何真实接口。</summary>
    public static string DecoyConsolePage() => """
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="robots" content="noindex, nofollow">
        <title>运维控制台 V3.2 登录</title>
        <style>
          body{font-family:Consolas,monospace;background:#0b1020;color:#c9d3e0;margin:0;padding:40px}
          .box{max-width:420px;margin:60px auto;border:1px solid #24314f;padding:28px;background:#0f1629}
          h1{font-size:18px;margin:0 0 18px;color:#8fb6ff;font-weight:600}
          label{display:block;font-size:12px;margin:12px 0 4px;color:#7f8ea8}
          input{width:100%;padding:8px;background:#0b1020;border:1px solid #24314f;color:#dfe7f3;box-sizing:border-box}
          button{margin-top:18px;width:100%;padding:9px;background:#1d4ed8;border:0;color:#fff;cursor:pointer}
          .tip{font-size:11px;color:#5b6a83;margin-top:14px;line-height:1.6}
        </style>
        </head>
        <body>
        <div class="box">
          <h1>运维控制台 V3.2</h1>
          <form method="post" action="/admin.php">
            <label>账号</label><input name="user" autocomplete="off">
            <label>口令</label><input name="pass" type="password" autocomplete="off">
            <button type="submit">登录</button>
          </form>
          <p class="tip">仅限内网运维使用。<br>建议使用 Chrome 内核浏览器访问。</p>
        </div>
        </body>
        </html>
        """;

    /// <summary>robots.txt 本身就是诱饵：合法的搜索引擎遵守，扫描器则会去抓取 Disallow 里的路径。</summary>
    public static string RobotsTxt() => """
        User-agent: *
        Disallow: /admin
        Disallow: /admin.php
        Disallow: /api/admin/
        Disallow: /backup.sql
        Disallow: /.env
        Disallow: /App_Data/
        """;
}
