using AiApproval.Ai;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Services;
using AiApproval.Tools;

namespace AiApproval.Api;

/// <summary>
/// 管理端接口（全部挂在 /api/admin/ 下，中间件已强制"必须管理员 + 非管理员访问直接记分封禁"）。
///
/// 审批权限始终握在"人"手里：
///   * AI 只能生成风险摘要与建议，没有任何批准/执行接口；
///   * 批准动作需要管理员令牌 + 请求签名，邮件链接还需要一次性签名令牌；
///   * 每一次裁決、回滚、用户管理都写入哈希链审计。
/// </summary>
public static class AdminApi
{
    public static void Map(WebApplication app)
    {
        var group = app.MapGroup("/api/admin");

        group.MapGet("/overview", async (HttpContext context, TaskService tasks, UserService users,
            VersionStore versions, Outbox outbox, AuditLog audit, SecurityGuard guard, AiClient ai,
            DatasetStore datasets) =>
        {
            var all = await tasks.ListAsync(null, null, 300);
            var pending = all.Where(t => t.Status == TaskState.PendingApproval).OrderByDescending(t => t.CreatedAt).ToList();
            var userList = await users.AllAsync();

            var chain = await audit.VerifyChainAsync();

            return Api.Ok(new
            {
                pendingCount = pending.Count,
                executingCount = all.Count(t => t.Status == TaskState.Executing),
                executedCount = all.Count(t => t.Status == TaskState.Executed),
                rejectedCount = all.Count(t => TaskState.IsRejected(t.Status)),
                failedCount = all.Count(t => t.Status == TaskState.Failed),
                userCount = userList.Count,
                versionCount = await versions.CountAsync(),
                auditCount = chain.Checked,
                auditChainOk = chain.Ok,
                auditChainMessage = chain.Message,
                outboxCount = (await outbox.ListAsync(null, 300)).Count,
                aiConfigured = ai.IsConfigured,
                aiModel = ai.Model,
                security = guard.Snapshot(),
                datasets = DataSetSummary(datasets),
                newestPending = pending.Take(5).Select(tasks.ToAdminView).ToList(),
            });
        });

        group.MapGet("/tasks", async (HttpContext context, TaskService tasks, string? status, string? userId, int? limit) =>
        {
            await tasks.ExpireOverdueAsync();
            var list = await tasks.ListAsync(Api.Clean(userId, 64), Api.Clean(status, 32), limit ?? 100);
            return Api.Ok(list.Select(tasks.ToAdminView).ToList());
        });

        group.MapGet("/tasks/{id}", async (HttpContext context, string id, TaskService tasks, VersionStore versions) =>
        {
            var task = await tasks.GetAsync(Api.Clean(id, 64));
            if (task is null) return Api.Error(404, "NOT_FOUND", "任务不存在");

            var relatedVersions = (await versions.ListAsync(null, 300))
                .Where(v => task.VersionIds.Contains(v.Id))
                .Select(VersionView)
                .ToList();

            return Api.Ok(new { task = tasks.ToAdminView(task), versions = relatedVersions });
        });

        group.MapPost("/tasks/{id}/approve", async (HttpContext context, string id, TaskService tasks,
            SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<ReasonRequest>(context) ?? new ReasonRequest();
            var reason = Api.Clean(body.Reason, 200);
            if (reason.Length < 2) return Api.Error(400, "REASON_REQUIRED", "请填写审批意见（不少于 2 个字符）");

            var (ok, error, task) = await tasks.DecideAsync(Api.Clean(id, 64), true, reason, identity, "admin-ui");
            if (!ok) return Api.Error(409, "CONFLICT", error);

            return Api.Ok(new { message = "已批准并执行（执行前已保存快照，可一键回退）", task = tasks.ToAdminView(task!) });
        });

        group.MapPost("/tasks/{id}/reject", async (HttpContext context, string id, TaskService tasks,
            SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<ReasonRequest>(context) ?? new ReasonRequest();
            var reason = Api.Clean(body.Reason, 200);
            if (reason.Length < 2) return Api.Error(400, "REASON_REQUIRED", "请填写打回理由（不少于 2 个字符）");

            var (ok, error, task) = await tasks.DecideAsync(Api.Clean(id, 64), false, reason, identity, "admin-ui");
            if (!ok) return Api.Error(409, "CONFLICT", error);

            return Api.Ok(new { message = "已打回", task = tasks.ToAdminView(task!) });
        });

        group.MapPost("/tasks/approve-by-token", async (HttpContext context, TaskService tasks, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var body = Api.ReadBody<ApproveByTokenRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var reason = Api.Clean(body.Reason, 200);
            if (reason.Length < 2) reason = "通过邮件签名链接批准";

            var (ok, error) = await tasks.DecideByTokenAsync(Api.Clean(body.TaskId, 64), body.Token ?? "", reason, identity);
            if (!ok) return Api.Error(403, "TOKEN_REJECTED", error);

            var task = await tasks.GetAsync(Api.Clean(body.TaskId, 64));
            await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                "approval.token-used", Api.Clean(body.TaskId, 64), "SUCCESS", "通过邮件签名令牌完成审批");

            return Api.Ok(new { message = "已通过签名令牌批准并执行", task = task is null ? null : tasks.ToAdminView(task) });
        });

