using System.Text;
using System.Text.RegularExpressions;

namespace AiApproval.Core;

//可疑内容判定结果
public sealed class AnomalyResult
{
    public bool Hit => Category.Length > 0;

    //XSS / SQL注入 / 提示词注入 / 编码绕过
    public string Category { get; init; } = "";

    //命中的具体规则名
    public string Pattern { get; init; } = "";

    //命中处的片段（截断后，供管理员核看）
    public string Snippet { get; init; } = "";

    public static readonly AnomalyResult None = new();
}

/// <summary>
/// 报销单文本字段的「异常内容」判定。
///
/// 设计边界（很重要）：
/// - 只拦**正常员工绝不会写进事由的东西**：XSS、SQL 注入、提示词注入。
/// - **不拦业务填写错误**：金额超限、类型不在枚举、城市没填、事由太短——那些是正常的
///   "员工不懂事"，由 InvoicePolicy 的业务校验给出提示，不进拦截台账。
/// - 命中即**不写飞书**，落拦截台账（App_Data/intercepts.json）供管理员追溯。
/// </summary>
public static class InvoiceAnomaly
{
    private static readonly TimeSpan Timeout = TimeSpan.FromMilliseconds(50);

    //XSS
    private static readonly (string Pattern, Regex Rx)[] XssRules =
    {
        ("script 标签", new Regex(@"(?i)<\s*script", RegexOptions.Compiled, Timeout)),
        ("事件属性", new Regex(@"(?i)\bon(error|load|click|mouseover|focus|submit|mouseenter|animationstart)\s*=", RegexOptions.Compiled, Timeout)),
        ("javascript 协议", new Regex(@"(?i)(javascript\s*:|vbscript\s*:|data\s*:\s*text/html)", RegexOptions.Compiled, Timeout)),
        ("危险标签", new Regex(@"(?i)<\s*(iframe|svg|embed|object|base|meta|link)\b", RegexOptions.Compiled, Timeout)),
        ("DOM 危险调用", new Regex(@"(?i)(document\.cookie|document\.write|\.innerHTML\s*=|window\.location\s*=|eval\s*\()", RegexOptions.Compiled, Timeout)),
    };

    //SQL 注入
    private static readonly (string Pattern, Regex Rx)[] SqlRules =
    {
        ("布尔恒真", new Regex(@"(?i)(\bor\b\s+['""\d]\s*=\s*['""\d]|'\s*or\s+'|\)\s*or\s*\()", RegexOptions.Compiled, Timeout)),
        ("UNION 查询", new Regex(@"(?i)union[\s/**]+select", RegexOptions.Compiled, Timeout)),
        ("系统表探测", new Regex(@"(?i)(information_schema|sysobjects|xp_cmdshell|load_file\s*\(|into\s+outfile)", RegexOptions.Compiled, Timeout)),
        ("时间盲注", new Regex(@"(?i)(\bsleep\s*\(|\bbenchmark\s*\(|\bwaitfor\s+delay\b|pg_sleep\s*\()", RegexOptions.Compiled, Timeout)),
        ("堆叠语句", new Regex(@"(?i)(;\s*(drop|delete|update|insert)\s+)", RegexOptions.Compiled, Timeout)),
    };

