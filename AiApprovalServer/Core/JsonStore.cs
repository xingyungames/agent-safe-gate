using System.Collections.Concurrent;
using System.Text;

namespace AiApproval.Core;

/// <summary>
/// 用 JSON 文件顶替数据库（无法改数据库时的过渡方案）。
///
/// 关键点（文件存储的经典坑都在这里堵上）：
///   * 读-改-写全程持锁：按"文件"粒度 <see cref="SemaphoreSlim"/>，避免并发写互相覆盖；
///   * 原子落盘：写临时文件 → File.Move(overwrite) → 原文件要么是旧的要么是新的，不会出现半截 JSON；
///   * 落盘前先备份 .bak，损坏时可人工恢复；
///   * 读取失败（JSON 损坏）不静默返回空集合，而是抛出，避免"数据看起来被清空"再被写回。
/// </summary>
public sealed class JsonStore
{
    private readonly string _root;
    private readonly ConcurrentDictionary<string, SemaphoreSlim> _gates = new(StringComparer.OrdinalIgnoreCase);

    public JsonStore(string root)
    {
        _root = Path.GetFullPath(root);
        Directory.CreateDirectory(_root);
    }

    public string Root => _root;

    public string PathOf(string name) => Path.Combine(_root, name);

    private SemaphoreSlim Gate(string name) => _gates.GetOrAdd(name, _ => new SemaphoreSlim(1, 1));

    /// <summary>只读访问（仍走锁，避免读到写入中途；原子替换已经保证一致性，这里只是省心）。</summary>
    public async Task<TResult> ReadAsync<T, TResult>(string name, Func<List<T>, TResult> work)
    {
        var gate = Gate(name);
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            var list = await LoadListAsync<T>(name).ConfigureAwait(false);
            return work(list);
        }
        finally { gate.Release(); }
    }

    public async Task<List<T>> ReadListAsync<T>(string name)
        => await ReadAsync<T, List<T>>(name, list => list).ConfigureAwait(false);

    /// <summary>读-改-写事务。persist=false 时只改内存副本（用于纯查询型统计）。</summary>
    public async Task<TResult> MutateAsync<T, TResult>(string name, Func<List<T>, Task<TResult>> work, bool persist = true)
    {
        var gate = Gate(name);
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            var list = await LoadListAsync<T>(name).ConfigureAwait(false);
            var result = await work(list).ConfigureAwait(false);
            if (persist) await WriteAtomicAsync(name, list).ConfigureAwait(false);
            return result;
        }
        finally { gate.Release(); }
    }

    public Task<TResult> MutateAsync<T, TResult>(string name, Func<List<T>, TResult> work, bool persist = true)
        => MutateAsync<T, TResult>(name, list => Task.FromResult(work(list)), persist);

    public async Task AppendAsync<T>(string name, T item)
        => await MutateAsync<T, bool>(name, list => { list.Add(item); return true; }).ConfigureAwait(false);

    public async Task<List<T>> LoadListAsync<T>(string name)
    {
        var path = PathOf(name);
        if (!File.Exists(path)) return new List<T>();

        var raw = await File.ReadAllTextAsync(path, Encoding.UTF8).ConfigureAwait(false);
        if (string.IsNullOrWhiteSpace(raw)) return new List<T>();

        var list = System.Text.Json.JsonSerializer.Deserialize<List<T>>(raw, AppJson.Disk);
        if (list is null)
            throw new InvalidDataException($"JSON 数据结构异常：{name}（预期为数组）");
        return list;
    }

    /// <summary>原子写：临时文件 + 覆盖式 Move，并保留一份 .bak。</summary>
    public async Task WriteAtomicAsync<T>(string name, List<T> list)
    {
        var path = PathOf(name);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);

        var json = System.Text.Json.JsonSerializer.Serialize(list, AppJson.Disk);
        var tmp = path + ".tmp";

        await File.WriteAllTextAsync(tmp, json, new UTF8Encoding(false)).ConfigureAwait(false);

        if (File.Exists(path))
        {
            try { File.Copy(path, path + ".bak", overwrite: true); }
            catch { /* 备份失败不阻断主流程 */ }
        }

        File.Move(tmp, path, overwrite: true);
    }

    /// <summary>追加一行 JSONL（审计链专用：只追加，不重写，天然抗"改历史"）。</summary>
    public async Task AppendLineAsync(string name, string line)
    {
        var gate = Gate("jsonl:" + name);
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            var path = PathOf(name);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            await using var fs = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read, 4096, useAsync: true);
            await using var sw = new StreamWriter(fs, new UTF8Encoding(false));
            await sw.WriteLineAsync(line).ConfigureAwait(false);
            await sw.FlushAsync().ConfigureAwait(false);
        }
        finally { gate.Release(); }
    }

    public async Task<List<string>> ReadLinesAsync(string name, int maxLines = 0)
    {
        var path = PathOf(name);
        if (!File.Exists(path)) return new List<string>();

        var gate = Gate("jsonl:" + name);
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            var lines = new List<string>();
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var sr = new StreamReader(fs, Encoding.UTF8);
            string? line;
            while ((line = await sr.ReadLineAsync().ConfigureAwait(false)) is not null)
            {
                if (line.Length > 0) lines.Add(line);
            }

            if (maxLines > 0 && lines.Count > maxLines)
                lines = lines.Skip(lines.Count - maxLines).ToList();
            return lines;
        }
        finally { gate.Release(); }
    }

    /// <summary>二进制块（版本库内容寻址存储）读取。</summary>
    public async Task<byte[]?> ReadBlobAsync(string relativePath)
    {
        var full = SafePath(relativePath);
        if (full is null || !File.Exists(full)) return null;
        return await File.ReadAllBytesAsync(full).ConfigureAwait(false);
    }

    public async Task WriteBlobAsync(string relativePath, byte[] content)
    {
        var full = SafePath(relativePath) ?? throw new InvalidOperationException("路径非法");
        Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        var tmp = full + ".tmp";
        await File.WriteAllBytesAsync(tmp, content).ConfigureAwait(false);
        File.Move(tmp, full, overwrite: true);
    }

    /// <summary>把相对路径限制在存储根内（防路径穿越写文件）。</summary>
    private string? SafePath(string relativePath)
    {
        if (string.IsNullOrWhiteSpace(relativePath)) return null;
        if (relativePath.Contains("..", StringComparison.Ordinal)) return null;

        var full = Path.GetFullPath(Path.Combine(_root, relativePath));
        var rootWithSep = _root.EndsWith(Path.DirectorySeparatorChar) ? _root : _root + Path.DirectorySeparatorChar;
        return full.StartsWith(rootWithSep, StringComparison.OrdinalIgnoreCase) ? full : null;
    }
}
