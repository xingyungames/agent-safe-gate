using AiApproval.Ai;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Services;

namespace AiApproval.Api;

//报销单接口，唯一写入口
public static class InvoiceApi
{
    public static void Map(WebApplication app)
    {
        var group = app.MapGroup("/api/invoice");

        //提交报销单
        group.MapPost("/submit", async (HttpContext context, InvoiceService invoices, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.InvoiceSubmit))
                return Api.Error(403, "FORBIDDEN", "当前角色无权提交报销单");

            if (!guard.TryConsume($"invoice:user:{identity.User.Id}", 30, TimeSpan.FromMinutes(5), identity.Ip))
                return Api.Error(429, "RATE_LIMITED", "提交过于频繁，请稍后再试");

            var body = Api.ReadBody<InvoiceSubmitRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            DateOnly? date = null;
            if (!string.IsNullOrWhiteSpace(body.InvoiceDate))
            {
                if (!DateOnly.TryParse(body.InvoiceDate, out var parsed))
                    return Api.Error(400, "BAD_DATE", "发票日期格式应为 yyyy-MM-dd");
                date = parsed;
            }

            var draft = new InvoicePolicy.SubmitDraft(
                Api.Clean(body.Department, 32),
                Api.Clean(body.Rtype, 16),
                body.Amount,
                Api.Clean(body.Reason, 300),
                Api.Clean(body.HasInvoice, 8),
                Api.Clean(body.City, 16),
                date,
                Api.Clean(body.InvoiceInfo, 128));

            var result = await invoices.SubmitAsync(identity, draft, Api.Clean(body.TaskId, 64));
            if (!result.Ok) return Api.Error(400, "SUBMIT_REJECTED", result.Error);

            return Api.Ok(new
            {
                recordId = result.RecordId,
                billNo = result.BillNo,
                message = result.Message,
                hint = "单据已写入飞书报销单主表，状态为待审核；请在列表或详情里查看",
            });
        });

        //报销单列表
        group.MapGet("/list", async (HttpContext context, InvoiceService invoices, int? limit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.InvoiceReadSelf))
                return Api.Error(403, "FORBIDDEN", "当前角色无权查看报销单");

            var (ok, error, rows) = await invoices.ListAsync(identity, limit ?? 100);
            if (!ok) return Api.Error(502, "FEISHU_UNAVAILABLE", error);

            return Api.Ok(new
            {
                scope = InvoicePolicy.CanSeeAll(identity.User.Role) ? "all" : "mine",
                applicant = identity.User.DisplayName,
                count = rows.Count,
                rows = rows.Select(ToView).ToList(),
            });
        });

        //单据详情
        group.MapGet("/detail", async (HttpContext context, InvoiceService invoices, string recordId) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var (ok, error, row) = await invoices.GetAsync(identity, Api.Clean(recordId, 64));
            if (!ok || row is null)
            {
                var code = error.Contains("无权") ? "FORBIDDEN" : "NOT_FOUND";
                return Api.Error(code == "FORBIDDEN" ? 403 : 404, code, error);
            }

            return Api.Ok(new { row = ToView(row) });
        });

        //审核
        group.MapPost("/review", async (HttpContext context, InvoiceService invoices, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.InvoiceReview))
                return Api.Error(403, "FORBIDDEN", "当前角色无权审核报销单");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage))
                return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<InvoiceReviewRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var result = await invoices.ReviewAsync(identity, Api.Clean(body.RecordId, 64),
                Api.Clean(body.Status, 16), Api.Clean(body.Opinion, 300), Api.Clean(body.TaskId, 64));
            if (!result.Ok) return Api.Error(400, "REVIEW_REJECTED", result.Error);
            return Api.Ok(new { recordId = result.RecordId, message = result.Message });
        });

        //修改
        group.MapPost("/update", async (HttpContext context, InvoiceService invoices, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.InvoiceUpdate))
                return Api.Error(403, "FORBIDDEN", "当前角色无权修改报销单");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage))
                return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<InvoiceUpdateRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var patch = body.Fields
                .Where(kv => !string.IsNullOrWhiteSpace(kv.Key))
                .ToDictionary(kv => Api.Clean(kv.Key, 32), kv => (kv.Value ?? "").Trim());

            var result = await invoices.UpdateAsync(identity, Api.Clean(body.RecordId, 64), patch,
                Api.Clean(body.TaskId, 64));
            if (!result.Ok) return Api.Error(400, "UPDATE_REJECTED", result.Error);
            return Api.Ok(new { recordId = result.RecordId, message = result.Message });
        });

        //删除
        group.MapPost("/delete", async (HttpContext context, InvoiceService invoices, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.InvoiceDelete))
                return Api.Error(403, "FORBIDDEN", "只有管理员及以上可以删除报销单");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage))
                return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<InvoiceDeleteRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var reason = Api.Clean(body.Reason, 200);
            if (reason.Length < 2) return Api.Error(400, "REASON_REQUIRED", "请填写删除理由（不少于 2 个字符）");

            var result = await invoices.DeleteAsync(identity, Api.Clean(body.RecordId, 64), reason,
                Api.Clean(body.TaskId, 64));
            if (!result.Ok) return Api.Error(400, "DELETE_REJECTED", result.Error);
            return Api.Ok(new { recordId = result.RecordId, message = result.Message });
        });

        //按日志回退
        group.MapPost("/rollback", async (HttpContext context, InvoiceService invoices, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.LogRollback))
                return Api.Error(403, "FORBIDDEN", "只有管理员及以上可以按日志回退");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage))
                return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<InvoiceRollbackRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var result = await invoices.RollbackAsync(identity, Api.Clean(body.VersionId, 64), Api.Clean(body.TaskId, 64));
            if (!result.Ok) return Api.Error(400, "ROLLBACK_REJECTED", result.Error);
            return Api.Ok(new { recordId = result.RecordId, message = result.Message });
        });

        //AI 预审评分：只读，只出等级与理由，不改任何单据状态、不写飞书
        group.MapGet("/ai-scores", async (HttpContext context, InvoiceService invoices, InvoiceAiService ai, bool? refresh) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.InvoiceReview))
                return Api.Error(403, "FORBIDDEN", "当前角色无权查看 AI 预审结果");
            if (!ai.Enabled) return Api.Error(400, "AI_DISABLED", "报销 AI 预审未启用");

            var (ok, error, rows) = await invoices.ListAsync(identity, 500).ConfigureAwait(false);
            if (!ok) return Api.Error(502, "FEISHU_UNAVAILABLE", error);

            var outcome = await ai.ScoreAsync(identity, rows, refresh == true, context.RequestAborted).ConfigureAwait(false);
            var index = outcome.Scores
                .GroupBy(s => s.RecordId, StringComparer.Ordinal)
                .ToDictionary(g => g.Key, g => g.First(), StringComparer.Ordinal);

            return Api.Ok(new
            {
                aiAvailable = outcome.AiAvailable,
                aiError = outcome.AiError,
                quotaExhausted = outcome.QuotaExhausted,
                note = "以下等级仅为 AI 与规则的建议，不改变任何单据状态；审批必须由你本人点击执行",
                stats = new
                {
                    total = outcome.Total,
                    skippedDecided = outcome.SkippedDecided,
                    skippedCached = outcome.SkippedCached,
                    scored = outcome.Scored,
                    ruleOnly = outcome.RuleOnly,
                    aiFailedBatches = outcome.AiFailedBatches,
                    injected = outcome.Injected,
                },
                rows = rows.Select(row => AiScoreView(row, index.GetValueOrDefault(row.RecordId))).ToList(),
            });
        });

        //AI 按需求选单：只出候选清单，绝不执行
        group.MapPost("/ai-find", async (HttpContext context, InvoiceService invoices, InvoiceAiService ai, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.InvoiceReview))
                return Api.Error(403, "FORBIDDEN", "当前角色无权让 AI 选单");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage))
                return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<InvoiceAiFindRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var (ok, error, rows) = await invoices.ListAsync(identity, 500).ConfigureAwait(false);
            if (!ok) return Api.Error(502, "FEISHU_UNAVAILABLE", error);

            var index = await ai.ScoreIndexAsync(rows, context.RequestAborted).ConfigureAwait(false);
            var outcome = await ai.FindAsync(identity, rows, index, Api.Clean(body.Requirement, 200),
                context.RequestAborted).ConfigureAwait(false);

            if (!outcome.Ok) return Api.Error(400, "AI_FIND_FAILED", outcome.Error);

            return Api.Ok(new
            {
                action = outcome.Action,
                note = outcome.Note,
                message = "以上仅为 AI 给出的候选清单，尚未执行任何操作；确认后请点击按钮批量处理",
                picked = outcome.Picked.Select(FindItemView).ToList(),
                dropped = outcome.Dropped.Select(FindItemView).ToList(),
            });
        });

        //批量审核：唯一写入口，必须由财务本人点击；逐单复用单条审核逻辑
        group.MapPost("/batch-review", async (HttpContext context, InvoiceService invoices, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.InvoiceReview))
                return Api.Error(403, "FORBIDDEN", "当前角色无权批量审核");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage))
                return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<InvoiceBatchReviewRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var action = Api.Clean(body.Action, 16).ToLowerInvariant();
            if (action is not ("approve" or "reject"))
                return Api.Error(400, "BAD_ACTION", "action 只能是 approve（通过）或 reject（打回）");

            var ids = body.RecordIds ?? new List<string>();
            if (ids.Count == 0) return Api.Error(400, "EMPTY_SELECTION", "请先勾选要处理的单据");

            var newStatus = action == "approve" ? "已通过" : "已驳回";
            var head = action == "approve" ? "批量审核通过" : "批量审核打回";
            var extra = Api.Clean(body.Opinion, 200);
            var opinion = extra.Length == 0 ? head : $"{head}：{extra}";
            if (opinion.Length > 300) opinion = opinion[..300];

            var result = await invoices.BatchReviewAsync(identity, ids, newStatus, opinion, context.RequestAborted)
                .ConfigureAwait(false);

            if (result.Error.Length > 0) return Api.Error(400, "BATCH_REJECTED", result.Error);

            return Api.Ok(new
            {
                status = newStatus,
                success = result.Success,
                failed = result.Failed,
                message = $"已处理 {result.Items.Count} 单：成功 {result.Success}，失败 {result.Failed}（每单均独立留快照与审计，可单独回退）",
                items = result.Items.Select(i => new { recordId = i.RecordId, ok = i.Ok, error = i.Error, message = i.Message }).ToList(),
            });
        });

        //契约元数据
        group.MapGet("/meta", (HttpContext context, AppConfig cfg) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            return Api.Ok(new
            {
                //在飞书中打开原表
                tableUrl = cfg.Feishu.TableUrl,
                userTableUrl = cfg.Feishu.UserTableUrl,
                feishuConfigured = cfg.FeishuConfigured,

                departments = InvoicePolicy.Departments,
                types = InvoicePolicy.Types,
                invoiceStates = InvoicePolicy.InvoiceStates,
                complianceResults = InvoicePolicy.ComplianceResults,
                statuses = InvoicePolicy.Statuses,
                cities = InvoicePolicy.Cities,
                cityTierCaps = new { tier1 = 500, tier2 = 350, tier3 = 250 },
                amountRange = new { min = InvoicePolicy.MinAmount, max = InvoicePolicy.MaxAmount },
                readonlyFields = InvoicePolicy.ReadOnlyFields,
                serverStampedFields = InvoicePolicy.ServerStampedFields,
                financeEditableFields = InvoicePolicy.FinanceEditableFields,
                rules = new[]
                {
                    "无票属于硬性不合规，提交前阻断",
                    "票据状态未提会记为风险并写入风险摘要",
                    "招待费单次超 2000 元记为风险，需总经理事前审批",
                    "办公用品超 5000 元记为风险，应走采购流程",
                    "差旅费按城市分级校验住宿上限：一线 500、新一线 350、二线 250 元每晚",
                    "开票日期距今超过 30 天记为风险，需附情况说明",
                },
                canSeeAll = InvoicePolicy.CanSeeAll(identity.User.Role),
                canReview = AccountPolicy.Can(identity.User.Role, Perm.InvoiceReview),
                canUpdate = AccountPolicy.Can(identity.User.Role, Perm.InvoiceUpdate),
                canDelete = AccountPolicy.Can(identity.User.Role, Perm.InvoiceDelete),
                canRollback = AccountPolicy.Can(identity.User.Role, Perm.LogRollback),
            });
        });
    }

    private static object ToView(InvoiceRow row) => new
    {
        recordId = row.RecordId,
        billNo = row.Text(InvoicePolicy.FieldBillNo),
        applicant = row.Text(InvoicePolicy.FieldApplicant),
        employeeId = row.Text(InvoicePolicy.FieldEmployeeId),
        department = row.Text(InvoicePolicy.FieldDepartment),
        rtype = row.Text(InvoicePolicy.FieldType),
        amount = row.Amount,
        reason = row.Text(InvoicePolicy.FieldReason),
        hasInvoice = row.Text(InvoicePolicy.FieldInvoiceState),
        city = row.Text(InvoicePolicy.FieldCity),
        invoiceDate = row.Text(InvoicePolicy.FieldInvoiceDate),
        submittedAt = row.Text(InvoicePolicy.FieldSubmittedAt),
        compliance = row.Text(InvoicePolicy.FieldCompliance),
        riskSummary = row.Text(InvoicePolicy.FieldRiskSummary),
        status = row.Text(InvoicePolicy.FieldStatus),
        reviewer = row.Text(InvoicePolicy.FieldReviewer),
        reviewOpinion = row.Text(InvoicePolicy.FieldReviewOpinion),
        reviewedAt = row.Text(InvoicePolicy.FieldReviewedAt),
        invoiceInfo = row.Text(InvoicePolicy.FieldInvoiceInfo),
        raw = row.Fields.ToDictionary(kv => kv.Key, kv => kv.Value.GetRawText()),
    };

    //AI 评分的对外视图（纯只读建议字段）
    private static object AiScoreView(InvoiceRow row, InvoiceAiScore? score) => new
    {
        recordId = row.RecordId,
        billNo = row.Text(InvoicePolicy.FieldBillNo),
        applicant = row.Text(InvoicePolicy.FieldApplicant),
        employeeId = row.Text(InvoicePolicy.FieldEmployeeId),
        department = row.Text(InvoicePolicy.FieldDepartment),
        rtype = row.Text(InvoicePolicy.FieldType),
        amount = row.Amount,
        city = row.Text(InvoicePolicy.FieldCity),
        hasInvoice = row.Text(InvoicePolicy.FieldInvoiceState),
        invoiceDate = row.Text(InvoicePolicy.FieldInvoiceDate),
        reason = row.Text(InvoicePolicy.FieldReason),
        status = row.Text(InvoicePolicy.FieldStatus),
        hasScore = score is not null,
        ruleLevel = score?.RuleLevel ?? 0,
        aiLevel = score?.AiLevel ?? 0,
        finalLevel = score?.FinalLevel ?? 0,
        levelLabel = score is null ? "未评分" : InvoiceAiLevel.Label(score.FinalLevel),
        levelRisk = score is null ? "" : InvoiceAiLevel.ToRisk(score.FinalLevel),
        ruleSummary = score?.RuleSummary ?? "",
        aiReason = score?.AiReason ?? "",
        suggest = score?.Suggest ?? "",
        flags = score?.Flags ?? new List<string>(),
        scoredAt = score?.ScoredAt,
    };

    //AI 选单条目视图
    private static object FindItemView(InvoiceAiFindItem item) => new
    {
        recordId = item.RecordId,
        billNo = item.BillNo,
        summary = item.Summary,
        reason = item.Reason,
        level = item.Level,
        levelLabel = item.Level > 0 ? InvoiceAiLevel.Label(item.Level) : "",
        status = item.Status,
        applicant = item.Applicant,
        amount = item.Amount,
        rtype = item.Type,
        department = item.Department,
        city = item.City,
        hasInvoice = item.InvoiceState,
        droppedReason = item.DroppedReason,
    };

    //后台写操作限流
    private static bool AdminWriteAllowed(SecurityGuard guard, RequestIdentity identity, out string message)    {
        message = "";
        if (guard.TryConsume($"invoicewrite:{identity.User.Id}", guard.AdminWritePerMinute, TimeSpan.FromMinutes(1), identity.Ip)) return true;
        message = "操作过于频繁，请稍后再试";
        return false;
    }
}