        group.MapPost("/tasks/expire", async (HttpContext context, TaskService tasks) =>
        {
            var count = await tasks.ExpireOverdueAsync();
            return Api.Ok(new { message = $"已作废 {count} 个超时任务", expired = count });
        });

        // ---------------------------------------------------------------- 用户管理
        group.MapGet("/users", async (HttpContext context, UserService users) =>
        {
            var list = await users.AllAsync();
            return Api.Ok(list.Select(u => new
            {
                id = u.Id,
                userName = u.UserName,
                displayName = u.DisplayName,
                role = u.Role,
                roleLabel = Roles.Label(u.Role),
                emailMasked = u.EmailMasked,
                disabled = u.Disabled,
                locked = u.IsLocked,
                lockedUntil = u.LockedUntil,
                failedLoginCount = u.FailedLoginCount,
                createdAt = u.CreatedAt,
                createdBy = u.CreatedBy,
                lastLoginAt = u.LastLoginAt,
                lastLoginIp = u.LastLoginIp,
                aiCallsToday = u.AiCallsToday,
            }).OrderBy(u => u.userName).ToList());
        });

        group.MapPost("/users", async (HttpContext context, UserService users, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<UserAdminRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var role = Api.Clean(body.Role, 16).ToLowerInvariant();
            if (!Roles.IsValid(role)) return Api.Error(400, "BAD_ROLE", "角色只能是 user / staff / admin");

            var (user, error) = await users.CreateAsync(Api.Clean(body.UserName, 32), body.Password ?? "",
                Api.Clean(body.DisplayName, 32), role, Api.Clean(body.Email, 128), identity.User.UserName);

            if (user is null) return Api.Error(400, "CREATE_FAILED", error);

            return Api.Ok(new
            {
                message = $"已创建账号 {user.UserName}（{Roles.Label(user.Role)}）",
                user = new { user.Id, user.UserName, user.DisplayName, user.Role, roleLabel = Roles.Label(user.Role) },
            });
        });

        group.MapPost("/users/{id}/role", async (HttpContext context, string id, UserService users, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<ChangeRoleRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var role = Api.Clean(body.Role, 16).ToLowerInvariant();
            if (!Roles.IsValid(role)) return Api.Error(400, "BAD_ROLE", "角色非法");

            var targetUserId = Api.Clean(id, 64);
            if (targetUserId == identity.User.Id && role != Roles.Admin)
                return Api.Error(400, "SELF_DEMOTE", "不能降级自己的管理员角色（请让另一位管理员操作）");

            var ok = await users.SetRoleAsync(targetUserId, role, identity.User.UserName, Api.Ip(context));
            if (!ok) return Api.Error(404, "NOT_FOUND", "账号不存在");

            return Api.Ok(new { message = $"角色已更新为 {Roles.Label(role)}，该账号的旧令牌已失效" });
        });

