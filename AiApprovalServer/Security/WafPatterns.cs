using System.Text.RegularExpressions;

namespace AiApproval.Security;

//请求级 WAF 特征库
public static class WafPatterns
{
    private static readonly TimeSpan Timeout = TimeSpan.FromMilliseconds(50);

    //规则表，匹配超时按攻击处理
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

    //扫描字符串，命中返回标签
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

    //扫描路径、查询串与关键头
    public static string? ScanRequestLine(HttpContext context)    {
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

    //只解一层百分号编码
    private static string DecodeOnce(string value)
    {
        try { return Uri.UnescapeDataString(value); }
        catch { return value; }
    }

    //请求体特征库：比请求行更保守，只拦"在本系统任何正常业务里都不该出现"的形态
    //注意：XSS / 命令注入 / 模板注入 不在此列——写文件接口会合法写入脚本内容，
    //那由 ContentRiskScanner + 强制人工审批处理，若在此拦截会导致正常业务无法提交
    private static readonly (string Label, Regex Rx)[] BodyRules =
    {
        ("SQL注入", new Regex(
            @"(?i)(union[\s/**]+select|\bor\b\s+['""\d]\s*=\s*['""\d]|'\s*or\s+'|\)\s*or\s*\(|\bsleep\s*\(|\bbenchmark\s*\(|information_schema|xp_cmdshell|load_file\s*\(|into\s+outfile|\bwaitfor\s+delay\b|;\s*drop\s+table|;\s*delete\s+from)",
            RegexOptions.Compiled, Timeout)),
        ("JNDI注入", new Regex(@"(?i)(\$\{jndi:|\$\{lower:|\$\{env:|\$\{sys:)", RegexOptions.Compiled, Timeout)),
        ("SSRF特征", new Regex(
            @"(?i)(gopher://|dict://|file:///|169\.254\.169\.254|metadata\.google\.internal|metadata\.azure)",
            RegexOptions.Compiled, Timeout)),
        ("协议走私", new Regex(@"(?i)(%0d%0a|%0a%0d|\r\n\s*(set-cookie|location)\s*:)", RegexOptions.Compiled, Timeout)),
        ("路径穿越", new Regex(
            @"(?i)(/etc/passwd|/proc/self/|boot\.ini|win\.ini|%2e%2e%2f|%252e%252e|\.\./\.\./|\.\.\\\.\.\\)",
            RegexOptions.Compiled, Timeout)),
    };

    //扫描请求体：先还原 \\uXXXX 与一层 %XX，堵住编码绕过
    public static string? ScanBody(string? text)
    {
        if (string.IsNullOrEmpty(text)) return null;

        var sample = text.Length > 262144 ? text[..262144] : text;
        var decoded = DecodeOnce(DecodeUnicode(sample));

        foreach (var (label, rx) in BodyRules)
        {
            try
            {
                if (rx.IsMatch(decoded)) return label;
            }
            catch (RegexMatchTimeoutException)
            {
                return label + "（正则匹配超时，疑似 ReDoS 探测）";
            }
        }

        return null;
    }

    //还原 JSON 里的 \uXXXX 转义（攻击者常用它绕过字面特征）
    private static string DecodeUnicode(string text)
    {
        if (text.IndexOf("\\u", StringComparison.Ordinal) < 0) return text;
        try
        {
            return Regex.Replace(text, @"\\u([0-9a-fA-F]{4})",
                m => ((char)Convert.ToInt32(m.Groups[1].Value, 16)).ToString(),
                RegexOptions.None, Timeout);
        }
        catch
        {
            return text;
        }
    }
}

