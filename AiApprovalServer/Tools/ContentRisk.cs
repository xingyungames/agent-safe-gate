using System.Text.RegularExpressions;

namespace AiApproval.Tools;

/// <summary>内容风险扫描的命中结果</summary>
public sealed class ContentRiskResult
{
    public bool HasRisk => Hits.Count > 0;
    /// <summary>命中标签（中文，直接给审批人看）</summary>
    public List<string> Hits { get; init; } = new();
    /// <summary>是否需要人工审批（脚本/外链/内网/编码载荷等一律要人看一眼）</summary>
    public bool RequiresApproval => Hits.Count > 0;
    public string Summary => Hits.Count == 0 ? "" : string.Join("；", Hits);
}

/// <summary>可执行性判定结果（后缀判断 ∪ 内容识别）</summary>
public sealed class ExecutableVerdict
{
    public bool IsExecutable => Evidences.Count > 0;
    /// <summary>判定依据（中文，写进审批上下文，让审批人看到"为什么认为是可执行"）</summary>
    public List<string> Evidences { get; init; } = new();
    /// <summary>判定通道：suffix / content / both，便于排查与审计</summary>
    public string Channel { get; init; } = "";
    public string Summary => Evidences.Count == 0 ? "" : string.Join("；", Evidences);
}

/// <summary>
/// 片段级内容风险扫描（"片段审批"的硬防线）。
///
/// 背景：沙箱（个人工作区）允许低风险写入自动执行，**但"写入"本身是最高频的投毒载体**——
/// 落地一个 .bat/.ps1 脚本、往里塞 PowerShell 下载执行、写内网地址或外链钓鱼、用编码把载荷藏起来，
/// 都是典型手法。光靠模型自觉拒绝是不行的（换模型、被注入说服就失守），所以这里用确定性规则扫描。
///
/// 三件事：
///   1) <see cref="Scan"/>：内容风险扫描（下载执行、内网/元数据地址、外链、编码载荷、注入话术…）→ 命中即强制人工审批；
///   2) <see cref="ScanPath"/>：文件名后缀风险扫描；
///   3) <see cref="DetectExecutable"/>：**可执行性判定 = 后缀判断 ∪ 内容识别**（PE/ELF/Mach-O 魔数、shebang、
///      批处理/PowerShell 语法、PHP/ASP 标签、base64 解码执行等）。判定为可执行 → 一律强制人工审批，永不自动执行。
///
/// 设计原则：**不做"一刀切拒绝"**——可执行内容也允许写，但必须经人确认；只有"文本工具确实承载不了"的
/// 真二进制内容（含 NUL 字节）才给出能力边界说明。
/// </summary>
public static class ContentRiskScanner
{
    private static readonly TimeSpan Timeout = TimeSpan.FromMilliseconds(60);

    /// <summary>可执行/脚本类后缀：写入这类文件一律转人工审批</summary>
    public static readonly string[] ScriptExtensions =
    {
        "bat", "cmd", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta",
        "sh", "bash", "zsh", "py", "pl", "rb", "jar", "reg", "lnk", "scr", "com", "pif",
    };

    /// <summary>二进制可执行后缀（文本工具写不了真二进制，但允许写"文本形式"的同名文件，只是必须人工审批）</summary>
    public static readonly string[] BinaryExtensions =
    {
        "exe", "dll", "msi", "sys", "drv", "ocx", "so", "dylib", "bin", "iso", "img", "apk", "app",
    };

