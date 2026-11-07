using AiApproval.Core;
using AiApproval.Security;

namespace AiApproval.Services;

/// <summary>
/// 拦截台账：记录"内容异常、因此没有写进飞书"的单据，供管理员审计追踪。
/// 与审计哈希链的关系：每条台账同时写一条哈希链审计（invoice.intercept），
/// 台账本身是便于检索的结构化视图，审计链才是不可篡改的凭证。
/// </summary>
public sealed class InterceptService
{
    private const int MaxPayloadChars = 800;
    private const int KeepRecords = 2000;

    private readonly AppConfig _cfg;
    private readonly JsonStore _store;
    private readonly AuditLog _audit;

    public InterceptService(AppConfig cfg, JsonStore store, AuditLog audit)
    {
        _cfg = cfg;
        _store = store;
        _audit = audit;
    }

    //记一条拦截（不写飞书，只落台账 + 审计）
    public async Task<InterceptRecord> RecordAsync(RequestIdentity identity, string action,
        AnomalyResult anomaly, string field, string? payload, string context)
    {
        var record = new InterceptRecord
        {
            Id = "ic_" + Crypto.RandomHex(6),
            CreatedAt = DateTime.UtcNow,
            UserId = identity.User.Id,
            UserName = identity.User.UserName,
            DisplayName = identity.User.DisplayName,
            EmployeeId = identity.User.EmployeeId,
            Role = identity.User.Role,
            Ip = identity.Ip,
            Action = action,
            Category = anomaly.Category,
            Pattern = anomaly.Pattern,
            Field = field,
            Snippet = Truncate(anomaly.Snippet, 200),
            Payload = Truncate(payload, MaxPayloadChars),
            Context = Truncate(context, 300),
            Handled = "pending",
        };

        await _store.MutateAsync<InterceptRecord, bool>(StoreNames.Intercepts, list =>
        {
            list.Add(record);

            //只保留最近 N 条，避免文件无限增长（审计链里仍然有全量凭证）
            if (list.Count > KeepRecords)
                list.RemoveRange(0, list.Count - KeepRecords);

            return true;
        }).ConfigureAwait(false);

        await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
            "invoice.intercept", record.Id, "BLOCKED",
            $"内容异常已拦截，未写入飞书：类别={anomaly.Category} 规则={anomaly.Pattern} 字段={field}；" +
            $"片段={Truncate(anomaly.Snippet, 120)}").ConfigureAwait(false);

        return record;
    }

    //列表（管理端）
    public async Task<List<InterceptRecord>> ListAsync(string? handled, int limit)
    {
        var list = await _store.ReadListAsync<InterceptRecord>(StoreNames.Intercepts).ConfigureAwait(false);

        IEnumerable<InterceptRecord> q = list;
        if (!string.IsNullOrWhiteSpace(handled))
            q = q.Where(r => string.Equals(r.Handled, handled.Trim(), StringComparison.OrdinalIgnoreCase));

        return q.OrderByDescending(r => r.CreatedAt)
                .Take(Math.Clamp(limit <= 0 ? 100 : limit, 1, 500))
                .ToList();
    }

    //待处理数量（总览用）
    public async Task<int> CountPendingAsync()
        => (await _store.ReadListAsync<InterceptRecord>(StoreNames.Intercepts).ConfigureAwait(false))
            .Count(r => r.IsPending);

    //单条详情
    public async Task<InterceptRecord?> GetAsync(string id)
        => (await _store.ReadListAsync<InterceptRecord>(StoreNames.Intercepts).ConfigureAwait(false))
            .FirstOrDefault(r => string.Equals(r.Id, id, StringComparison.Ordinal));

    //管理员标记处理结果：confirmed / falsePositive / ignored
    public async Task<(bool Ok, string Error)> HandleAsync(string id, string newState, string note,
        RequestIdentity admin)
    {
        var allowed = new[] { "confirmed", "falsePositive", "ignored" };
        if (!allowed.Contains(newState, StringComparer.OrdinalIgnoreCase))
            return (false, "处理结果只能是 confirmed / falsePositive / ignored");

        var updated = await _store.MutateAsync<InterceptRecord, InterceptRecord?>(StoreNames.Intercepts, list =>
        {
            var target = list.FirstOrDefault(r => string.Equals(r.Id, id, StringComparison.Ordinal));
            if (target is null) return null;

            target.Handled = newState.ToLowerInvariant();
            target.HandledBy = admin.User.UserName;
            target.HandledAt = DateTime.UtcNow;
            if (!string.IsNullOrWhiteSpace(note)) target.Note = Truncate(note, 200);
            return target;
        }).ConfigureAwait(false);

        if (updated is null) return (false, "拦截记录不存在");

        await _audit.WriteAsync(admin.User.Id, admin.User.UserName, admin.User.Role, admin.Ip,
            "invoice.intercept.handle", id, updated.Handled.ToUpperInvariant(),
            $"管理员处理拦截记录：{updated.Category}/{updated.Pattern}；备注={Truncate(note, 120)}").ConfigureAwait(false);

        return (true, "");
    }

    private static string Truncate(string? value, int max)
    {
        if (string.IsNullOrEmpty(value)) return "";
        var cleaned = value.Replace("\r", " ").Replace("\n", " ").Trim();
        return cleaned.Length <= max ? cleaned : cleaned[..max];
    }
}
