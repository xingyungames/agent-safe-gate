using System.Text;
using AiApproval.Core;
using AiApproval.Security;

namespace AiApproval.Tools;

/// <summary>
/// 版本库：执行前的"旧状态"快照 + 差异(Diff) + 反向操作数据(Undo Log)。
///
/// 快照存储采用内容寻址（blob 文件名 = SHA-256），同一内容只存一份：
///   * 相同文件被反复覆盖不会把磁盘写爆；
///   * 顺手获得完整性校验能力（读出后可重新算摘要比对，发现文件被篡改）。
/// </summary>
public sealed class VersionStore
{
    private readonly JsonStore _store;
    private readonly AppConfig _cfg;
    private readonly SemaphoreSlim _lock = new(1, 1);

    public VersionStore(JsonStore store, AppConfig cfg)
    {
        _store = store;
        _cfg = cfg;
    }

    /// <summary>
    /// 记录一次"可回退的执行"。
    /// previousBytes 为 null 表示目标原本不存在（回退 = 删除）；newBytes 为 null 表示被删除（回退 = 恢复）。
    /// </summary>
    public async Task<VersionRecord> RecordAsync(string taskId, string userId, string userName, string kind,
        string operation, string target, byte[]? previousBytes, byte[]? newBytes,
        string undoLogJson, string compensateAction = "")
    {
        var record = new VersionRecord
        {
            Id = "v_" + Crypto.RandomHex(8),
            TaskId = taskId,
            UserId = userId,
            UserName = userName,
            Kind = kind,
            Operation = operation,
            Target = target,
            PreviousExisted = previousBytes is not null,
            PreviousSize = previousBytes?.Length ?? 0,
            NewSize = newBytes?.Length ?? 0,
            UndoLogJson = undoLogJson,
            CompensateAction = compensateAction,
            CreatedAt = DateTime.UtcNow,
        };

        if (previousBytes is not null)
        {
            record.PreviousHash = Crypto.Sha256Hex(previousBytes);
            record.PreviousBlob = record.PreviousHash + ".bin";
            await WriteBlobIfMissingAsync(record.PreviousBlob, previousBytes).ConfigureAwait(false);
        }

        if (newBytes is not null)
            record.NewHash = Crypto.Sha256Hex(newBytes);

        var oldText = AsText(previousBytes);
        var newText = AsText(newBytes);
        if (oldText is null || newText is null)
        {
            record.DiffText = previousBytes is null && newBytes is null
                ? "（无内容变化）"
                : $"二进制或非 UTF-8 内容，未生成逐行对比。旧 {record.PreviousSize} 字节 → 新 {record.NewSize} 字节。";
        }
        else
        {
            var (diff, added, removed) = LineDiff.Compute(oldText, newText);
            record.DiffText = diff;
            record.AddedLines = added;
            record.RemovedLines = removed;
        }

        await _store.MutateAsync<VersionRecord, bool>(StoreNames.Versions, list =>
        {
            list.Add(record);
            return true;
        }).ConfigureAwait(false);

        return record;
    }

    private async Task WriteBlobIfMissingAsync(string blobName, byte[] content)
    {
        var relative = StoreNames.Blobs + "/" + blobName;
        var existing = await _store.ReadBlobAsync(relative).ConfigureAwait(false);
        if (existing is not null) return;

        await _lock.WaitAsync().ConfigureAwait(false);
        try
        {
            existing = await _store.ReadBlobAsync(relative).ConfigureAwait(false);
            if (existing is null)
                await _store.WriteBlobAsync(relative, content).ConfigureAwait(false);
        }
        finally { _lock.Release(); }
    }

    public async Task<byte[]?> ReadBlobAsync(string? blobName)
    {
        if (string.IsNullOrWhiteSpace(blobName)) return null;
        if (blobName.Contains("..") || blobName.Contains('/') || blobName.Contains('\\')) return null;
        return await _store.ReadBlobAsync(StoreNames.Blobs + "/" + blobName).ConfigureAwait(false);
    }

    public async Task<List<VersionRecord>> ListAsync(string? userId = null, int limit = 100)
    {
        var all = await _store.ReadListAsync<VersionRecord>(StoreNames.Versions).ConfigureAwait(false);
        IEnumerable<VersionRecord> query = all.OrderByDescending(v => v.CreatedAt);
        if (!string.IsNullOrWhiteSpace(userId))
            query = query.Where(v => v.UserId == userId);
        return query.Take(Math.Clamp(limit, 1, 500)).ToList();
    }

    public async Task<VersionRecord?> GetAsync(string id)
    {
        var all = await _store.ReadListAsync<VersionRecord>(StoreNames.Versions).ConfigureAwait(false);
        return all.FirstOrDefault(v => v.Id == id);
    }

    public async Task<bool> UpdateAsync(string id, Action<VersionRecord> mutate)
        => await _store.MutateAsync<VersionRecord, bool>(StoreNames.Versions, list =>
        {
            var target = list.FirstOrDefault(v => v.Id == id);
            if (target is null) return false;
            mutate(target);
            return true;
        }).ConfigureAwait(false);

    public async Task<int> CountAsync()
        => (await _store.ReadListAsync<VersionRecord>(StoreNames.Versions).ConfigureAwait(false)).Count;

    /// <summary>
    /// 尝试按 UTF-8 解码；包含 NUL 字节（典型的二进制特征）时返回 null。
    /// 注意：bytes 为 null 表示"该文件原本不存在"（新建操作），此时应按**空文本**处理——
    /// 否则新建出来的纯文本文件会被误判成"二进制"，Diff 就没了（实测踩到的 bug）。
    /// </summary>
    private static string? AsText(byte[]? bytes)
    {
        if (bytes is null) return "";
        if (bytes.Length == 0) return "";
        if (bytes.Contains((byte)0)) return null;

        try
        {
            return new UTF8Encoding(false, throwOnInvalidBytes: true).GetString(bytes);
        }
        catch
        {
            return null;
        }
    }
}
