using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Tools;

namespace AiApproval.Services;

/// <summary>
/// 业务数据初始化与迁移（甲方 / 乙方对接台的底座）。
///
/// 做三件事：
///   1) **迁移**：给已存在的客户档案补齐新增字段（contact/industry/status/requirement/account_user_id），只补空值不覆盖；
///   2) **播种**：需求单数据集为空时写入演示数据（DatasetStore.EnsureSeedAsync 覆盖）；
///   3) **绑定**：把演示账号（用户角色）绑定到某个客户档案上，让"我司档案"一登录就有内容。
/// 全部是服务端维护操作，不经过 AI 工具链（用户与 AI 都无法自行绑定账号与档案）。
/// </summary>
public sealed class BusinessSeed
{
    private readonly DatasetStore _datasets;
    private readonly UserService _users;
    private readonly AuditLog _audit;
    private readonly ILogger<BusinessSeed> _logger;

    public BusinessSeed(DatasetStore datasets, UserService users, AuditLog audit, ILogger<BusinessSeed> logger)
    {
        _datasets = datasets;
        _users = users;
        _audit = audit;
        _logger = logger;
    }

    public async Task<List<string>> EnsureAsync()
    {
        var notes = new List<string>();

        var patched = await _datasets.EnsureSchemaFieldsAsync("customers").ConfigureAwait(false);
        if (patched > 0) notes.Add($"客户档案已补齐 {patched} 个新增字段（旧数据自动迁移，未覆盖任何已有值）");

        await _datasets.EnsureSeedAsync().ConfigureAwait(false);

        // 演示绑定：把"用户"角色账号绑到客户档案上（已绑定则跳过）
        var clients = await _datasets.RowsAsync("customers").ConfigureAwait(false);
        var alreadyLinked = clients.Any(r => !string.IsNullOrWhiteSpace(Get(r, "account_user_id")));
        if (alreadyLinked) return notes;

        var users = await _users.AllAsync().ConfigureAwait(false);
        var candidates = users.Where(u => u.Role == Roles.User && !u.Disabled).ToList();
        if (candidates.Count == 0) return notes;

        // 优先绑定到还没指派负责人的档案之外的第一条；若存在演示档案 C1003 则优先使用
        var preferred = clients.FirstOrDefault(r => Get(r, "id").Equals("C1003", StringComparison.OrdinalIgnoreCase))
                        ?? clients.FirstOrDefault();

        if (preferred is null) return notes;

        var clientId = Get(preferred, "id");
        var userName = candidates[0].UserName;
        var ok = await _datasets.SetFieldServerSideAsync("customers", clientId, "account_user_id", candidates[0].Id).ConfigureAwait(false);

        if (ok)
        {
            notes.Add($"已把甲方账号 {userName} 绑定到客户档案 {clientId}（{Get(preferred, "name")}），用于演示「我司档案」");
            await _audit.WriteAsync("system", "system", "system", "", "biz.link-account", clientId, "SUCCESS",
                $"绑定账号 {userName} → 甲方档案 {clientId}").ConfigureAwait(false);
        }

        _logger.LogInformation("业务数据初始化完成，绑定 {Count} 条", ok ? 1 : 0);
        return notes;
    }

    private static string Get(Dictionary<string, string> row, string field)
    {
        var kv = row.FirstOrDefault(x => string.Equals(x.Key, field, StringComparison.OrdinalIgnoreCase));
        return kv.Key is null ? "" : kv.Value;
    }
}