    /// <summary>内容层面的可执行特征（识别"改了后缀的脚本"或"没后缀的脚本"）</summary>
    private static readonly (string Label, Regex Rx)[] ExecutableContentRules =
    {
        ("内容以 shebang 开头（#! 脚本）", new Regex(@"^\s*#!\s*/(usr/)?bin/(env\s+)?[\w\-/ ]+", RegexOptions.Compiled, Timeout)),
        ("内容含文件魔数（PE/ELF/Mach-O 可执行文件头）", new Regex(@"^MZ|\x7FELF|\xCA\xFE\xBA\xBE|\xCF\xFA\xED\xFE", RegexOptions.Compiled, Timeout)),
        ("内容为批处理脚本（@echo off / goto / setlocal / %~dp0）", new Regex(@"(?i)(@echo\s+off|goto\s*:|setlocal|%~dp0|for\s+/f\s+.*\s+in\s*\()", RegexOptions.Compiled, Timeout)),
        ("内容为 PowerShell 脚本（param( / cmdlet 绑定 / Write-Host / $env:）", new Regex(@"(?i)(^\s*param\s*\(|\[Parameter\s*\(|Write-Host|\$env:[A-Za-z_]+|Get-ChildItem|\bSet-ExecutionPolicy\b)", RegexOptions.Compiled, Timeout)),
        ("内容为 Web 脚本标签（PHP / ASP / JSP）", new Regex(@"(?i)(<\?php|<%@\s*page|<%[=@]|<\s*jsp:)", RegexOptions.Compiled, Timeout)),
        ("内容为 Bash/Shell 脚本（export VAR= / function / && 串联命令）", new Regex(@"(?i)(^\s*(export|source|alias)\s+\w+=|^\s*\w+\s*\(\)\s*\{|chmod\s+\+x|apt-get\s+install|yum\s+install)", RegexOptions.Compiled, Timeout)),
    };

    /// <summary>
    /// 可执行性判定：**后缀判断 ∪ 内容识别**，两者命中任一即为"可执行/脚本"。
    /// 这是"不搞一刀切"的关键——同一个 .txt 里写着 PowerShell 下载执行，也会被认出来。
    /// </summary>
    public static ExecutableVerdict DetectExecutable(string? relativePath, string? content)
    {
        var evidences = new List<string>();
        var bySuffix = false;
        var byContent = false;

        var ext = Path.GetExtension(relativePath ?? "").TrimStart('.').ToLowerInvariant();
        if (ext.Length > 0)
        {
            if (ScriptExtensions.Contains(ext))
            {
                evidences.Add($"后缀 .{ext}（脚本/可执行类）");
                bySuffix = true;
            }
            else if (BinaryExtensions.Contains(ext))
            {
                evidences.Add($"后缀 .{ext}（二进制可执行类）");
                bySuffix = true;
            }
        }

        var sample = content is { Length: > 0 }
            ? (content.Length > 20000 ? content[..20000] : content)
            : "";

        if (sample.Length > 0)
        {
            foreach (var (label, rx) in ExecutableContentRules)
            {
                try
                {
                    if (rx.IsMatch(sample)) { evidences.Add(label); byContent = true; }
                }
                catch (RegexMatchTimeoutException)
                {
                    evidences.Add(label + "（匹配超时）");
                    byContent = true;
                }
            }
        }

        return new ExecutableVerdict
        {
            Evidences = evidences,
            Channel = bySuffix && byContent ? "both" : bySuffix ? "suffix" : byContent ? "content" : "",
        };
    }

    /// <summary>
    /// 文本工具能否承载这段内容：出现 NUL 字节说明是**真二进制**（不是"文本形式的脚本"）。
    /// 这不是策略拒绝，而是工具能力边界——所以会给使用者明确的替代建议。
    /// </summary>
    public static bool LooksLikeRealBinary(string? content)
        => !string.IsNullOrEmpty(content) && content.Contains('\0');

