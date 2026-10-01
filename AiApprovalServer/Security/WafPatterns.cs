using System.Text.RegularExpressions;

namespace AiApproval.Security;

/// <summary>
/// 请求级 WAF 特征库（只针对"路径 / 查询串 / 关键请求头"，不扫描 JSON 正文）。
///
/// 为什么正文不拦：正文里装的是用户想写入文件或交给 AI 的自然语言内容，
/// 出现 &lt;script&gt;、../ 属于正常业务数据。这类风险由"输出编码 + PathGuard + 规则引擎"消除，
/// 而不是用字符串黑名单误杀（黑名单拦截反倒会掩盖真正的注入问题）。
///
/// 所有正则都设了 50ms 匹配超时：命中超时按"攻击"处理（ReDoS 探测本身也是攻击特征）。
/// </summary>
public static class WafPatterns
{
    private static readonly TimeSpan Timeout = TimeSpan.FromMilliseconds(50);

    private static readonly (string Label, Regex Rx)[] Rules =
    {
        ("SQL注入", new Regex(@"(?i)(union[\s/**]+select|\bor\b\s+['""\d]\s*=\s*['""\d]|\bsleep\s*\(|\bbenchmark\s*\(|information_schema|xp_cmdshell|load_file\s*\(|into\s+outfile|\bwaitfor\s+delay\b)", RegexOptions.Compiled, Timeout)),
        ("XSS", new Regex(@"(?i)(<\s*script|javascript\s*:|vbscript\s*:|on(error|load|click|mouseover|focus|submit)\s*=|\bexpression\s*\(|<\s*iframe|<\s*svg|<\s*img[^>]{0,40}onerror|document\.cookie|<\s*embed|<\s*object)", RegexOptions.Compiled, Timeout)),
        ("路径穿越", new Regex(@"(?i)(\.\./|\.\.\\|%2e%2e|%252e%252e|\.\.%2f|%c0%ae|/etc/passwd|/proc/self/|boot\.ini|win\.ini)", RegexOptions.Compiled, Timeout)),
        ("命令注入", new Regex(@"(?i)([;|&]\s*(cat|ls|whoami|id|rm|curl|wget|nc|ncat|powershell|cmd|bash|sh)\b|\$\(|\|\s*(bash|sh|cmd|powershell)\b|&&\s*(rm|del|format)\b)", RegexOptions.Compiled, Timeout)),
        ("JNDI注入", new Regex(@"(?i)(\$\{jndi:|\$\{lower:|\$\{env:|\$\{sys:|%24%7bjndi)", RegexOptions.Compiled, Timeout)),
        ("SSRF特征", new Regex(@"(?i)(gopher://|dict://|file:///|169\.254\.169\.254|metadata\.google\.internal|metadata\.azure)", RegexOptions.Compiled, Timeout)),
        ("协议走私", new Regex(@"(?i)(%0d%0a|\r\n\s*(set-cookie|location):|transfer-encoding\s*:\s*chunked.*content-length)", RegexOptions.Compiled, Timeout)),
        ("模板注入", new Regex(@"(?i)(\{\{\s*\d+\s*\*\s*\d+\s*\}\}|\{php\}<\?php|<%=\s*\d+\s*\+)", RegexOptions.Compiled, Timeout)),
    };

    /// <summary>扫描单个字符串；命中返回特征标签，未命中返回 null。</summary>
    public static string? Scan(string? text)
    {
        if (string.IsNullOrEmpty(text)) return null;

        foreach (var (label, rx) in Rules)
        {
            try
            {
                if (rx.IsMatch(text)) return label;
            }
            catch (RegexMatchTimeoutException)
            {
                return label + "（正则匹配超时，疑似 ReDoS 探测）";
            }
        }

        return null;
    }

    /// <summary>扫描请求行：路径 + 原始查询串 + 关键请求头。</summary>
    public static string? ScanRequestLine(HttpContext context)
    {
        var path = context.Request.Path.Value ?? "";
        if (path.Contains('\0')) return "空字节注入";

        var hit = Scan(path);
        if (hit is not null) return hit;

        var rawQuery = context.Request.QueryString.Value;
        if (!string.IsNullOrEmpty(rawQuery) && rawQuery.Length <= 4096)
        {
            hit = Scan(DecodeOnce(rawQuery));
            if (hit is not null) return hit;
        }

        foreach (var header in new[] { "User-Agent", "Referer", "X-Forwarded-For", "Cookie", "Origin", "Accept-Language" })
        {
            var value = context.Request.Headers[header].ToString();
            if (string.IsNullOrEmpty(value)) continue;

            hit = Scan(value.Length > 2048 ? value[..2048] : value);
            if (hit is not null) return $"{header} 头：{hit}";
        }

        return null;
    }

    /// <summary>只解一层百分号编码：避免"双重编码绕过"与"过度解码把正常内容当成攻击"两头踩坑。</summary>
    private static string DecodeOnce(string value)
    {
        try { return Uri.UnescapeDataString(value); }
        catch { return value; }
    }
}
