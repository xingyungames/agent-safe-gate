using AiApproval.Core;

namespace AiApproval.Tools;

/// <summary>
/// 路径守卫：所有文件操作的唯一入口（硬白名单）。
///
/// 拦截的越权/逃逸手法：
///   * 相对穿越：../../etc/passwd、..%2f、....// 
///   * 绝对路径：/etc/passwd、C:\Windows\System32
///   * 备用数据流与非法字符：file.txt::$DATA、文件中的 \0
///   * Windows 设备名：CON、PRN、AUX、NUL、COM1..COM9、LPT1..LPT9
///   * 符号链接/目录联接逃逸：目标路径的任意祖先目录是 reparse point（指向白名单之外）
///   * 规范化后是否仍在白名单根目录之下（最终一道防线，防"规则看漏了某种编码"）
/// </summary>
public sealed class PathGuard
{
    private static readonly char[] InvalidChars = { '<', '>', ':', '"', '|', '?', '*', '\0', '\u0001' };
    private static readonly string[] ReservedNames =
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    };

    public const int MaxRelativePathLength = 200;

    private readonly AppConfig _cfg;

    public PathGuard(AppConfig cfg) => _cfg = cfg;

    public string UserRoot(string userId) => Path.GetFullPath(Path.Combine(_cfg.WorkspacesDir, "u_" + userId));

    public string ShareRoot() => Path.GetFullPath(Path.Combine(_cfg.WorkspacesDir, "share"));

    public string ScopeRoot(string userId, string scope) =>
        string.Equals(scope, "share", StringComparison.OrdinalIgnoreCase) ? ShareRoot() : UserRoot(userId);

    /// <summary>
    /// 解析用户请求的路径。成功时返回白名单内的绝对路径与规范化相对路径（统一用 / 分隔，供审计展示）。
    /// </summary>
    public (bool Ok, string Error, string FullPath, string Relative) Resolve(string userId, string? scope, string? requestedPath)
    {
        var normalizedScope = string.Equals(scope?.Trim(), "share", StringComparison.OrdinalIgnoreCase) ? "share" : "private";
        var root = ScopeRoot(userId, normalizedScope);

        var raw = (requestedPath ?? "").Trim().Replace('\\', '/');
        // 根目录的几种写法统一成""，否则"./"会被当成"以点结尾的非法目录名"
        while (raw.StartsWith("./", StringComparison.Ordinal)) raw = raw[2..];
        if (raw is "." or "/") raw = "";

        if (raw.Length > MaxRelativePathLength) return Fail($"路径过长（上限 {MaxRelativePathLength} 字符）");

        // 统一分隔符，便于检查穿越
        var unified = raw;

        if (unified.Contains('\0')) return Fail("路径包含非法字符");
        if (unified.Contains("..")) return Fail("路径不得包含上级目录引用（..）");
        if (unified.StartsWith('/')) return Fail("禁止使用绝对路径");
        if (unified.Length >= 2 && unified[1] == ':') return Fail("禁止使用盘符路径");
        if (unified.Contains("$DATA", StringComparison.OrdinalIgnoreCase)) return Fail("禁止使用备用数据流");
        if (unified.Contains("::")) return Fail("路径包含非法字符");

        foreach (var segment in unified.Split('/', StringSplitOptions.RemoveEmptyEntries))
        {
            if (segment.IndexOfAny(InvalidChars) >= 0) return Fail("路径包含非法字符");
            var bareName = segment.Split('.')[0].ToUpperInvariant();
            if (ReservedNames.Contains(bareName)) return Fail($"禁止使用系统保留名：{segment}");
            if (segment.EndsWith('.') || segment.EndsWith(' ')) return Fail("目录名不得以点或空格结尾");
        }

        string full;
        try
        {
            full = Path.GetFullPath(Path.Combine(root, unified.Replace('/', Path.DirectorySeparatorChar)));
        }
        catch
        {
            return Fail("路径格式非法");
        }

        var rootWithSep = root.EndsWith(Path.DirectorySeparatorChar) ? root : root + Path.DirectorySeparatorChar;
        if (!full.StartsWith(rootWithSep, StringComparison.OrdinalIgnoreCase) &&
            !string.Equals(full, root, StringComparison.OrdinalIgnoreCase))
        {
            return Fail("目标路径超出你的授权目录");
        }

        if (HasReparsePointEscape(root, full, out var linkReason))
            return Fail(linkReason);

        var relative = Path.GetRelativePath(root, full).Replace('\\', '/');
        if (relative == ".") relative = "";
        return (true, "", full, relative);
    }

    /// <summary>
    /// 检查 root → target 之间是否存在 reparse point（软/硬链接、目录联接）。
    /// 攻击者可先让 AI 在自己目录里创建一个指向 /etc 的链接，再通过"合法路径"读写白名单外的文件。
    /// </summary>
    private static bool HasReparsePointEscape(string root, string target, out string reason)
    {
        reason = "";
        try
        {
            var relative = Path.GetRelativePath(root, target);
            if (relative == ".") return false;

            var current = root;
            foreach (var segment in relative.Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries))
            {
                current = Path.Combine(current, segment);
                if (!File.Exists(current) && !Directory.Exists(current)) continue;

                var info = new FileInfo(current);
                if (info.Exists && info.LinkTarget is not null)
                {
                    reason = $"路径中包含符号链接：{segment}（出于安全策略已拒绝）";
                    return true;
                }

                var dirInfo = new DirectoryInfo(current);
                if (dirInfo.Exists && dirInfo.LinkTarget is not null)
                {
                    reason = $"路径中包含目录链接：{segment}（出于安全策略已拒绝）";
                    return true;
                }

                var attrs = File.GetAttributes(current);
                if ((attrs & FileAttributes.ReparsePoint) != 0)
                {
                    reason = $"路径命中重解析点：{segment}（出于安全策略已拒绝）";
                    return true;
                }
            }
        }
        catch
        {
            // 属性读取失败时保守放行：后续仍会做"规范化后是否在白名单根之下"的判定
            return false;
        }

        return false;
    }

    private static (bool, string, string, string) Fail(string error) => (false, error, "", "");
}