        group.MapPost("/users/{id}/status", async (HttpContext context, string id, UserService users, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<DisableRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var targetUserId = Api.Clean(id, 64);
            if (targetUserId == identity.User.Id && body.Disabled)
                return Api.Error(400, "SELF_DISABLE", "不能禁用自己的账号");

            var ok = await users.SetDisabledAsync(targetUserId, body.Disabled, identity.User.UserName, Api.Ip(context));
            if (!ok) return Api.Error(404, "NOT_FOUND", "账号不存在");

            return Api.Ok(new { message = body.Disabled ? "账号已禁用（令牌立即失效）" : "账号已启用" });
        });

        group.MapPost("/users/{id}/reset-password", async (HttpContext context, string id, UserService users, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<ResetPasswordRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var ok = await users.ChangePasswordAsync(Api.Clean(id, 64), body.NewPassword ?? "",
                identity.User.UserName, Api.Ip(context));
            if (!ok) return Api.Error(400, "RESET_FAILED", "重置失败（口令不满足强度要求或账号不存在）");

            return Api.Ok(new { message = "口令已重置，该账号旧令牌全部失效" });
        });

        // ---------------------------------------------------------------- 审计
        group.MapGet("/audit", async (HttpContext context, AuditLog audit, string? action, string? actor, int? limit, int? offset) =>
        {
            var list = await audit.ReadAsync(limit ?? 200, offset ?? 0, Api.Clean(action, 40), Api.Clean(actor, 64));
            return Api.Ok(list.Select(e => new
            {
                seq = e.Seq,
                time = e.Time,
                actorName = e.ActorName,
                actorRole = e.ActorRole,
                ip = e.Ip,
                action = e.Action,
                target = e.Target,
                outcome = e.Outcome,
                detail = e.Detail,
                hash = e.Hash[..12],
                prevHash = e.PrevHash[..12],
            }).ToList());
        });

        group.MapGet("/audit/verify", async (AuditLog audit) =>
        {
            var (ok, count, brokenAt, message) = await audit.VerifyChainAsync();
            return Api.Ok(new { ok, checkedCount = count, brokenAt, message });
        });

        // ---------------------------------------------------------------- 版本与回退
        group.MapGet("/versions", async (HttpContext context, VersionStore versions, string? userId, int? limit) =>
        {
            var list = await versions.ListAsync(Api.Clean(userId, 64), limit ?? 100);
            return Api.Ok(list.Select(VersionView).ToList());
        });