    private static readonly (string Label, Regex Rx)[] Rules =
    {
        ("下载执行脚本", new Regex(@"(?i)(invoke-webrequest|invoke-expression|\biex\b|downloadstring|downloadfile|start-bitstransfer|urlmon|winhttp|certutil\s+-urlcache|bitsadmin)", RegexOptions.Compiled, Timeout)),
        ("命令执行/反弹", new Regex(@"(?i)(powershell(\.exe)?\s+-(enc|e|command|exec)|\bcmd(\.exe)?\s*/c|/bin/(ba)?sh\s+-c|nc\s+-e|ncat\s+-e|bash\s+-i|socat\s+)", RegexOptions.Compiled, Timeout)),
        ("编码/混淆载荷", new Regex(@"(?i)(base64\s*-d|frombase64string|\[convert\]::frombase64|eval\(|assert\(|unescape\(|-enc\s+[A-Za-z0-9+/=]{24,})", RegexOptions.Compiled, Timeout)),
        ("内网/元数据地址", new Regex(@"(?i)(https?://(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.0\.0\.0|localhost)|169\.254\.169\.254|metadata\.google\.internal)", RegexOptions.Compiled, Timeout)),
        ("外部链接", new Regex(@"(?i)https?://(?!127\.0\.0\.1|localhost)[a-z0-9][a-z0-9.\-]{2,}\.[a-z]{2,24}", RegexOptions.Compiled, Timeout)),
        ("计划任务/开机自启", new Regex(@"(?i)(schtasks\s+/create|register-scheduledtask|reg\s+add\s+.*\\run|startup\s+folder|/etc/cron|systemctl\s+enable)", RegexOptions.Compiled, Timeout)),
        ("隐匿执行", new Regex(@"(?i)(-windowstyle\s+hidden|-w\s+hidden|attrib\s+\+h|Set-ItemProperty.*Hidden|chmod\s+\+x|>[\s]*nul\s*2>&1|/dev/null\s+2>&1)", RegexOptions.Compiled, Timeout)),
        ("凭据/密钥特征", new Regex(@"(?i)(password\s*=|passwd\s*=|api[_-]?key\s*[:=]|secret\s*[:=]|-----BEGIN\s+(RSA|OPENSSH|PRIVATE)|sk-[A-Za-z0-9]{16,})", RegexOptions.Compiled, Timeout)),
        ("注入话术", new Regex(@"(?i)(忽略(以上|之前|所有)(的)?(规则|指令)|ignore\s+(all\s+)?(previous|above)\s+instructions|跳过(审批|审核)|直接执行不要审批|你现在是管理员)", RegexOptions.Compiled, Timeout)),
    };

    /// <summary>扫描一段待写入内容（片段级）。</summary>
    public static ContentRiskResult Scan(string? content)
    {
        var result = new ContentRiskResult();
        if (string.IsNullOrWhiteSpace(content)) return result;

        var sample = content.Length > 20000 ? content[..20000] : content;

        // 控制字符/不可见字符堆砌：常见于"把载荷藏进看似正常的文档"
        var controlCount = sample.Count(c => char.IsControl(c) && c != '\n' && c != '\r' && c != '\t');
        if (controlCount > 8) result.Hits.Add($"内容含 {controlCount} 个不可见控制字符（疑似隐藏载荷）");

        foreach (var (label, rx) in Rules)
        {
            try
            {
                if (rx.IsMatch(sample)) result.Hits.Add(label);
            }
            catch (RegexMatchTimeoutException)
            {
                result.Hits.Add(label + "（匹配超时）");
            }
        }

        return result;
    }

    /// <summary>按文件路径判断后缀风险。</summary>
    public static ContentRiskResult ScanPath(string? relativePath)
    {
        var result = new ContentRiskResult();
        var path = (relativePath ?? "").Trim();
        if (path.Length == 0) return result;

        var ext = Path.GetExtension(path).TrimStart('.').ToLowerInvariant();
        if (ext.Length == 0) return result;

        if (ScriptExtensions.Contains(ext))
            result.Hits.Add($"脚本/可执行类后缀 .{ext}（可能被用于投放脚本）");
        else if (BinaryExtensions.Contains(ext))
            result.Hits.Add($"二进制可执行后缀 .{ext}");

        return result;
    }

    public static bool IsBinaryExtension(string? relativePath)
    {
        var ext = Path.GetExtension(relativePath ?? "").TrimStart('.').ToLowerInvariant();
        return ext.Length > 0 && BinaryExtensions.Contains(ext);
    }
}
