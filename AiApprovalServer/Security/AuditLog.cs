using System.Text;
using AiApproval.Core;

namespace AiApproval.Security;

/// <summary>
/// 审计日志：JSONL 追加写 + 逐条哈希链。
///
/// 为什么用哈希链：运维/攻击者可以删改文件里的某一行，但无法在不破坏后续所有 Hash 的情况下
/// 伪造历史。后台"审计链校验"按钮会重新计算全链并定位第一个断链位置。
///
/// 多进程安全：每次写之前都会**重新读一次文件尾部**校准 (Seq, 上一个 Hash)，并且追加时对文件加独占锁。
/// 否则两个实例各自持有内存里的链头时写同一个文件，会写出重复序号、把链写断
/// （这是实测踩到的坑：审计校验报 "序号断裂：期望 42，实际 41"）。
/// </summary>
public sealed class AuditLog
{
    private readonly JsonStore _store;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly string _path;
    private string _lastHash = new string('0', 64);
    private long _seq;
    private bool _initialized;
    private long _knownLength = -1;

    public AuditLog(JsonStore store)
    {
        _store = store;
        _path = store.PathOf(StoreNames.Audit);
    }

    private async Task EnsureInitializedAsync()
    {
        if (_initialized) return;
        await RefreshTailAsync(force: true).ConfigureAwait(false);
        _initialized = true;
    }

    /// <summary>从文件尾部校准链头（文件长度没变就跳过，避免每条都读全文件）。</summary>
    private async Task RefreshTailAsync(bool force)
    {
        try
        {
            if (!File.Exists(_path))
            {
                if (force || _knownLength != 0) { _seq = 0; _lastHash = new string('0', 64); _knownLength = 0; }
                return;
            }

            var length = new FileInfo(_path).Length;
            if (!force && length == _knownLength) return;

            var lines = await _store.ReadLinesAsync(StoreNames.Audit).ConfigureAwait(false);
            if (lines.Count == 0)
            {
                _seq = 0;
                _lastHash = new string('0', 64);
            }
            else
            {
                var last = AppJson.Deserialize<AuditEntry>(lines[^1]);
                if (last is not null)
                {
                    _seq = last.Seq;
                    _lastHash = last.Hash;
                }
            }

            _knownLength = length;
        }
        catch
        {
            // 校准失败不阻断写入：宁可写出一条可能断链的记录，也不要让审计整体失效
        }
    }

    public async Task<AuditEntry> WriteAsync(AuditEntry entry)
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            await EnsureInitializedAsync().ConfigureAwait(false);
            await RefreshTailAsync(force: false).ConfigureAwait(false);

            entry.Seq = ++_seq;
            entry.PrevHash = _lastHash;
            entry.Hash = ComputeHash(entry);

            var line = AppJson.Serialize(entry);
            await AppendExclusiveAsync(line).ConfigureAwait(false);

            _lastHash = entry.Hash;
            try { _knownLength = new FileInfo(_path).Length; } catch { _knownLength = -1; }

