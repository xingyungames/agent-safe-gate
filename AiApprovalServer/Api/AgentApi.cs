using AiApproval.Ai;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Services;
using AiApproval.Tools;

namespace AiApproval.Api;

/// <summary>
/// 业务侧接口（用户 / 员工）。
///
/// 注意：连"浏览自己的文件列表"这种只读 UI 操作，也走同一条
/// 规则引擎 + Harness 通道（只是不落任务、不需要审批），
/// 避免出现"界面用的旁路接口"绕过权限校验这种典型漏洞。
/// </summary>
public static class AgentApi
{
    public static void Map(WebApplication app)
    {
        var group = app.MapGroup("/api/agent");

        group.MapGet("/tools", (HttpContext context) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var tools = ToolCatalog.ForRole(identity.User.Role)
                .Where(t => !t.AdminOnly)
                .Select(t => new
                {
                    name = t.Name,
                    title = t.Title,
                    description = t.Description,
                    risk = t.RiskLevel,
                    riskLabel = Risk.Label(t.RiskLevel),
                    requiresApproval = t.RequiresApproval,
                    stateChanging = t.StateChanging,
                    permissions = t.Permissions.Select(p => Permissions.Label(p)).ToList(),
                    @params = t.Params,
                    required = t.RequiredParams,
                })
                .ToList();

            return Api.Ok(new
            {
                role = identity.User.Role,
                roleLabel = Roles.Label(identity.User.Role),
                permissions = ToolCatalog.PermissionsOf(identity.User.Role)
                    .Select(p => new { key = p, label = Permissions.Label(p) }).ToList(),
                tools,
            });
        });

        group.MapGet("/help", () => Api.Ok(new
        {
            localInstructions = LocalIntentParser.HelpText(),
            examples = new[]
            {
                "列出我的文件",
                "读取 draft/客户名单.txt",
                "写入 note.txt 内容：今天的工作记录",
                "删除 note.txt 原因：内容已过期",
                "查询客户 等级=黄金",
                "修改客户 C1003 等级 改为 铂金",
                "删除客户 C1003 原因：重复录入",
                "通知我 标题：周报 内容：本周进展如下",
                "我的任务",
            },
        }));

        group.MapPost("/chat", ChatAsync);