        group.MapPost("/versions/{id}/rollback", async (HttpContext context, string id, VersionStore versions,
            Harness harness, JsonStore store, TaskService tasks, AuditLog audit, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<ReasonRequest>(context) ?? new ReasonRequest();
            var reason = Api.Clean(body.Reason, 200);
            if (reason.Length < 2) return Api.Error(400, "REASON_REQUIRED", "请填写回退原因（不少于 2 个字符）");

            var version = await versions.GetAsync(Api.Clean(id, 64));
            if (version is null) return Api.Error(404, "NOT_FOUND", "版本记录不存在");
            if (version.Reverted) return Api.Error(409, "ALREADY_REVERTED", "该版本已经回退过");

            var rollbackTaskId = "t_" + Crypto.RandomHex(8);

            // 回退本身也建一条任务：审计闭环里"谁在什么时候把什么退回了什么状态"必须可查
            await store.MutateAsync<AgentTask, bool>(StoreNames.Tasks, list =>
            {
                list.Add(new AgentTask
                {
                    Id = rollbackTaskId,
                    UserId = version.UserId,
                    UserName = version.UserName,
                    UserRole = Roles.Admin,
                    Intent = $"回退版本 {version.Id}（{version.Operation} {version.Target}）",
                    ToolName = "admin_rollback",
                    ActionJson = AppJson.Serialize(new { tool = "admin_rollback", args = new { versionId = version.Id, reason } }),
                    RiskLevel = Risk.High,
                    Status = TaskState.Executing,
                    StatusReason = $"由 {identity.User.UserName} 发起回退",
                    CreatedAt = DateTime.UtcNow,
                    DecidedAt = DateTime.UtcNow,
                    DecidedBy = identity.User.UserName,
                    DecisionReason = reason,
                    RollbackOfVersionId = version.Id,
                    Rule = new RuleOutcome { Decision = "ALLOW", RequiresApproval = false, RiskLevel = Risk.High, Reasons = { "管理员发起的回退（本身可被再次回退）" } },
                    Review = new ReviewOutcome { Verdict = "admin-action", Available = true, Reason = "管理员直接操作，不经过 AI 审核" },
                });
                return true;
            });

            var (ok, message, newVersionId, data) = await harness.RollbackAsync(
                version, identity.User.Id, identity.User.UserName, identity.User.Role, rollbackTaskId);

            await store.MutateAsync<AgentTask, bool>(StoreNames.Tasks, list =>
            {
                var task = list.FirstOrDefault(t => t.Id == rollbackTaskId);
                if (task is null) return false;
                task.Status = ok ? TaskState.Executed : TaskState.Failed;
                task.ExecutedAt = DateTime.UtcNow;
                task.ResultJson = AppJson.Serialize(new { ok, summary = message, data }, disk: true);
                if (!string.IsNullOrEmpty(newVersionId)) task.VersionIds.Add(newVersionId);
                return true;
            });

            var originalTask = await tasks.GetAsync(version.TaskId);
            if (ok && originalTask is not null)
            {
                await store.MutateAsync<AgentTask, bool>(StoreNames.Tasks, list =>
                {
                    var task = list.FirstOrDefault(t => t.Id == originalTask.Id);
                    if (task is null) return false;
                    task.RolledBack = true;
                    task.RolledBackAt = DateTime.UtcNow;
                    task.RolledBackBy = identity.User.UserName;
                    return true;
                });
            }

            await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                "admin.rollback", version.Id, ok ? "SUCCESS" : "FAILED", $"{message}；原因：{reason}");

            if (!ok) return Api.Error(400, "ROLLBACK_FAILED", message);

            return Api.Ok(new
            {
                message,
                taskId = rollbackTaskId,
                newVersionId,
                data,
                note = "回退操作本身也已保存快照，可再次回退（撤销误回退）",
            });
        });

        // ---------------------------------------------------------------- 数据与出站箱
        group.MapGet("/datasets/{name}", async (HttpContext context, DatasetStore datasets, string name, int? limit) =>
        {
            if (!DatasetCatalog.IsKnown(name)) return Api.Error(404, "NOT_FOUND", "未知数据集");

            var rows = await datasets.RowsAsync(name.ToLowerInvariant());
            var (ok, error, queried, query) = await datasets.QueryAsync(name.ToLowerInvariant(), null, null, null, limit ?? 50);
            if (!ok) return Api.Error(400, "QUERY_FAILED", error);

            return Api.Ok(new
            {
                dataset = name.ToLowerInvariant(),
                label = DatasetCatalog.All[name].Label,
                query,
                totalRows = rows.Count,
                fields = DatasetCatalog.All[name].Fields.Select(f => new
                {
                    name = f.Name,
                    label = f.Label,
                    type = f.Type,
                    writable = f.Writable,
                    enumValues = f.EnumValues,
                    maxLength = f.MaxLength,
                }),
                rows = queried,
            });
        });

        group.MapGet("/outbox", async (HttpContext context, Outbox outbox, int? limit) =>
        {
            var list = await outbox.ListAsync(null, limit ?? 100);
            return Api.Ok(list.Select(m => new
            {
                id = m.Id,
                createdAt = m.CreatedAt,
                to = Crypto.Mask(m.To, 2, 6),
                subject = m.Subject,
                body = m.Body,
                status = m.Status,
                taskId = m.TaskId,
                ownerUserName = m.OwnerUserName,
                approvalLink = m.ApprovalLink,
                sentAt = m.SentAt,
                error = m.Error,
                compensatedAt = m.CompensatedAt,
            }).ToList());
        });

        group.MapPost("/outbox/{id}/recall", async (HttpContext context, string id, Outbox outbox, SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var ok = await outbox.RecallAsync(Api.Clean(id, 64), identity.User.UserName);
            if (!ok) return Api.Error(404, "NOT_FOUND", "通知不存在或已撤回");

            return Api.Ok(new { message = "已执行补偿事务：通知标记为已撤回" });
        });

        // ---------------------------------------------------------------- 安全态势
        group.MapGet("/security", (HttpContext context, SecurityGuard guard, AppConfig cfg) => Api.Ok(new
        {
            snapshot = guard.Snapshot(),
            limits = new
            {
                loginPerFiveMinutesPerIp = cfg.Limits.LoginPerFiveMinutesPerIp,
                loginPerFifteenMinutesPerAccount = cfg.Limits.LoginPerFifteenMinutesPerAccount,
                accountLockMinutes = cfg.Limits.AccountLockMinutes,
                agentChatPerFiveMinutesPerUser = cfg.Limits.AgentChatPerFiveMinutesPerUser,
                wafStrikesBeforeBan = cfg.Limits.WafStrikesBeforeBan,
                honeypotStrikesBeforeBan = cfg.Limits.HoneypotStrikesBeforeBan,
                banMinutes = cfg.Limits.BanMinutes,
            },
            defenses = new[]
            {
                "JWT(HS256) + 令牌世代失效",
                "请求签名 HMAC-SHA256 + 时间戳 + 一次性随机数（Anti-Replay）",
                "来源校验（Origin）+ 无 Cookie 认证（天然抗 CSRF）",
                "路径/查询串/请求头 WAF 特征扫描（含 ReDoS 超时防护）",
                "蜜罐路径 + 诱饵后台 + 自动封禁",
                "账号维度失败锁定 + IP 维度限流",
                "规则引擎硬白名单（工具/参数/路径/收件人/字段）",
                "AI 审核物理隔离（不接触用户原始输入）",
                "审批链接 HMAC 签名 + 一次性 + 过期",
                "审计日志哈希链（可校验篡改）",
                "AES-256-GCM 加密敏感字段（用户原始输入、邮箱）",
                "CSP / nosniff / DENY 框架 / no-referrer 等安全响应头",
            },
        }));

        group.MapPost("/security/unban", async (HttpContext context, SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");

            var body = Api.ReadBody<UnbanRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var ip = Api.Clean(body.Ip, 64);
            var ok = guard.Unban(ip);
            await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                "admin.unban", ip, ok ? "SUCCESS" : "NOT_FOUND", "管理员手动解封");

            return ok ? Api.Ok(new { message = $"已解封 {ip}" }) : Api.Error(404, "NOT_FOUND", "该 IP 不在封禁列表中");
        });
    }

    private static bool AdminWriteAllowed(SecurityGuard guard, RequestIdentity identity, out string message)
    {
        message = "";
        if (guard.TryConsume($"adminwrite:{identity.User.Id}", guard.AdminWritePerMinute, TimeSpan.FromMinutes(1))) return true;
        message = "管理操作过于频繁，请稍后再试";
        return false;
    }

    private static object VersionView(VersionRecord v) => new
    {
        id = v.Id,
        taskId = v.TaskId,
        userId = v.UserId,
        userName = v.UserName,
        kind = v.Kind,
        operation = v.Operation,
        target = v.Target,
        previousExisted = v.PreviousExisted,
        previousHash = v.PreviousHash,
        previousSize = v.PreviousSize,
        newSize = v.NewSize,
        addedLines = v.AddedLines,
        removedLines = v.RemovedLines,
        diff = v.DiffText,
        createdAt = v.CreatedAt,
        reverted = v.Reverted,
        revertedAt = v.RevertedAt,
        revertedBy = v.RevertedBy,
        compensateAction = v.CompensateAction,
    };

    private static List<object> DataSetSummary(DatasetStore datasets)
        => DatasetCatalog.All.Keys
            .Select(name => (object)new
            {
                name,
                label = DatasetCatalog.All[name].Label,
                fieldCount = DatasetCatalog.All[name].Fields.Count,
            })
            .ToList();
}

public sealed class DisableRequest
{
    public bool Disabled { get; set; }
}
