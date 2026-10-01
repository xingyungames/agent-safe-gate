using System.Text;

namespace AiApproval.Tools;

/// <summary>
/// 行级差异（LCS 动态规划），输出类似 Git 的 +/- 变更，用于后台"这到底改了什么"的可视化。
///
/// 复杂度保护：DP 规模上限 600×600，超出则退化为"整体替换"摘要——
/// 宁可给一个粗粒度结论，也不让一次超大文件对比打爆服务端 CPU/内存。
/// </summary>
public static class LineDiff
{
    private const int MaxDpLines = 600;
    private const int MaxOutputLines = 400;

    public static (string Text, int Added, int Removed) Compute(string? oldText, string? newText, int context = 3)
    {
        var oldLines = SplitLines(oldText);
        var newLines = SplitLines(newText);

        if (oldLines.Length > MaxDpLines || newLines.Length > MaxDpLines)
        {
            return (
                $"文件过大，未做逐行对比。旧文件 {oldLines.Length} 行 / 新文件 {newLines.Length} 行，" +
                "已完整保存旧版本快照，可一键回退。",
                0, 0);
        }

        var n = oldLines.Length;
        var m = newLines.Length;
        var lcs = new int[n + 1, m + 1];

        for (var i = n - 1; i >= 0; i--)
        {
            for (var j = m - 1; j >= 0; j--)
            {
                lcs[i, j] = string.Equals(oldLines[i], newLines[j], StringComparison.Ordinal)
                    ? lcs[i + 1, j + 1] + 1
                    : Math.Max(lcs[i + 1, j], lcs[i, j + 1]);
            }
        }

        // 回溯得到操作序列
        var ops = new List<(char Kind, string Text)>();
        int x = 0, y = 0;
        while (x < n && y < m)
        {
            if (string.Equals(oldLines[x], newLines[y], StringComparison.Ordinal))
            {
                ops.Add((' ', oldLines[x]));
                x++; y++;
            }
            else if (lcs[x + 1, y] >= lcs[x, y + 1])
            {
                ops.Add(('-', oldLines[x]));
                x++;
            }
            else
            {
                ops.Add(('+', newLines[y]));
                y++;
            }
        }
        while (x < n) ops.Add(('-', oldLines[x++]));
        while (y < m) ops.Add(('+', newLines[y++]));

        // 只保留有变更的区块（上下各 context 行）
        var keep = new bool[ops.Count];
        for (var i = 0; i < ops.Count; i++)
        {
            if (ops[i].Kind == ' ') continue;
            for (var k = Math.Max(0, i - context); k <= Math.Min(ops.Count - 1, i + context); k++)
                keep[k] = true;
        }

        var sb = new StringBuilder();
        var added = ops.Count(o => o.Kind == '+');
        var removed = ops.Count(o => o.Kind == '-');
        var emitted = 0;
        var lastKept = -2;

        for (var i = 0; i < ops.Count; i++)
        {
            if (!keep[i]) continue;

            if (i != lastKept + 1) sb.Append("@@\n");
            lastKept = i;

            sb.Append(ops[i].Kind).Append(ops[i].Text).Append('\n');

            if (++emitted >= MaxOutputLines)
            {
                sb.Append("... 变更过长，已截断展示（快照中保留完整内容）\n");
                break;
            }
        }

        if (sb.Length == 0)
            sb.Append("（内容无变化）\n");

        return (sb.ToString(), added, removed);
    }

    private static string[] SplitLines(string? text)
    {
        if (string.IsNullOrEmpty(text)) return Array.Empty<string>();
        return text.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
    }
}
