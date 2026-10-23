using System.Text;
using System.Text.Json;
using System.Collections.Concurrent;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Tools;

namespace AiApproval.Services;

//一条报销单
public sealed record InvoiceRow(string RecordId, Dictionary<string, JsonElement> Fields)
{
    //取字段文本
    public string Text(string name)
    {
        if (!Fields.TryGetValue(name, out var v)) return "";
        return v.ValueKind switch
        {
            JsonValueKind.String => v.GetString() ?? "",
            JsonValueKind.Number => v.ToString(),
            _ => "",
        };
    }

    //取金额
    public decimal Amount
    {
        get
        {
            var raw = Text(InvoicePolicy.FieldAmount);
            return decimal.TryParse(raw, out var v) ? v : 0m;
        }
    }
}

//写操作结果
public sealed record InvoiceResult(bool Ok, string Error = "", string RecordId = "", string Message = "")
{
    //自动编号，写完回取
    public string BillNo { get; init; } = "";

    public static InvoiceResult Fail(string error) => new(false, error);
}

//批量审核的单条结果
public sealed class BatchReviewItem
{
    public string RecordId { get; set; } = "";
    public bool Ok { get; set; }
    public string Error { get; set; } = "";
    public string Message { get; set; } = "";
}

//批量审核汇总
public sealed class BatchReviewResult
{
    public string Error { get; set; } = "";
    public List<BatchReviewItem> Items { get; set; } = new();
    public int Success { get; set; }
    public int Failed { get; set; }
}

//报销单服务：唯一写入口
public sealed class InvoiceService
{
    private readonly AppConfig _cfg;
    private readonly FeishuApi _api;
    private readonly VersionStore _versions;
    private readonly AuditLog _audit;

    //短期幂等：同一用户 90 秒内提交内容完全相同的单据，直接返回上一次结果（防连点/重试造成重复行）
    private readonly ConcurrentDictionary<string, (string RecordId, string BillNo, string Message, DateTime At)>
        _recentSubmits = new(StringComparer.Ordinal);
    private readonly ILogger<InvoiceService> _logger;

    public InvoiceService(AppConfig cfg, FeishuApi api, VersionStore versions, AuditLog audit, ILogger<InvoiceService> logger)
    {
        _cfg = cfg;
        _api = api;
        _versions = versions;
        _audit = audit;
        _logger = logger;
    }

    //功能是否可用
    public bool Enabled => _cfg.Feishu.Configured && !string.IsNullOrWhiteSpace(_cfg.Feishu.TableId);

    //不可用原因
    public string DisabledReason =>
        !_cfg.Feishu.Configured
            ? "未配置飞书凭证（config/secrets.local.json）"
            : "未配置报销单主表 Feishu:TableId";

