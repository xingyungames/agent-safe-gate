using System.Text.RegularExpressions;

namespace AiApproval.Ai;

/// <summary>
/// 输入侧注入检测。
///
/// 设计取舍：**检测到注入特征不等于直接拒绝**。
/// 直接字符串拦截有两个问题：① 误杀（用户可能只是在讨论"忽略以上指令"这句话本身）；
/// ② 掩盖真实风险（真正危险的模型不会用明显的关键词）。
/// 因此这里的结论只有一个用途：**把状态变更类操作强制升级为人工审批**，并写进审计与审批上下文。
/// 也就是说，注入检测提高的是"人类看一眼"的概率，而不是替代防线。
/// </summary>
public static class InjectionScanner
{
    private static readonly (string Label, Regex Rx)[] Rules =
    {
        ("要求忽略既有规则", new Regex(@"(?i)(忽略|无视|忘掉|取消|关闭)(以上|上面|之前|前面|所有|全部)?(的)?(规则|指令|提示|限制|安全策略|防护)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("英文越狱指令", new Regex(@"(?i)\b(ignore|disregard|forget)\b[^.\n]{0,20}\b(previous|above|prior|earlier|all)\b[^.\n]{0,20}\b(instruction|rule|prompt|restriction)s?\b", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("索取系统提示词", new Regex(@"(?i)(输出|显示|打印|告诉我|重复|复述|泄露)[^。\n]{0,12}(系统提示|提示词|prompt|system message|开发者指令|你的规则)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("身份替换尝试", new Regex(@"(?i)(你现在是|从现在起你是|扮演|假装你是|假设你是|you are now|act as|roleplay as)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("越狱关键词", new Regex(@"(?i)(开发者模式|上帝模式|越狱模式|jailbreak|\bDAN\b|do anything now|无限制模式)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("要求跳过审批", new Regex(@"(?i)(跳过|绕过|免去|取消|无需|不需|不用)[^。\n]{0,8}(审批|审核|确认|复核)|(直接执行|自动批准|立刻批准|帮我批准|approved\s*=\s*true)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("权限提升尝试", new Regex(@"(?i)(我(就)?是|把我设为|给我|提升为|授予我)[^。\n]{0,8}(管理员|admin|root|超级用户|权限)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("凭据套取", new Regex(@"(?i)(密码|口令|密钥|私钥|api\s*key|token|令牌)[^。\n]{0,10}(发|输出|告诉|给我|展示|打印)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("标签伪造", new Regex(@"(?i)(</?\s*(user_input|system|assistant|action)\s*>|###\s*(system|instruction)|\[\s*system\s*\])", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("编码绕过特征", new Regex(@"(?i)(base64|rot13|十六进制|unicode\s*escape)[^。\n]{0,12}(解码|decoded?|执行|execute)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
        ("数据外发诱导", new Regex(@"(?i)(把|将)[^。\n]{0,10}(数据|名单|客户|合同|备份)[^。\n]{0,10}(发到|上传到|发送到|导出到)[^。\n]{0,20}(邮箱|外网|网盘|外部|http)", RegexOptions.Compiled, TimeSpan.FromMilliseconds(50))),
    };

    public static List<string> Scan(string? text)
    {
        var flags = new List<string>();
        if (string.IsNullOrWhiteSpace(text)) return flags;

        var sample = text.Length > 6000 ? text[..6000] : text;

        foreach (var (label, rx) in Rules)
        {
            try
            {
                if (rx.IsMatch(sample)) flags.Add(label);
            }
            catch (RegexMatchTimeoutException)
            {
                flags.Add(label + "（匹配超时）");
            }
        }

        return flags;
    }
}