    //提示词注入 / 越权话术
    private static readonly (string Pattern, Regex Rx)[] PromptRules =
    {
        ("要求忽略规则", new Regex(@"(?i)(忽略|无视|忘掉|忘记|跳过|绕过)[^。\n]{0,8}(规则|指令|提示|限制|审批|审核|安全策略)", RegexOptions.Compiled, Timeout)),
        ("英文越狱", new Regex(@"(?i)\b(ignore|disregard|forget|bypass)\b[^.\n]{0,20}\b(previous|above|prior|rule|instruction|prompt|approval)\b", RegexOptions.Compiled, Timeout)),
        ("索取系统提示", new Regex(@"(?i)(系统提示词?|system\s+prompt|开发者指令|你的规则)[^。\n]{0,10}(输出|显示|打印|告诉我|复述|泄露)", RegexOptions.Compiled, Timeout)),
        ("身份替换", new Regex(@"(?i)(你现在是|从现在起你是|假装你是|假设你是|you are now|act as|roleplay as)\s*\S{0,12}(管理员|root|admin|超级用户|系统)", RegexOptions.Compiled, Timeout)),
        ("要求免审/直批", new Regex(@"(?i)(直接执行|自动批准|立刻批准|免去审批|无需审批|approved\s*=\s*true|skip\s+approval)", RegexOptions.Compiled, Timeout)),
        ("提升权限", new Regex(@"(?i)(把我设为|给我|提升为|授予我)[^。\n]{0,8}(管理员|admin|root|超管|权限)", RegexOptions.Compiled, Timeout)),
        ("套取凭据", new Regex(@"(?i)(密码|口令|密钥|私钥|api\s*key|token|令牌)[^。\n]{0,10}(发|输出|告诉|给我|展示|打印)", RegexOptions.Compiled, Timeout)),
        ("标签伪造", new Regex(@"(?i)(<\s*/?\s*(system|assistant|action|user_input)\s*>|###\s*(system|instruction)|\[\s*system\s*\])", RegexOptions.Compiled, Timeout)),
    };

    //判定一段文本；category 由调用方给（事由 / 发票信息 / 审批意见）
    public static AnomalyResult Scan(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return AnomalyResult.None;

        var raw = text.Length > 2000 ? text[..2000] : text;
        var decoded = DecodeOnce(DecodeUnicode(raw));

        var hit = Match(decoded, "XSS", XssRules)
               ?? Match(decoded, "SQL注入", SqlRules)
               ?? Match(decoded, "提示词注入", PromptRules);

        return hit ?? AnomalyResult.None;
    }

    //同时扫描多个字段，返回第一个命中项（带字段名）
    public static (AnomalyResult Result, string Field) ScanFields(params (string Field, string? Text)[] fields)
    {
        foreach (var (field, text) in fields)
        {
            var r = Scan(text);
            if (r.Hit) return (r, field);
        }
        return (AnomalyResult.None, "");
    }

    private static AnomalyResult? Match(string text, string category, (string Pattern, Regex Rx)[] rules)
    {
        foreach (var (pattern, rx) in rules)
        {
            try
            {
                var m = rx.Match(text);
                if (!m.Success) continue;

                var start = Math.Max(0, m.Index - 20);
                var len = Math.Min(text.Length - start, Math.Max(60, m.Length + 40));
                return new AnomalyResult
                {
                    Category = category,
                    Pattern = pattern,
                    Snippet = Clean(text.Substring(start, len)),
                };
            }
            catch (RegexMatchTimeoutException)
            {
                return new AnomalyResult { Category = category, Pattern = pattern + "（匹配超时）", Snippet = "" };
            }
        }
        return null;
    }

    //片段清洗：去控制字符、压成一行、截断
    private static string Clean(string s)
    {
        var sb = new StringBuilder(Math.Min(s.Length, 120));
        foreach (var c in s)
        {
            if (sb.Length >= 120) break;
            if (char.IsControl(c) && c is not ('\n' or '\t')) continue;
            sb.Append(c is '\n' or '\t' ? ' ' : c);
        }
        return sb.ToString().Trim();
    }

    //还原 JSON 的 \uXXXX 转义（防编码绕过）
    private static string DecodeUnicode(string text)
    {
        if (text.IndexOf("\\u", StringComparison.Ordinal) < 0) return text;
        try
        {
            return Regex.Replace(text, @"\\u([0-9a-fA-F]{4})",
                m => ((char)Convert.ToInt32(m.Groups[1].Value, 16)).ToString(), RegexOptions.None, Timeout);
        }
        catch { return text; }
    }

    //解一层百分号编码（防编码绕过）
    private static string DecodeOnce(string value)
    {
        if (value.IndexOf('%') < 0) return value;
        try { return Uri.UnescapeDataString(value); }
        catch { return value; }
    }
}