        group.MapGet("/tasks", async (HttpContext context, TaskService tasks) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var list = await tasks.ListAsync(identity.User.Id, null, 100);
            return Api.Ok(list.Select(tasks.ToUserView).ToList());
        });

        group.MapGet("/tasks/{id}", async (HttpContext context, string id, TaskService tasks) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var task = await tasks.GetAsync(Api.Clean(id, 64));
            if (task is null) return Api.Error(404, "NOT_FOUND", "任务不存在");

            // 水平越权防护：只能看自己的任务（管理员走管理端接口）
            if (task.UserId != identity.User.Id && !identity.IsAdmin)
                return Api.Error(403, "FORBIDDEN", "无权查看该任务");

            return Api.Ok(tasks.ToUserView(task));
        });

        group.MapGet("/files", async (HttpContext context, RuleEngine rules, Harness harness,
            string? scope, string? path) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var args = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["scope"] = scope ?? "private",
                ["path"] = path ?? "",
            };

            return await RunReadOnlyAsync(identity, "list_files", args, rules, harness, context).ConfigureAwait(false);
        });

        group.MapGet("/files/content", async (HttpContext context, RuleEngine rules, Harness harness,
            string? scope, string path) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var args = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["scope"] = scope ?? "private",
                ["path"] = path ?? "",
            };

            return await RunReadOnlyAsync(identity, "read_file", args, rules, harness, context).ConfigureAwait(false);
        });

        group.MapGet("/notices", async (HttpContext context, Outbox outbox) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var list = await outbox.ListAsync(identity.User.Id, 50);
            return Api.Ok(list.Select(m => new
            {
                id = m.Id,
                createdAt = m.CreatedAt,
                subject = m.Subject,
                body = m.Body,
                status = m.Status,
                taskId = m.TaskId,
                sentAt = m.SentAt,
                error = m.Error,
            }).ToList());
        });

        group.MapGet("/versions", async (HttpContext context, VersionStore versions) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var list = await versions.ListAsync(identity.User.Id, 100);
            return Api.Ok(list.Select(v => new
            {
                id = v.Id,
                taskId = v.TaskId,
                kind = v.Kind,
                operation = v.Operation,
                target = v.Target,
                createdAt = v.CreatedAt,
                addedLines = v.AddedLines,
                removedLines = v.RemovedLines,
                diff = v.DiffText,
                reverted = v.Reverted,
                revertedBy = v.RevertedBy,
            }).ToList());
        });

        group.MapPost("/session/reset", (HttpContext context, ChatSessionStore sessions) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            sessions.Reset(identity.User.Id, null);
            return Api.Ok(new { message = "会话上下文已清空" });
        });
    }

    private static async Task<IResult> ChatAsync(HttpContext context, AgentOrchestrator agent, SecurityGuard guard,
        AppConfig cfg, AuditLog audit)
    {
        var identity = Api.Identity(context);
        if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

        var ip = Api.Ip(context);
        if (!guard.TryConsume($"chat:user:{identity.User.Id}", cfg.Limits.AgentChatPerFiveMinutesPerUser, TimeSpan.FromMinutes(5))
            || !guard.TryConsume($"chat:ip:{ip}", cfg.Limits.AgentChatPerFiveMinutesPerIp, TimeSpan.FromMinutes(5)))
        {
            await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, ip,
                "agent.rate-limit", "chat", "BLOCKED", "对话频率超限（防 AI 额度被刷）");
            return Api.Error(429, "RATE_LIMITED", "请求过于频繁，请稍后再试");
        }

        var body = Api.ReadBody<ChatRequest>(context);
        if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

        if (string.IsNullOrWhiteSpace(body.Message))
            return Api.Error(400, "EMPTY_MESSAGE", "请输入内容");

        AgentOutcome outcome;
        try
        {
            outcome = await agent.HandleAsync(identity, body.Message, Api.Clean(body.SessionId, 40), context.RequestAborted);
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
            // 多步执行轨迹：前端据此展示"先搜 → 再读 → 再写"的过程
            steps = outcome.Steps.Select(s => new
            {
                index = s.Index,
                tool = s.Tool,
                title = s.Title,
                args = AppJson.Deserialize<object>(s.ArgsJson),
                summary = s.Summary,
                status = s.Status,
                result = AppJson.Deserialize<object>(s.ResultJson),
                elapsedMs = s.ElapsedMs,
            }).ToList(),
            translations = outcome.Translations,
            stepLimitReached = outcome.StepLimitReached,
        });
    }

    /// <summary>只读工具直通（界面用）：规则引擎照常校验，但不产生任务/审批。</summary>
    private static async Task<IResult> RunReadOnlyAsync(RequestIdentity identity, string toolName,
        Dictionary<string, string> args, RuleEngine rules, Harness harness, HttpContext context)
    {
        var eval = await rules.EvaluateAsync(identity.User.Role, identity.User.Id, toolName, args, injectionSuspected: false)
            .ConfigureAwait(false);

        if (eval.Outcome.Decision == "DENY")
            return Api.Error(403, "RULE_DENIED", string.Join("；", eval.Outcome.Reasons));

        var tool = ToolCatalog.Get(toolName);
        if (tool is null || tool.StateChanging)
            return Api.Error(400, "BAD_TOOL", "该接口只允许只读工具");

        var transient = new AgentTask
        {
            Id = "ui_" + Crypto.RandomHex(4),
            UserId = identity.User.Id,
            UserName = identity.User.UserName,
            UserRole = identity.User.Role,
            ToolName = toolName,
            Rule = eval.Outcome,
        };

        var result = await harness.ExecuteAsync(transient, context.RequestAborted).ConfigureAwait(false);
        if (!result.Ok) return Api.Error(400, "EXECUTE_FAILED", result.Summary);

        return Api.Ok(new { summary = result.Summary, data = result.Data });
    }
}
