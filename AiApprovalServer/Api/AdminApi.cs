using AiApproval.Ai;
using AiApproval.Core;
using AiApproval.Security;
using AiApproval.Services;
using AiApproval.Tools;

namespace AiApproval.Api;

//管理端接口，均需管理员
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

        //用户管理
        group.MapGet("/users", async (HttpContext context, UserService users) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.AccountList))
                return Api.Error(403, "FORBIDDEN", "当前角色无权查看账号列表");

            var list = await users.AllAsync();
            return Api.Ok(list.Select(u => new
            {
                id = u.Id,
                userName = u.UserName,
                displayName = u.DisplayName,
                employeeId = u.EmployeeId,
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
                //账号来源
                origin = u.Origin,
                //口令方案与迭代次数
                passwordScheme = string.IsNullOrEmpty(u.PasswordHash)
                    ? "none"
                    : string.IsNullOrEmpty(u.PasswordScheme) ? "pbkdf2-sha256" : u.PasswordScheme,
                passwordIterations = u.PasswordIterations,
                //飞书镜像状态
                mirrorState = u.MirrorState,
                mirrorError = u.MirrorError,
                mirrorAt = u.MirrorAt,
                mirrorRoleLabel = Roles.ToFeishuLabel(u.Role),
            }).OrderBy(u => u.employeeId).ToList());
        });

        group.MapGet("/users/{id}/mirror-password", async (HttpContext context, string id, UserService users, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.AccountManage))
                return Api.Error(403, "FORBIDDEN", "当前角色无权查看演示口令");

            var user = await users.FindByIdAsync(Api.Clean(id, 64));
            if (user is null) return Api.Error(404, "NOT_FOUND", "账号不存在");

            var demo = users.RevealDemoPassword(user);
            await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                "user.mirror-password", user.UserName, "SUCCESS", "读取飞书镜像演示口令");

            return Api.Ok(new
            {
                userName = user.UserName,
                demoPassword = demo ?? "",
                hint = demo is null
                    ? "该账号没有演示口令（镜像模式为 real/none 或尚未镜像）"
                    : "这是写进飞书用户表的演示口令，可交付协作方登录他们的前端；与用户在本系统的真实口令无关",
            });
        });

        //飞书用户表只读预览
        group.MapGet("/users/feishu-rows", async (HttpContext context, FeishuMirror mirror, UserService users) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AccountPolicy.Can(identity.User.Role, Perm.AccountList))
                return Api.Error(403, "FORBIDDEN", "当前角色无权查看账号列表");
            if (!mirror.Enabled)
                return Api.Ok(new { configured = false, rows = Array.Empty<object>(), message = "未配置飞书，无法读取对面的用户表" });

            var rows = await mirror.ListAccountRowsAsync();
            var local = (await users.AllAsync()).Select(u => u.UserName)
                .ToHashSet(StringComparer.OrdinalIgnoreCase);

            return Api.Ok(new
            {
                configured = true,
                count = rows.Count,
                //不回口令哈希
                rows = rows.Select(r => new
                {
                    recordId = r.RecordId,
                    userName = r.UserName,
                    displayName = r.DisplayName,
                    employeeId = r.EmployeeId,
                    roleLabel = r.RoleLabel,
                    statusLabel = r.StatusLabel,
                    hasPassword = r.HasPassword,
                    existsLocally = local.Contains(r.UserName),
                    createdAt = r.CreatedAt,
                    lastLoginAt = r.LastLoginAt,
                }).ToList(),
            });
        });

        //同步：只补对面新增账号
        group.MapPost("/users/sync-from-feishu", async (HttpContext context, UserService users,
            SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            if (!AccountPolicy.Can(identity.User.Role, Perm.AccountCreate))
            {
                await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                    "user.sync-feishu", "用户表", "DENIED", "当前角色无创建账号权限，不能执行同步").ConfigureAwait(false);
                return Api.Error(403, "FORBIDDEN", "当前角色无权创建账号，不能执行同步");
            }

            var body = Api.ReadBody<FeishuSyncRequest>(context) ?? new FeishuSyncRequest();
            var report = await users.ImportFromFeishuAsync(identity.User.Role, identity.User.UserName, Api.Ip(context), body.DryRun);

            if (!report.Ok) return Api.Error(409, "SYNC_UNAVAILABLE", report.Message);

            return Api.Ok(new
            {
                dryRun = report.DryRun,
                message = (report.DryRun ? "预览结果（未写入）：" : "") + report.Message
                          + (Api.Clean(body.Reason, 200) is { Length: > 0 } why ? $"；理由：{why}" : ""),
                read = report.Read,
                created = report.Created,
                adopted = report.Adopted,
                exists = report.Exists,
                skipped = report.Skipped,
                needsPasswordReset = report.NeedsPasswordReset,
                items = report.Items.Select(i => new
                {
                    userName = i.UserName,
                    displayName = i.DisplayName,
                    employeeId = i.EmployeeId,
                    roleLabel = i.Role,
                    action = i.Action,
                    detail = i.Detail,
                    needsPasswordReset = i.NeedsPasswordReset,
                }).ToList(),
            });
        });

        group.MapPost("/users", async (HttpContext context, UserService users, SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<UserAdminRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var role = (Api.Clean(body.Role, 16).ToLowerInvariant() is { Length: > 0 } r) ? r : Roles.Employee;
            var policyError = AccountPolicy.CanCreateAccount(identity.User.Role, role);
            if (policyError is not null)
            {
                await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                    "user.create", Api.Clean(body.UserName, 32), "DENIED", policyError);
                guard.Strike(Api.Ip(context), $"越权创建账号（{identity.User.UserName}）", weight: 2, waf: true);
                return Api.Error(403, "FORBIDDEN", policyError);
            }

            var (user, error) = await users.CreateAsync(Api.Clean(body.UserName, 32), body.Password ?? "",
                Api.Clean(body.DisplayName, 32), role, Api.Clean(body.Email, 128), identity.User.UserName);

            if (user is null) return Api.Error(400, "CREATE_FAILED", error);

            var demo = users.RevealDemoPassword(user);
            return Api.Ok(new
            {
                message = $"已创建账号 {user.UserName}（{Roles.Label(user.Role)}，工号 {user.EmployeeId}）",
                user = new
                {
                    user.Id, user.UserName, user.DisplayName, user.EmployeeId,
                    user.Role, roleLabel = Roles.Label(user.Role),
                    mirrorState = user.MirrorState, mirrorError = user.MirrorError,
                },
                //只回演示口令，不回真实口令
                mirrorDemoPassword = demo ?? "",
                mirrorNote = user.MirrorState switch
                {
                    "synced" => "已同步到飞书用户表",
                    "skipped" => "未配置飞书，已跳过镜像（不影响本机账号）",
                    _ => "飞书镜像失败，已静默处理；可稍后在账号列表点「重试镜像」",
                },
            });
        });

        group.MapPost("/users/{id}/role", async (HttpContext context, string id, UserService users,
            SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<ChangeRoleRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var role = Api.Clean(body.Role, 16).ToLowerInvariant();
            var reason = Api.Clean(body.Reason, 200);
            if (reason.Length < 2) return Api.Error(400, "REASON_REQUIRED", "请填写变更理由（不少于 2 个字符）");

            var targetUserId = Api.Clean(id, 64);
            var target = await users.FindByIdAsync(targetUserId);
            if (target is null) return Api.Error(404, "NOT_FOUND", "账号不存在");

            var superAdmins = await users.CountActiveSuperAdminsAsync();
            var policyError = AccountPolicy.CanChangeRole(identity.User.Role, identity.User.Id, targetUserId,
                target.Role, role, superAdmins);
            if (policyError is not null)
            {
                await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                    "user.role", target.UserName, "DENIED",
                    $"{policyError}（目标当前角色={Roles.Label(target.Role)}，请求角色={Roles.Label(role)}）");
                guard.Strike(Api.Ip(context), $"越权变更角色（{identity.User.UserName}）", weight: 2, waf: true);
                return Api.Error(403, "FORBIDDEN", policyError);
            }

            var ok = await users.SetRoleAsync(targetUserId, role, identity.User.UserName, Api.Ip(context));
            if (!ok) return Api.Error(404, "NOT_FOUND", "账号不存在");

            return Api.Ok(new { message = $"角色已更新为 {Roles.Label(role)}，该账号的旧令牌已失效" });
        });

        group.MapPost("/users/{id}/status", async (HttpContext context, string id, UserService users,
            SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<AccountStatusRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var targetUserId = Api.Clean(id, 64);
            var target = await users.FindByIdAsync(targetUserId);
            if (target is null) return Api.Error(404, "NOT_FOUND", "账号不存在");

            var superAdmins = await users.CountActiveSuperAdminsAsync();
            var policyError = AccountPolicy.CanSetDisabled(identity.User.Role, identity.User.Id, targetUserId,
                target.Role, body.Disabled, superAdmins);
            if (policyError is not null)
            {
                await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                    body.Disabled ? "user.disable" : "user.enable", target.UserName, "DENIED", policyError);
                return Api.Error(403, "FORBIDDEN", policyError);
            }

            var ok = await users.SetDisabledAsync(targetUserId, body.Disabled, identity.User.UserName, Api.Ip(context));
            if (!ok) return Api.Error(404, "NOT_FOUND", "账号不存在");

            return Api.Ok(new { message = body.Disabled ? "账号已停用（令牌立即失效）" : "账号已启用" });
        });

        group.MapPost("/users/{id}/reset-password", async (HttpContext context, string id, UserService users,
            SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var body = Api.ReadBody<AdminResetPasswordRequest>(context);
            if (body is null) return Api.Error(400, "BAD_REQUEST", "请求体格式错误");

            var targetUserId = Api.Clean(id, 64);
            var target = await users.FindByIdAsync(targetUserId);
            if (target is null) return Api.Error(404, "NOT_FOUND", "账号不存在");

            var policyError = AccountPolicy.CanResetPassword(identity.User.Role, identity.User.Id, targetUserId, target.Role);
            if (policyError is not null)
            {
                await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                    "user.password", target.UserName, "DENIED", policyError);
                return Api.Error(403, "FORBIDDEN", policyError);
            }

            var ok = await users.ChangePasswordAsync(targetUserId, body.NewPassword ?? "",
                identity.User.UserName, Api.Ip(context));
            if (!ok) return Api.Error(400, "RESET_FAILED", "重置失败（口令不满足强度要求或账号不存在）");

            var refreshed = await users.FindByIdAsync(targetUserId);
            return Api.Ok(new
            {
                message = "口令已重置，该账号旧令牌全部失效",
                mirrorDemoPassword = refreshed is null ? "" : (users.RevealDemoPassword(refreshed) ?? ""),
            });
        });

        group.MapPost("/users/{id}/delete", async (HttpContext context, string id, UserService users,
            SecurityGuard guard, AuditLog audit) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);

            var targetUserId = Api.Clean(id, 64);
            var target = await users.FindByIdAsync(targetUserId);
            if (target is null) return Api.Error(404, "NOT_FOUND", "账号不存在");

            var superAdmins = await users.CountActiveSuperAdminsAsync();
            var policyError = AccountPolicy.CanDeleteAccount(identity.User.Role, identity.User.Id, targetUserId,
                target.Role, superAdmins);
            if (policyError is not null)
            {
                await audit.WriteAsync(identity.User.Id, identity.User.UserName, identity.User.Role, Api.Ip(context),
                    "user.delete", target.UserName, "DENIED", policyError);
                return Api.Error(403, "FORBIDDEN", policyError);
            }

            var ok = await users.DeleteAsync(targetUserId, identity.User.UserName, Api.Ip(context));
            if (!ok) return Api.Error(404, "NOT_FOUND", "账号不存在");
            return Api.Ok(new { message = $"账号 {target.UserName} 已删除（本机权威库；飞书镜像行需人工清理）" });
        });

        group.MapPost("/users/{id}/mirror-retry", async (HttpContext context, string id, UserService users,
            SecurityGuard guard) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!AdminWriteAllowed(guard, identity, out var limitMessage)) return Api.Error(429, "RATE_LIMITED", limitMessage);
            if (!AccountPolicy.Can(identity.User.Role, Perm.AccountManage))
                return Api.Error(403, "FORBIDDEN", "当前角色无权重试镜像");

            var result = await users.RetryMirrorAsync(Api.Clean(id, 64), Api.Ip(context));
            return Api.Ok(new
            {
                ok = result.Ok,
                skipped = result.Skipped,
                error = result.Error,
                message = result.Ok
                    ? (result.Skipped ? "已跳过（飞书未配置）" : "镜像已同步")
                    : "镜像仍然失败（已静默处理，不影响本机账号）",
            });
        });

        //审计
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

        //版本与回退
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

            //回退本身也建任务，留审计
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

        //数据与出站箱
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

        //安全态势
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

        //报销单 AI 预审：管理台只读建议，没有任何执行端点
        group.MapGet("/invoice-ai/scores", async (HttpContext context, InvoiceService invoices, InvoiceAiService ai,
            bool? refresh) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!Roles.IsAdminSide(identity.User.Role))
                return Api.Error(403, "FORBIDDEN", "只有管理员可以查看 AI 预审建议");
            if (!ai.Enabled) return Api.Error(400, "AI_DISABLED", "报销 AI 预审未启用");

            var (ok, error, rows) = await invoices.ListAsync(identity, 500);
            if (!ok) return Api.Error(502, "FEISHU_UNAVAILABLE", error);

            InvoiceAiOutcome? outcome = null;
            Dictionary<string, InvoiceAiScore> index;
            if (refresh == true)
            {
                outcome = await ai.ScoreAsync(identity, rows, true, context.RequestAborted);
                index = outcome.Scores
                    .GroupBy(s => s.RecordId, StringComparer.Ordinal)
                    .ToDictionary(g => g.Key, g => g.First(), StringComparer.Ordinal);
            }
            else
            {
                index = await ai.ScoreIndexAsync(rows, context.RequestAborted);
            }

            var list = rows
                .Select(r => new
                {
                    recordId = r.RecordId,
                    billNo = r.Text(InvoicePolicy.FieldBillNo),
                    applicant = r.Text(InvoicePolicy.FieldApplicant),
                    employeeId = r.Text(InvoicePolicy.FieldEmployeeId),
                    rtype = r.Text(InvoicePolicy.FieldType),
                    amount = r.Amount,
                    city = r.Text(InvoicePolicy.FieldCity),
                    hasInvoice = r.Text(InvoicePolicy.FieldInvoiceState),
                    reason = r.Text(InvoicePolicy.FieldReason),
                    status = r.Text(InvoicePolicy.FieldStatus),
                    hasScore = index.ContainsKey(r.RecordId),
                    finalLevel = index.TryGetValue(r.RecordId, out var s) ? s.FinalLevel : 0,
                    levelLabel = index.TryGetValue(r.RecordId, out var s2) ? InvoiceAiLevel.Label(s2.FinalLevel) : "未评分",
                    ruleSummary = index.TryGetValue(r.RecordId, out var s3) ? s3.RuleSummary : "",
                    aiReason = index.TryGetValue(r.RecordId, out var s4) ? s4.AiReason : "",
                })
                .ToList();

            return Api.Ok(new
            {
                note = "管理台仅展示 AI 建议；本系统不存在由 AI 触发审批或执行的接口，审批必须由人或财务在各自界面点击",
                aiAvailable = outcome?.AiAvailable ?? true,
                aiError = outcome?.AiError ?? "",
                scoredCount = index.Count,
                total = list.Count,
                rows = list,
            });
        });

        //AI 按需求选单（管理台只读，同样不执行）
        group.MapGet("/invoice-ai/find", async (HttpContext context, InvoiceService invoices, InvoiceAiService ai,
            string? q) =>
        {
            var identity = Api.Identity(context);
            if (identity is null) return Api.Error(401, "UNAUTHENTICATED", "请先登录");
            if (!Roles.IsAdminSide(identity.User.Role))
                return Api.Error(403, "FORBIDDEN", "只有管理员可以使用 AI 选单建议");

            var requirement = Api.Clean(q, 200);
            if (requirement.Length < 2) return Api.Error(400, "BAD_REQUEST", "请提供 q（需求描述，不少于 2 个字符）");

            var (ok, error, rows) = await invoices.ListAsync(identity, 500);
            if (!ok) return Api.Error(502, "FEISHU_UNAVAILABLE", error);

            var index = await ai.ScoreIndexAsync(rows, context.RequestAborted);
            var outcome = await ai.FindAsync(identity, rows, index, requirement, context.RequestAborted);
            if (!outcome.Ok) return Api.Error(400, "AI_FIND_FAILED", outcome.Error);

            return Api.Ok(new
            {
                action = outcome.Action,
                note = outcome.Note,
                message = "仅为 AI 建议清单；管理台不提供批量审批接口，请到待审批任务里逐个由人处理",
                picked = outcome.Picked.Select(p => new
                {
                    recordId = p.RecordId,
                    billNo = p.BillNo,
                    summary = p.Summary,
                    reason = p.Reason,
                    level = p.Level,
                    levelLabel = p.Level > 0 ? InvoiceAiLevel.Label(p.Level) : "",
                    status = p.Status,
                    applicant = p.Applicant,
                    amount = p.Amount,
                    rtype = p.Type,
                }).ToList(),
                dropped = outcome.Dropped.Select(d => new { recordId = d.RecordId, droppedReason = d.DroppedReason }).ToList(),
            });
        });
    }

    private static bool AdminWriteAllowed(SecurityGuard guard, RequestIdentity identity, out string message)
    {
        message = "";
        if (guard.TryConsume($"adminwrite:{identity.User.Id}", guard.AdminWritePerMinute, TimeSpan.FromMinutes(1), identity.Ip)) return true;
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