    //列出报销单（按角色过滤）
    public async Task<(bool Ok, string Error, List<InvoiceRow> Rows)> ListAsync(RequestIdentity identity, int limit,
        CancellationToken ct = default)
    {
        if (!Enabled) return (false, DisabledReason, new List<InvoiceRow>());
        try
        {
            var items = await _api.ListRecordsAsync(_cfg.Feishu.TableId, Math.Clamp(limit, 1, 500), ct).ConfigureAwait(false);
            var rows = items.Select(ToRow).ToList();

            if (!InvoicePolicy.CanSeeAll(identity.User.Role))
                rows = rows.Where(r => InvoicePolicy.BelongsTo(identity.User.Role, identity.User.EmployeeId,
                    identity.User.DisplayName, r.Text(InvoicePolicy.FieldEmployeeId),
                    r.Text(InvoicePolicy.FieldApplicant))).ToList();

            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.list", "报销单主表", "SUCCESS",
                $"查询到 {rows.Count} 条（范围={(InvoicePolicy.CanSeeAll(identity.User.Role) ? "全部" : "仅本人")}）").ConfigureAwait(false);

            return (true, "", rows);
        }
        catch (Exception ex)
        {
            var msg = Describe(ex);
            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.list", "报销单主表", "FAILED", msg).ConfigureAwait(false);
            return (false, msg, new List<InvoiceRow>());
        }
    }

    //读取单条并校验归属
    public async Task<(bool Ok, string Error, InvoiceRow? Row)> GetAsync(RequestIdentity identity, string recordId,
        CancellationToken ct = default)
    {
        if (!Enabled) return (false, DisabledReason, null);
        try
        {
            var items = await _api.ListRecordsAsync(_cfg.Feishu.TableId, 500, ct).ConfigureAwait(false);
            var row = items.Select(ToRow).FirstOrDefault(r => r.RecordId == recordId);
            if (row is null) return (false, "单据不存在（可能已被删除）", null);

            if (!InvoicePolicy.BelongsTo(identity.User.Role, identity.User.EmployeeId, identity.User.DisplayName,
                    row.Text(InvoicePolicy.FieldEmployeeId), row.Text(InvoicePolicy.FieldApplicant)))
                return (false, "无权查看他人的报销单", null);

            return (true, "", row);
        }
        catch (Exception ex)
        {
            return (false, Describe(ex), null);
        }
    }

    //提交报销单
    public async Task<InvoiceResult> SubmitAsync(RequestIdentity identity, InvoicePolicy.SubmitDraft draft,
        string taskId, CancellationToken ct = default)
    {
        if (!Enabled) return InvoiceResult.Fail(DisabledReason);

        var today = DateOnly.FromDateTime(DateTime.UtcNow.AddHours(8));   //按东八区取日期
        var error = InvoicePolicy.ValidateSubmit(draft, out var compliance, today);
        if (error is not null)
        {
            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.submit", "报销单主表", "REJECTED", $"校验未通过：{error}").ConfigureAwait(false);
            return InvoiceResult.Fail(error);
        }

        //幂等闸门：90 秒内同样的单据只写一次（前端连点 / 网关重试 / 超时重发都合并成一次）
        var idemKey = SubmitKey(identity, draft);
        if (_recentSubmits.TryGetValue(idemKey, out var last) && DateTime.UtcNow - last.At < TimeSpan.FromSeconds(90))
        {
            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.submit", last.RecordId, "DUPLICATE-MERGED",
                $"90 秒内重复提交相同单据，已直接返回上次结果（单号={last.BillNo}），未重复写表").ConfigureAwait(false);
            return new InvoiceResult(true, "", last.RecordId,
                $"重复提交已合并（未重复写表）：单号 {last.BillNo}") { BillNo = last.BillNo };
        }

        //只写表里真实存在的列
        var fields = new Dictionary<string, object?>        {
            [InvoicePolicy.FieldApplicant] = identity.User.DisplayName,
            [InvoicePolicy.FieldEmployeeId] = identity.User.EmployeeId,
            [InvoicePolicy.FieldDepartment] = draft.Department,
            [InvoicePolicy.FieldType] = draft.Type,
            [InvoicePolicy.FieldAmount] = draft.Amount,
            [InvoicePolicy.FieldReason] = draft.Reason.Trim(),
            [InvoicePolicy.FieldInvoiceState] = draft.InvoiceState,
            [InvoicePolicy.FieldStatus] = "待审核",
            [InvoicePolicy.FieldCompliance] = compliance.Result,
            [InvoicePolicy.FieldRiskSummary] = compliance.RiskSummary,
            [InvoicePolicy.FieldSubmittedAt] = NowMs(),
        };
        if (!string.IsNullOrWhiteSpace(draft.City)) fields[InvoicePolicy.FieldCity] = draft.City;
        if (draft.InvoiceDate.HasValue)
            fields[InvoicePolicy.FieldInvoiceDate] = ToMs(draft.InvoiceDate.Value);
        if (!string.IsNullOrWhiteSpace(draft.InvoiceInfo)) fields[InvoicePolicy.FieldInvoiceInfo] = draft.InvoiceInfo;

        try
        {
            fields = await FitAsync(fields, ct).ConfigureAwait(false);
            var recordId = await _api.CreateRecordAsync(_cfg.Feishu.TableId, fields, ct).ConfigureAwait(false);
            if (string.IsNullOrEmpty(recordId)) return InvoiceResult.Fail("飞书未返回 record_id，写入结果未知，请到飞书表核对");

            //回取可读单号（短暂重试一次）
            var billNo = await FetchBillNoAsync(recordId, ct).ConfigureAwait(false);

            //记版本：新建时 previousBytes 为空，回退即删除该行
            var undo = AppJson.Serialize(new
            {
                kind = "invoice",
                tableId = _cfg.Feishu.TableId,
                recordId,
                action = "submit",
                billNo,
                fields,
            });
            var version = await _versions.RecordAsync(taskId, identity.User.Id, identity.User.UserName, "invoice",
                "submit", VersionTarget(recordId), null, Encoding.UTF8.GetBytes(AppJson.Serialize(fields)),
                undo).ConfigureAwait(false);

            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.submit", recordId, "SUCCESS",
                $"报销单已写入飞书：单号={billNo} {draft.Type} {draft.Amount} 元，合规={compliance.Result}，版本={version.Id}").ConfigureAwait(false);

            var okMessage = $"报销单已提交：单号 {billNo}，{draft.Type} {draft.Amount:0.##} 元，合规结果 {compliance.Result}";
            Remember(idemKey, recordId, billNo, okMessage);

            return new InvoiceResult(true, "", recordId, okMessage)
            {
                BillNo = billNo,
            };
        }
        catch (FeishuApiException ex) when (ex.UnknownState)
        {
            //写入结果未知（超时/连接中断）：绝不重试。先回查——真写进去了就认领，没写进去才允许用户重提
            var adopted = await TryAdoptAsync(identity, fields, ct).ConfigureAwait(false);
            if (!string.IsNullOrEmpty(adopted))
            {
                var billNo = await FetchBillNoAsync(adopted, ct).ConfigureAwait(false);
                var msg = $"报销单已提交（写入超时后回查确认成功）：单号 {billNo}，{draft.Type} {draft.Amount:0.##} 元";
                Remember(idemKey, adopted, billNo, msg);
                await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                    "invoice.submit", adopted, "SUCCESS",
                    $"飞书写入超时（{Describe(ex)}），回查确认已写入并认领该行，未重复写单；单号={billNo}").ConfigureAwait(false);
                return new InvoiceResult(true, "", adopted, msg) { BillNo = billNo };
            }

            var unknownMsg = "提交超时，且回查未发现该单据：本次确认**没有**写入，可以安全地重新提交";
            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.submit", "报销单主表", "UNKNOWN",
                $"飞书写入超时（{Describe(ex)}），回查未命中，未写入；已阻止自动重试以免重复写单").ConfigureAwait(false);
            return InvoiceResult.Fail(unknownMsg);
        }
        catch (Exception ex)
        {
            var msg = Describe(ex);
            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.submit", "报销单主表", "FAILED", msg).ConfigureAwait(false);
            return InvoiceResult.Fail(msg);
        }
    }

    //审核报销单
    public async Task<InvoiceResult> ReviewAsync(RequestIdentity identity, string recordId, string newStatus,
        string opinion, string taskId, CancellationToken ct = default)
    {
        if (!Enabled) return InvoiceResult.Fail(DisabledReason);

        var (ok, error, row) = await GetAsync(identity, recordId, ct).ConfigureAwait(false);
        if (!ok || row is null) return InvoiceResult.Fail(error);

        var transitionError = InvoicePolicy.ValidateReviewTransition(row.Text(InvoicePolicy.FieldStatus), newStatus);
        if (transitionError is not null)
            return InvoiceResult.Fail(transitionError);

        if (opinion.Trim().Length is < 2 or > 300)
            return InvoiceResult.Fail("审批意见需为 2 到 300 个字符");

        var fields = new Dictionary<string, object?>
        {
            [InvoicePolicy.FieldStatus] = newStatus,
            [InvoicePolicy.FieldReviewer] = identity.User.DisplayName,
            [InvoicePolicy.FieldReviewOpinion] = opinion.Trim(),
            [InvoicePolicy.FieldReviewedAt] = NowMs(),
        };

        return await MutateAsync(identity, row, fields, "review", taskId,
            $"审核={newStatus}", ct).ConfigureAwait(false);
    }

    //批量审核：逐单复用单条写入口，逐单回报，部分失败不影响其他单
    public async Task<BatchReviewResult> BatchReviewAsync(RequestIdentity identity, IReadOnlyList<string> recordIds,
        string newStatus, string opinion, CancellationToken ct = default)
    {
        var result = new BatchReviewResult();

        var ids = recordIds
            .Where(x => !string.IsNullOrWhiteSpace(x))
            .Select(x => x.Trim())
            .Distinct(StringComparer.Ordinal)
            .ToList();

        var cap = Math.Clamp(_cfg.InvoiceAi.MaxBatchReview, 1, 200);
        if (ids.Count > cap)
        {
            result.Error = $"一次最多处理 {cap} 单（本次 {ids.Count} 单），请分批执行";
            return result;
        }
        if (ids.Count == 0)
        {
            result.Error = "没有要处理的单据";
            return result;
        }

        foreach (var id in ids)
        {
            ct.ThrowIfCancellationRequested();

            //每单都走完整写入口：状态流转校验 + 意见长度 + 归属校验 + 快照 + 版本 + 审计
            var one = await ReviewAsync(identity, id, newStatus, opinion, "", ct).ConfigureAwait(false);

            result.Items.Add(new BatchReviewItem
            {
                RecordId = id,
                Ok = one.Ok,
                Error = one.Error,
                Message = one.Message,
            });

            if (one.Ok) result.Success++;
            else result.Failed++;
        }

        await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
            "invoice.batch", "报销单主表", result.Failed == 0 ? "SUCCESS" : "PARTIAL",
            $"批量审核={newStatus}：共 {result.Items.Count} 单，成功 {result.Success}，失败 {result.Failed}").ConfigureAwait(false);

        return result;
    }

    //修改报销单字段
    public async Task<InvoiceResult> UpdateAsync(RequestIdentity identity, string recordId,        IDictionary<string, string> patch, string taskId, CancellationToken ct = default)
    {
        if (!Enabled) return InvoiceResult.Fail(DisabledReason);

        var (ok, error, row) = await GetAsync(identity, recordId, ct).ConfigureAwait(false);
        if (!ok || row is null) return InvoiceResult.Fail(error);

        if (patch.Count == 0) return InvoiceResult.Fail("没有任何要修改的字段");

        var fields = new Dictionary<string, object?>();
        foreach (var (name, value) in patch)
        {
            if (!InvoicePolicy.FinanceEditableFields.Contains(name))
                return InvoiceResult.Fail($"字段不允许修改：{name}（可改：{string.Join("、", InvoicePolicy.FinanceEditableFields)}）");

            switch (name)
            {
                case InvoicePolicy.FieldDepartment:
                    if (!InvoicePolicy.Departments.Contains(value)) return InvoiceResult.Fail("部门不在允许的枚举内");
                    fields[name] = value;
                    break;
                case InvoicePolicy.FieldType:
                    if (!InvoicePolicy.Types.Contains(value)) return InvoiceResult.Fail("报销类型不在允许的枚举内");
                    fields[name] = value;
                    break;
                case InvoicePolicy.FieldAmount:
                    if (!decimal.TryParse(value, out var amount) || amount < InvoicePolicy.MinAmount || amount > InvoicePolicy.MaxAmount)
                        return InvoiceResult.Fail($"金额必须是 {InvoicePolicy.MinAmount} 到 {InvoicePolicy.MaxAmount} 之间的数字");
                    fields[name] = amount;
                    break;
                case InvoicePolicy.FieldInvoiceState:
                    if (!InvoicePolicy.InvoiceStates.Contains(value)) return InvoiceResult.Fail("票据状态不在允许的枚举内");
                    fields[name] = value;
                    break;
                case InvoicePolicy.FieldCity:
                    if (value.Length > 0 && !InvoicePolicy.Cities.Contains(value)) return InvoiceResult.Fail("城市不在允许的清单内");
                    fields[name] = value;
                    break;
                case InvoicePolicy.FieldInvoiceDate:
                    if (!DateOnly.TryParse(value, out var date)) return InvoiceResult.Fail("发票日期格式应为 yyyy-MM-dd");
                    fields[name] = ToMs(date);
                    break;
                case InvoicePolicy.FieldCompliance:
                    if (!InvoicePolicy.ComplianceResults.Contains(value)) return InvoiceResult.Fail("合规结果只能是 通过 或 风险");
                    fields[name] = value;
                    break;
                case InvoicePolicy.FieldReason:
                case InvoicePolicy.FieldRiskSummary:
                    if (value.Length > 300) return InvoiceResult.Fail($"{name}不得超过 300 个字符");
                    fields[name] = value;
                    break;
            }
        }

        return await MutateAsync(identity, row, fields, "update", taskId,
            $"修改字段={string.Join('、', fields.Keys)}", ct).ConfigureAwait(false);
    }

    //删除报销单
    public async Task<InvoiceResult> DeleteAsync(RequestIdentity identity, string recordId, string reason,
        string taskId, CancellationToken ct = default)
    {
        if (!Enabled) return InvoiceResult.Fail(DisabledReason);

        var (ok, error, row) = await GetAsync(identity, recordId, ct).ConfigureAwait(false);
        if (!ok || row is null) return InvoiceResult.Fail(error);

        try
        {
            var before = SnapshotBytes(row);
            await _api.DeleteRecordAsync(_cfg.Feishu.TableId, recordId, ct).ConfigureAwait(false);

            var undo = AppJson.Serialize(new
            {
                kind = "invoice",
                tableId = _cfg.Feishu.TableId,
                recordId,
                action = "delete",
                billNo = row.Text(InvoicePolicy.FieldBillNo),
                fields = row.Fields.ToDictionary(kv => kv.Key, kv => kv.Value.GetRawText()),
            });
            var version = await _versions.RecordAsync(taskId, identity.User.Id, identity.User.UserName, "invoice",
                "delete", VersionTarget(recordId), before, null, undo).ConfigureAwait(false);

            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.delete", recordId, "SUCCESS",
                $"删除报销单 {row.Text(InvoicePolicy.FieldBillNo)}：{reason}（版本={version.Id}，可按日志回退重建）").ConfigureAwait(false);

            return new InvoiceResult(true, "", recordId,
                $"已删除单据 {row.Text(InvoicePolicy.FieldBillNo)}（快照已保存，可按日志回退重建）");
        }
        catch (Exception ex)
        {
            var msg = Describe(ex);
            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.delete", recordId, "FAILED", msg).ConfigureAwait(false);
            return InvoiceResult.Fail(msg);
        }
    }

    //按版本回退一次操作
    public async Task<InvoiceResult> RollbackAsync(RequestIdentity identity, string versionId,
        string taskId, CancellationToken ct = default)
    {
        if (!Enabled) return InvoiceResult.Fail(DisabledReason);

        var version = await _versions.GetAsync(versionId).ConfigureAwait(false);
        if (version is null) return InvoiceResult.Fail("版本记录不存在");
        if (version.Kind != "invoice") return InvoiceResult.Fail("该版本不是报销单操作，请到相应入口回退");
        if (version.Reverted) return InvoiceResult.Fail("该版本已经回退过（不允许重复回退）");

        Dictionary<string, JsonElement>? undo;
        try
        {
            undo = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(version.UndoLogJson);
        }
        catch (Exception ex)
        {
            return InvoiceResult.Fail($"撤销日志无法解析：{ex.Message}");
        }
        if (undo is null) return InvoiceResult.Fail("撤销日志为空");

        var recordId = undo.TryGetValue("recordId", out var rid) ? rid.GetString() ?? "" : "";
        var action = undo.TryGetValue("action", out var act) ? act.GetString() ?? "" : "";
        if (string.IsNullOrEmpty(recordId)) return InvoiceResult.Fail("撤销日志缺少 recordId");

        try
        {
            switch (action)
            {
                case "submit":
                {
                    //新建的回退即删除；已不存在则视为已回退
                    var (ok, _, row) = await GetAsync(identity, recordId, ct).ConfigureAwait(false);
                    if (!ok || row is null)
                    {
                        await MarkRevertedAsync(version.Id, identity, ct).ConfigureAwait(false);
                        await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                            "invoice.rollback", recordId, "SKIPPED",
                            $"版本 {version.Id} 回退提交：单据已不存在，视为已回退").ConfigureAwait(false);
                        return new InvoiceResult(true, "", recordId, "该单据已不存在，视为已回退（无需重复操作）");
                    }

                    var before = SnapshotBytes(row);
                    await _api.DeleteRecordAsync(_cfg.Feishu.TableId, recordId, ct).ConfigureAwait(false);

                    //再存一份可恢复快照
                    await _versions.RecordAsync(taskId, identity.User.Id, identity.User.UserName, "invoice",
                        "rollback-delete", VersionTarget(recordId), before, null,
                        AppJson.Serialize(new
                        {
                            kind = "invoice",
                            tableId = _cfg.Feishu.TableId,
                            recordId,
                            action = "delete",
                            billNo = row.Text(InvoicePolicy.FieldBillNo),
                            fields = row.Fields.ToDictionary(kv => kv.Key, kv => kv.Value.GetRawText()),
                        })).ConfigureAwait(false);
                    break;
                }
                case "delete":
                {
                    //删除的回退即按快照重建
                    var fields = ReadSnapshotFields(undo);
                    if (fields.Count == 0) return InvoiceResult.Fail("快照为空，无法重建");
                    fields = SanitizeForFeishu(fields);
                    fields = await FitAsync(fields, ct).ConfigureAwait(false);
                    var newId = await _api.CreateRecordAsync(_cfg.Feishu.TableId, fields, ct).ConfigureAwait(false);
                    if (string.IsNullOrEmpty(newId)) return InvoiceResult.Fail("重建失败：飞书未返回 record_id");
                    recordId = newId;

                    //重建出来的行也要有自己的版本记录
                    await _versions.RecordAsync(taskId, identity.User.Id, identity.User.UserName, "invoice",
                        "rebuild", VersionTarget(newId), null, Encoding.UTF8.GetBytes(AppJson.Serialize(fields)),
                        AppJson.Serialize(new
                        {
                            kind = "invoice",
                            tableId = _cfg.Feishu.TableId,
                            recordId = newId,
                            action = "submit",
                            rebuiltFrom = version.Id,
                            fields,
                        })).ConfigureAwait(false);
                    break;
                }
                default:
                {
                    //修改 / 审核的回退即写回旧值
                    var previous = await _versions.ReadBlobAsync(version.PreviousBlob).ConfigureAwait(false);
                    if (previous is null) return InvoiceResult.Fail("找不到快照内容，无法回退");
                    var oldFields = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(Encoding.UTF8.GetString(previous));
                    if (oldFields is null) return InvoiceResult.Fail("快照内容无法解析");

                    var restore = new Dictionary<string, object?>();
                    foreach (var name in InvoicePolicy.FinanceEditableFields
                                 .Concat(new[] { InvoicePolicy.FieldStatus, InvoicePolicy.FieldReviewer, InvoicePolicy.FieldReviewOpinion }))
                    {
                        if (oldFields.TryGetValue(name, out var v))
                            restore[name] = CoerceSnapshotValue(name, v);
                        else if (InvoicePolicy.ClearWhenAbsentFields.Contains(name))
                            restore[name] = "";   //快照里没有 = 当时就是空的，回退要把它清掉
                    }
                    if (restore.Count == 0) return InvoiceResult.Fail("快照中没有可恢复的字段");
                    restore = SanitizeForFeishu(restore);
                    restore = await FitAsync(restore, ct).ConfigureAwait(false);
                    await _api.UpdateRecordAsync(_cfg.Feishu.TableId, recordId, restore, ct).ConfigureAwait(false);
                    break;
                }
            }

            await MarkRevertedAsync(version.Id, identity, ct).ConfigureAwait(false);

            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.rollback", recordId, "SUCCESS",
                $"按版本 {version.Id} 回退（原动作={action}，单号={version.Target}）").ConfigureAwait(false);

            return new InvoiceResult(true, "", recordId, $"已按日志回退（{action}），单据号可能变化，请到列表确认");
        }
        catch (Exception ex)
        {
            var msg = Describe(ex);
            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                "invoice.rollback", recordId, "FAILED", msg).ConfigureAwait(false);
            return InvoiceResult.Fail(msg);
        }
    }

    //写记录 + 版本 + 审计
    private async Task<InvoiceResult> MutateAsync(RequestIdentity identity, InvoiceRow row,
        Dictionary<string, object?> fields, string operation, string taskId, string note, CancellationToken ct)
    {
        try
        {
            var before = SnapshotBytes(row);
            fields = await FitAsync(fields, ct).ConfigureAwait(false);
            await _api.UpdateRecordAsync(_cfg.Feishu.TableId, row.RecordId, fields, ct).ConfigureAwait(false);

            var after = new Dictionary<string, object?>();
            foreach (var (name, value) in row.Fields)
                after[name] = JsonToValue(value);
            foreach (var (name, value) in fields) after[name] = value;

            var undo = AppJson.Serialize(new
            {
                kind = "invoice",
                tableId = _cfg.Feishu.TableId,
                recordId = row.RecordId,
                action = operation,
                fields,
            });
            var version = await _versions.RecordAsync(taskId, identity.User.Id, identity.User.UserName, "invoice",
                operation, VersionTarget(row.RecordId), before, Encoding.UTF8.GetBytes(AppJson.Serialize(after)),
                undo).ConfigureAwait(false);

            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                $"invoice.{operation}", row.RecordId, "SUCCESS",
                $"{note}；单号={row.Text(InvoicePolicy.FieldBillNo)}；版本={version.Id}").ConfigureAwait(false);

            return new InvoiceResult(true, "", row.RecordId, $"已提交飞书（{note}），可在版本与回退里一键撤销");
        }
        catch (Exception ex)
        {
            var msg = Describe(ex);
            await _audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, identity.Ip,
                $"invoice.{operation}", row.RecordId, "FAILED", msg).ConfigureAwait(false);
            return InvoiceResult.Fail(msg);
        }
    }

    //标记版本已回退
    private async Task MarkRevertedAsync(string versionId, RequestIdentity identity, CancellationToken ct)
    {
        await _versions.UpdateAsync(versionId, v =>
        {
            v.Reverted = true;
            v.RevertedAt = DateTime.UtcNow;
            v.RevertedBy = identity.User.UserName;
        }).ConfigureAwait(false);
    }

    //JSON 转一行
    //幂等键：用户 + 单据内容
    private static string SubmitKey(RequestIdentity identity, InvoicePolicy.SubmitDraft d)
        => string.Join('|',
            identity.User.Id, d.Department, d.Type, d.Amount.ToString("0.##"), d.Reason.Trim(),
            d.InvoiceState, d.City, d.InvoiceDate?.ToString("yyyy-MM-dd") ?? "", d.InvoiceInfo);

    //记住最近一次提交结果（懒惰清理，避免字典无限增长）
    private void Remember(string key, string recordId, string billNo, string message)
    {
        if (_recentSubmits.Count > 500)
        {
            var cutoff = DateTime.UtcNow.AddMinutes(-10);
            foreach (var stale in _recentSubmits.Where(kv => kv.Value.At < cutoff).Select(kv => kv.Key).ToList())
                _recentSubmits.TryRemove(stale, out _);
        }
        _recentSubmits[key] = (recordId, billNo, message, DateTime.UtcNow);
    }

    //写入超时后回查：用我们刚生成的「记录提交时间」（毫秒时间戳）精确定位这一行，再用工号二次确认
    private async Task<string?> TryAdoptAsync(RequestIdentity identity, Dictionary<string, object?> fields,
        CancellationToken ct)
    {
        if (!fields.TryGetValue(InvoicePolicy.FieldSubmittedAt, out var stamp) || stamp is null) return null;

        long want;
        try { want = Convert.ToInt64(stamp); }
        catch { return null; }

        try
        {
            var items = await _api.ListRecordsAsync(_cfg.Feishu.TableId, 200, ct).ConfigureAwait(false);
            foreach (var item in items)
            {
                var row = ToRow(item);
                if (!long.TryParse(row.Text(InvoicePolicy.FieldSubmittedAt), out var got) || got != want) continue;
                if (!string.Equals(row.Text(InvoicePolicy.FieldEmployeeId), identity.User.EmployeeId,
                        StringComparison.OrdinalIgnoreCase)) continue;
                return row.RecordId;
            }
        }
        catch
        {
            //回查本身失败（飞书仍不可用）→ 视为未命中，交给用户决定是否重提
        }

        return null;
    }

    private static InvoiceRow ToRow(JsonElement item)    {
        var fields = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        if (item.TryGetProperty("fields", out var f) && f.ValueKind == JsonValueKind.Object)
            foreach (var p in f.EnumerateObject()) fields[p.Name] = p.Value.Clone();
        var recordId = item.TryGetProperty("record_id", out var rid) ? rid.GetString() ?? "" : "";
        return new InvoiceRow(recordId, fields);
    }

    //行快照字节
    private static byte[] SnapshotBytes(InvoiceRow row)
        => Encoding.UTF8.GetBytes(AppJson.Serialize(row.Fields.ToDictionary(kv => kv.Key, kv => kv.Value.GetRawText())));

    //从撤销日志还原字段
    private static Dictionary<string, object?> ReadSnapshotFields(Dictionary<string, JsonElement> undo)
    {
        var result = new Dictionary<string, object?>();
        if (!undo.TryGetValue("fields", out var node)) return result;
        if (node.ValueKind == JsonValueKind.Object)
        {
            foreach (var p in node.EnumerateObject())
                result[p.Name] = CoerceSnapshotValue(p.Name, p.Value);
        }
        return result;
    }

    //快照里每个字段存的都是 JSON 文本，这里还原成真实类型
    private static object? UnwrapSnapshotValue(JsonElement v)
    {
        if (v.ValueKind != JsonValueKind.String) return JsonToValue(v);
        var raw = v.GetString() ?? "";
        return TryParseJsonValue(raw) ?? raw;
    }

    //按字段名还原成飞书可写的真实类型
    private static object? CoerceSnapshotValue(string name, JsonElement v)
        => CoerceValueForFeishu(name, UnwrapSnapshotValue(v));

    //金额列是数字、日期列只认毫秒数字，飞书读取时都可能返回字符串
    private static object? CoerceValueForFeishu(string name, object? value)
    {
        if (value is null) return null;

        //金额是数字列：快照里可能是字符串，写回必须是数字
        if (name == InvoicePolicy.FieldAmount)
        {
            if (value is decimal or double or long or int) return value;
            if (decimal.TryParse(value as string, out var amount)) return amount;
            return value;
        }

        //日期列只认毫秒数字
        if (InvoicePolicy.DateFields.Contains(name))
        {
            if (value is long or decimal or int) return value;
            if (long.TryParse(value as string, out var ms)) return ms;
            return value;
        }

        return value;
    }

    //日期列不接受空串与 null，其余空串保留（表示清空）
    private static Dictionary<string, object?> SanitizeForFeishu(Dictionary<string, object?> fields)
    {
        var result = new Dictionary<string, object?>(StringComparer.Ordinal);
        foreach (var (name, value) in fields)
        {
            if (value is null) continue;
            if (value is string text && text.Length == 0 && InvoicePolicy.DateFields.Contains(name)) continue;
            result[name] = value;
        }
        return result;
    }

    //尝试解析 JSON 文本
    private static object? TryParseJsonValue(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return null;
        var trimmed = raw.TrimStart();
        if (trimmed.Length == 0) return null;
        if (trimmed[0] is not ('{' or '[' or '"') && !char.IsDigit(trimmed[0]) && trimmed[0] != '-') return null;
        try
        {
            using var doc = JsonDocument.Parse(raw);
            return JsonToValue(doc.RootElement);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    //JSON 值转对象
    private static object? JsonToValue(JsonElement v) => v.ValueKind switch
    {
        JsonValueKind.String => v.GetString(),
        JsonValueKind.Number => v.TryGetDecimal(out var d) ? d : v.GetDouble(),
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        JsonValueKind.Null => null,
        _ => v.GetRawText(),
    };

    //剔除表里不存在的列
    private async Task<Dictionary<string, object?>> FitAsync(Dictionary<string, object?> fields, CancellationToken ct)
    {
        try
        {
            var names = await _api.FieldNamesAsync(_cfg.Feishu.TableId, ct).ConfigureAwait(false);
            var fitted = fields
                .Where(kv => names.Contains(kv.Key) && !InvoicePolicy.ReadOnlyFields.Contains(kv.Key))
                .ToDictionary(kv => kv.Key, kv => kv.Value);
            var dropped = fields.Keys.Except(fitted.Keys).ToList();
            if (dropped.Count > 0)
                _logger.LogInformation("报销单写入剔除了表中不存在的列：{Dropped}", string.Join('、', dropped));
            return fitted;
        }
        catch (Exception ex)
        {
            //字段探测失败不阻断写入
            _logger.LogWarning("字段探测失败，按原字段写入：{Error}", Describe(ex));
            return fields.ToDictionary(kv => kv.Key, kv => kv.Value);
        }
    }

    //版本目标标识
    private static string VersionTarget(string recordId) => $"invoice:{recordId}";

    //回取自动编号，取不到就返回空串
    private async Task<string> FetchBillNoAsync(string recordId, CancellationToken ct)
    {
        for (var attempt = 0; attempt < 2; attempt++)
        {
            try
            {
                var raw = await _api.GetRecordAsync(_cfg.Feishu.TableId, recordId, ct).ConfigureAwait(false);
                using var doc = JsonDocument.Parse(raw);
                if (doc.RootElement.TryGetProperty("data", out var data))
                {
                    var record = data.TryGetProperty("record", out var rec) ? rec : data;
                    if (record.TryGetProperty("fields", out var fieldsNode)
                        && fieldsNode.TryGetProperty(InvoicePolicy.FieldBillNo, out var bill))
                    {
                        var text = bill.ValueKind == JsonValueKind.String ? bill.GetString() ?? "" : bill.ToString();
                        if (text.Length > 0) return text;
                    }
                }
            }
            catch (Exception ex)
            {
                _logger.LogInformation("回取单号失败（不影响提交）：{Error}", Describe(ex));
                return "";
            }

            if (attempt == 0) await Task.Delay(400, ct).ConfigureAwait(false);
        }
        return "";
    }

    //当前毫秒时间戳
    private static long NowMs() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

    //日期转毫秒
    private static long ToMs(DateOnly date)
        => new DateTimeOffset(date.ToDateTime(TimeOnly.MinValue), TimeSpan.FromHours(8)).ToUnixTimeMilliseconds();

    //异常链压成一行
    private static string Describe(Exception ex)
    {
        var sb = new StringBuilder();
        for (var e = ex; e is not null; e = e.InnerException)
        {
            if (sb.Length > 0) sb.Append(" <- ");
            sb.Append(e.GetType().Name).Append('(').Append(e.Message).Append(')');
        }
        return sb.ToString();
    }
}
