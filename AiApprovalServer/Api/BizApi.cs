using AiApproval.Ai;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Services;
using AiApproval.Tools;

namespace AiApproval.Api;

public sealed class BizActionRequest
{
    public string Tool { get; set; } = "";
    public Dictionary<string, string> Args { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public string Reason { get; set; } = "";
}

/// <summary>
/// 业务对接台接口（甲方 / 乙方业务的读写入口）。
///
/// 两条原则：
///   1) **读**：按角色做数据范围裁剪（甲方只能看到自己公司的档案与需求单；员工/管理员可看全部），
///      并且员工/管理员的批量读取本身也会记审计（客户资料属于商业秘密，批量拉取是要留痕的行为）；
///   2) **写**：全部转交 <see cref="AgentOrchestrator.HandleActionAsync"/>，
///      即"业务界面 → 规则引擎 → AI 安全审核 → 人工审批 → Harness 执行"，
///      **不提供任何直接改 JSON 数据的旁路**。
/// </summary>
public static class BizApi
{
    public static void Map(WebApplication app)
    {
        var group = app.MapGroup("/api/biz");

        group.MapGet("/overview", async (HttpContext context, DatasetStore datasets, TaskService tasks,
            AiClient ai, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var role = identity.User.Role;
            var isStaff = identity.IsStaffOrAbove;

            var clients = isStaff ? await datasets.RowsAsync("customers").ConfigureAwait(false) : new List<Dictionary<string, string>>();
            var requirements = isStaff ? await datasets.RowsAsync("requirements").ConfigureAwait(false) : new List<Dictionary<string, string>>();

            Dictionary<string, string>? selfClient = null;
            var myRequirements = new List<Dictionary<string, string>>();

            if (role == Roles.User)
            {
                selfClient = await datasets.FindByFieldAsync("customers", "account_user_id", identity.User.Id).ConfigureAwait(false);
                if (selfClient is not null)
                    myRequirements = await datasets.ListByFieldAsync("requirements", "customer_id", Pick(selfClient, "id"), 50).ConfigureAwait(false);
            }

            if (isStaff && clients.Count > 0)
            {
                await audit.WriteAsync(identity.User.Id, identity.User.UserName, role, Api.Ip(context),
                    "biz.read", "customers/requirements", "SUCCESS",
                    $"业务对接台读取客户 {clients.Count} 条、需求单 {requirements.Count} 条").ConfigureAwait(false);
            }

            var pending = await tasks.ListAsync(null, TaskState.PendingApproval, 100).ConfigureAwait(false);
            var myPending = pending.Count(t => t.UserId == identity.User.Id);

            return Api.Ok(new
            {
                role,
                roleLabel = Roles.Label(role),
                aiStatus = ai.Status,
                aiLastError = ai.IsConfigured ? ai.LastError : "",
                self = new
                {
                    linked = selfClient is not null,
                    client = selfClient,
                },
                myRequirements,
                clients = isStaff ? clients.Take(100).ToList() : new List<Dictionary<string, string>>(),
                requirements = isStaff ? requirements.Take(200).ToList() : new List<Dictionary<string, string>>(),
                stats = role == Roles.Admin
                    ? new
                    {
                        clientCount = clients.Count,
                        requirementCount = requirements.Count,
                        pendingCount = pending.Count,
                        myPendingCount = myPending,
                    }
                    : null,
            });
        });

        group.MapPost("/action", async (HttpContext context, AgentOrchestrator agent, SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var ip = Api.Ip(context);
            if (!guard.TryConsume($"biz:user:{identity.User.Id}", guard.BizActionPerFiveMinutesPerUser, TimeSpan.FromMinutes(5))
                || !guard.TryConsume($"biz:ip:{ip}", guard.BizActionPerFiveMinutesPerIp, TimeSpan.FromMinutes(5)))
            {
                await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, ip,
                    "biz.rate-limit", "action", "BLOCKED", "业务动作提交过于频繁").ConfigureAwait(false);
                return Api.Error(429, "RATE_LIMITED", "操作过于频繁，请稍后再试");
            }

            var body = Api.ReadBody<BizActionRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var tool = Api.Clean(body.Tool, 64);
            if (tool.Length == 0) return Api.Error(400, "BAD_REQUEST", "缺少 tool");

            if (body.Args.Count > 12) return Api.Error(400, "TOO_MANY_ARGS", "参数过多");

            var args = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var kv in body.Args)
            {
                var key = Api.Clean(kv.Key, 40);
                if (key.Length == 0) continue;
                args[key] = Api.Clean(kv.Value, 2000);
            }

            var reason = Api.Clean(body.Reason, 200);

            await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, ip,
                "biz.action", tool, "SUBMIT",
                $"参数={AppJson.Serialize(args)}{(reason.Length > 0 ? " 理由=" + reason : "")}").ConfigureAwait(false);

            AgentOutcome outcome;
            try
            {
                outcome = await agent.HandleActionAsync(identity, tool, args, reason, context.RequestAborted).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return Api.Error(499, "CANCELLED", "请求已取消");
            }

            return Api.Ok(new
            {
                sessionId = outcome.SessionId,
                reply = outcome.Reply,
                source = outcome.Source,
                status = outcome.Status,
                statusLabel = outcome.StatusLabel,
                taskId = outcome.TaskId,
                tool = outcome.Tool,
                intent = outcome.Intent,
                riskLevel = outcome.RiskLevel,
                riskLabel = Risk.Label(outcome.RiskLevel),
                actionSummary = outcome.ActionSummary,
                reviewVerdict = outcome.ReviewVerdict,
                injectionFlags = outcome.InjectionFlags,
                notes = outcome.Notes,
                result = outcome.Result,
                steps = outcome.Steps.Select(s => new
                {
                    index = s.Index,
                    tool = s.Tool,
                    title = s.Title,
                    summary = s.Summary,
                    status = s.Status,
                    elapsedMs = s.ElapsedMs,
                }).ToList(),
                translations = outcome.Translations,
            });
        });
    }

    private static string Pick(Dictionary<string, string> row, string field)
    {
        var kv = row.FirstOrDefault(x => string.Equals(x.Key, field, StringComparison.OrdinalIgnoreCase));
        return kv.Key is null ? "" : kv.Value;
    }
}