            return entry;
        }
        finally { _gate.Release(); }
    }

    /// <summary>独占追加：跨进程串行化，避免多实例同时写导致序号重复与断链。</summary>
    private async Task AppendExclusiveAsync(string line)
    {
        var directory = Path.GetDirectoryName(_path);
        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);

        for (var attempt = 1; attempt <= 20; attempt++)
        {
            try
            {
                using var fs = new FileStream(_path, FileMode.Append, FileAccess.Write, FileShare.None, 4096, useAsync: true);
                using var sw = new StreamWriter(fs, new System.Text.UTF8Encoding(false));
                await sw.WriteLineAsync(line).ConfigureAwait(false);
                await sw.FlushAsync().ConfigureAwait(false);
                return;
            }
            catch (IOException) when (attempt < 20)
            {
                // 另一个实例正在写：退避重试
                await Task.Delay(30 * attempt).ConfigureAwait(false);
            }
        }
    }

    public Task<AuditEntry> WriteAsync(string actorId, string actorName, string actorRole, string ip,
        string action, string target, string outcome, string detail = "")
        => WriteAsync(new AuditEntry
        {
            ActorId = actorId,
            ActorName = actorName,
            ActorRole = actorRole,
            Ip = ip,
            Action = action,
            Target = target,
            Outcome = outcome,
            Detail = Truncate(detail, 800),
        });

    public async Task<List<AuditEntry>> ReadAsync(int limit = 200, int offset = 0, string? actionFilter = null, string? actorFilter = null)
    {
        var lines = await _store.ReadLinesAsync(StoreNames.Audit).ConfigureAwait(false);
        var entries = new List<AuditEntry>(lines.Count);
        foreach (var line in lines)
        {
            var e = AppJson.Deserialize<AuditEntry>(line);
            if (e is not null) entries.Add(e);
        }

        IEnumerable<AuditEntry> query = entries.OrderByDescending(e => e.Seq);
        if (!string.IsNullOrWhiteSpace(actionFilter))
            query = query.Where(e => e.Action.Contains(actionFilter, StringComparison.OrdinalIgnoreCase));
        if (!string.IsNullOrWhiteSpace(actorFilter))
            query = query.Where(e => e.ActorName.Contains(actorFilter, StringComparison.OrdinalIgnoreCase));

        return query.Skip(Math.Max(0, offset)).Take(Math.Clamp(limit, 1, 500)).ToList();
    }

    public async Task<int> CountAsync()
    {
        var lines = await _store.ReadLinesAsync(StoreNames.Audit).ConfigureAwait(false);
        return lines.Count;
    }

    /// <summary>全链校验：返回是否完整、检查条数与首个断链位置。</summary>
    public async Task<(bool Ok, int Checked, long BrokenAt, string Message)> VerifyChainAsync()
    {
        var lines = await _store.ReadLinesAsync(StoreNames.Audit).ConfigureAwait(false);
        var prev = new string('0', 64);
        long expectedSeq = 0;

        foreach (var line in lines)
        {
            var e = AppJson.Deserialize<AuditEntry>(line);
            if (e is null)
                return (false, 0, expectedSeq, $"第 {expectedSeq + 1} 行不是合法 JSON，审计文件已损坏");

            expectedSeq++;
            if (e.Seq != expectedSeq)
                return (false, (int)(expectedSeq - 1), e.Seq, $"序号断裂：期望 {expectedSeq}，实际 {e.Seq}（有记录被删除或插入）");

            if (!Crypto.FixedEquals(e.PrevHash, prev))
                return (false, (int)(expectedSeq - 1), e.Seq, $"第 {e.Seq} 条的前序哈希不匹配（历史被改写）");

            var recomputed = ComputeHash(e);
            if (!Crypto.FixedEquals(recomputed, e.Hash))
                return (false, (int)(expectedSeq - 1), e.Seq, $"第 {e.Seq} 条内容哈希不匹配（该条被篡改）");

            prev = e.Hash;
        }

        return (true, lines.Count, 0, $"审计链完整，共校验 {lines.Count} 条记录");
    }

    /// <summary>链哈希计算：字段顺序固定、显式分隔，避免不同字段拼接产生歧义。</summary>
    private static string ComputeHash(AuditEntry e)
    {
        var sb = new StringBuilder();
        sb.Append(e.Seq).Append('\u0001')
          .Append(e.Time.ToUniversalTime().Ticks).Append('\u0001')
          .Append(e.ActorId).Append('\u0001')
          .Append(e.ActorName).Append('\u0001')
          .Append(e.ActorRole).Append('\u0001')
          .Append(e.Ip).Append('\u0001')
          .Append(e.Action).Append('\u0001')
          .Append(e.Target).Append('\u0001')
          .Append(e.Outcome).Append('\u0001')
          .Append(e.Detail).Append('\u0001')
          .Append(e.PrevHash);
        return Crypto.Sha256Hex(sb.ToString());
    }

    private static string Truncate(string value, int max)
        => string.IsNullOrEmpty(value) ? "" : value.Length <= max ? value : value[..max];
}