public sealed class InvoiceSubmitRequest
{
    public string Department { get; set; } = "";
    public string Rtype { get; set; } = "";
    public decimal Amount { get; set; }
    public string Reason { get; set; } = "";
    public string HasInvoice { get; set; } = "";
    public string City { get; set; } = "";
    public string InvoiceDate { get; set; } = "";
    public string InvoiceInfo { get; set; } = "";
    public string TaskId { get; set; } = "";
}

public sealed class InvoiceReviewRequest
{
    public string RecordId { get; set; } = "";
    public string Status { get; set; } = "";
    public string Opinion { get; set; } = "";
    public string TaskId { get; set; } = "";
}

public sealed class InvoiceUpdateRequest
{
    public string RecordId { get; set; } = "";
    public Dictionary<string, string> Fields { get; set; } = new();
    public string TaskId { get; set; } = "";
}

public sealed class InvoiceDeleteRequest
{
    public string RecordId { get; set; } = "";
    public string Reason { get; set; } = "";
    public string TaskId { get; set; } = "";
}

public sealed class InvoiceRollbackRequest
{
    public string VersionId { get; set; } = "";
    public string TaskId { get; set; } = "";
}

//AI 选单请求
public sealed class InvoiceAiFindRequest
{
    public string Requirement { get; set; } = "";
}

//批量审核请求：只能由财务本人点击提交
public sealed class InvoiceBatchReviewRequest
{
    public List<string>? RecordIds { get; set; }
    public string Action { get; set; } = "";
    public string Opinion { get; set; } = "";
}
