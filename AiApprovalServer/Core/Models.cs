using System.Text.Json.Serialization;

namespace AiApproval.Core;

/// <summary>角色常量。系统只有三种角色：普通用户 / 员工 / 管理员。</summary>
public static class Roles
{
    public const string User = "user";
    public const string Staff = "staff";
    public const string Admin = "admin";

    public static string Label(string role) => role switch
    {
        User => "用户",
        Staff => "员工",
        Admin => "管理员",
        _ => "未知角色",
    };

    public static bool IsValid(string role) => role is User or Staff or Admin;
}

/// <summary>任务状态机（审批流的全部合法状态）。</summary>
public static class TaskState
{
    /// <summary>等待管理员人工审批</summary>
    public const string PendingApproval = "PENDING_APPROVAL";
    /// <summary>审批已通过、正在执行（用于防"两次点击/两个管理员"并发重复执行）</summary>
    public const string Executing = "EXECUTING";
    /// <summary>已执行（自动放行 或 审批通过后执行）</summary>
    public const string Executed = "EXECUTED";
    /// <summary>被规则引擎硬拦截（确定性拦截，无人工介入）</summary>
    public const string RejectedRule = "REJECTED_RULE";
    /// <summary>被 AI 安全审核打回</summary>
    public const string RejectedAi = "REJECTED_AI";
    /// <summary>被管理员人工打回</summary>
    public const string RejectedAdmin = "REJECTED_ADMIN";
    /// <summary>审批超时作废</summary>
    public const string Expired = "EXPIRED";
    /// <summary>申请人自己撤回（不会执行任何动作）</summary>
    public const string Cancelled = "CANCELLED";
    /// <summary>执行期失败（工具报错），已留全过程审计</summary>
    public const string Failed = "FAILED";

    public static string Label(string status) => status switch
    {
        PendingApproval => "待人工审批",
        Executing => "执行中",
        Executed => "已执行",
        RejectedRule => "规则拦截",
        RejectedAi => "AI 审核拒绝",
        RejectedAdmin => "管理员打回",
        Expired => "已过期",
        Cancelled => "已撤回（申请人）",
        Failed => "执行失败",
        _ => status,
    };

    public static bool IsRejected(string status) =>
        status is RejectedRule or RejectedAi or RejectedAdmin or Expired;}

/// <summary>风险等级。由工具目录静态声明，AI 只能在此基础上"提高"不能"降低"。</summary>
public static class Risk
{
    public const string Low = "LOW";
    public const string Medium = "MEDIUM";
    public const string High = "HIGH";
    public const string Critical = "CRITICAL";

    private static readonly Dictionary<string, int> Order = new(StringComparer.Ordinal)
    {
        [Low] = 0,
        [Medium] = 1,
        [High] = 2,
        [Critical] = 3,
    };

    public static int Rank(string? level) => level is not null && Order.TryGetValue(level, out var r) ? r : 99;

    /// <summary>取两者中更高的风险等级（用于"只升不降"）。</summary>
    public static string Max(string a, string b) => Rank(a) >= Rank(b) ? a : b;

    public static string Label(string level) => level switch
    {
        Low => "低",
        Medium => "中",
        High => "高",
        Critical => "极高",
        _ => level,
    };
}

/// <summary>用户记录。落盘为 users.json，密码只存 PBKDF2 摘要，邮箱等敏感字段 AES-GCM 加密。</summary>
public sealed class UserRecord
{
    public string Id { get; set; } = "";
    public string UserName { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string Role { get; set; } = Roles.User;

    /// <summary>PBKDF2-HMAC-SHA256 摘要（base64）</summary>
    public string PasswordHash { get; set; } = "";
    public string PasswordSalt { get; set; } = "";
    public int PasswordIterations { get; set; }

    /// <summary>AES-256-GCM 密文（base64: nonce|tag|cipher），明文为邮箱</summary>
    public string EmailCipher { get; set; } = "";
    public string EmailMasked { get; set; } = "";

    public bool Disabled { get; set; }
    public int FailedLoginCount { get; set; }
    public DateTime? LockedUntil { get; set; }
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public string CreatedBy { get; set; } = "system";
    public DateTime? LastLoginAt { get; set; }
    public string LastLoginIp { get; set; } = "";

    /// <summary>AI 调用每日配额（防"经济型 DoS"：攻击者刷接口烧钱）</summary>
    public string AiQuotaDate { get; set; } = "";
    public int AiCallsToday { get; set; }

    /// <summary>令牌世代：改密码 / 改角色 / 禁用账号时 +1，使所有已签发令牌立即失效</summary>
    public int TokenEpoch { get; set; }

    [JsonIgnore] public bool IsLocked => LockedUntil.HasValue && LockedUntil.Value > DateTime.UtcNow;
}

/// <summary>规则引擎的确定性判定结果（硬防线）。</summary>
public sealed class RuleOutcome
{
    /// <summary>ALLOW = 允许继续走后续流程；DENY = 硬拦截；ASK = 参数不明确，把候选回给上游继续澄清（不落任务）</summary>
    public string Decision { get; set; } = "ALLOW";
    /// <summary>是否必须经人工审批（由工具目录静态决定，AI 无权下调）</summary>
    public bool RequiresApproval { get; set; }
    public string RiskLevel { get; set; } = Risk.Low;
    public List<string> Reasons { get; set; } = new();
    /// <summary>规范化/校验后的参数（下游只使用这一份，绝不重新解析 AI 原始文本）</summary>
    public Dictionary<string, string> NormalizedArgs { get; set; } = new();
    public string CanonicalAction { get; set; } = "";
    /// <summary>参数被系统"翻译"过的痕迹（例如 口语「重要」→ 枚举「铂金」、客户名「示例物流」→ C1004），审批时展示</summary>
    public List<string> Translations { get; set; } = new();

    /// <summary>
    /// 沙箱内低风险写（个人工作区新建/追加、内容无风险特征且非注入输入）：
    /// 即使 AI 审核不可用也允许自动执行（仍会落快照与审计）。
    /// 覆盖、删除、共享区、脚本类内容一律为 false —— 那些必须人工审批。
    /// </summary>
    public bool SandboxAutoAllowed { get; set; }
}

/// <summary>AI 安全审核结果（软防线）。审核端物理隔离：只看到 intent + action，看不到用户原始输入。</summary>
public sealed class ReviewOutcome
{
    /// <summary>allow / deny / review / unavailable</summary>
    public string Verdict { get; set; } = "unavailable";
    public string RiskLevel { get; set; } = Risk.Medium;
    public string Category { get; set; } = "";
    public string Reason { get; set; } = "";
    /// <summary>审核 AI 是否可用（不可用时按"必须人工审批"处理，绝不自动放行）</summary>
    public bool Available { get; set; }
    public string Raw { get; set; } = "";
}

/// <summary>审批任务（一次"AI 意图 + 工具调用"的完整生命周期记录）。</summary>
public sealed class AgentTask
{
    public string Id { get; set; } = "";
    public string UserId { get; set; } = "";
    public string UserName { get; set; } = "";
    public string UserRole { get; set; } = Roles.User;

    /// <summary>意图摘要（AI 生成的 intent，用于审计与人工审批阅读）</summary>
    public string Intent { get; set; } = "";
    /// <summary>规范化后的动作（工具名 + 参数），下游执行只认这一份</summary>
    public string ActionJson { get; set; } = "";
    public string ToolName { get; set; } = "";

    /// <summary>用户原始输入的 AES-GCM 密文（管理员审批时解密展示完整上下文）</summary>
    public string RawInputCipher { get; set; } = "";
    /// <summary>原始输入的字符数（不落明文）</summary>
    public int RawInputLength { get; set; }

    public string RiskLevel { get; set; } = Risk.Low;
    public RuleOutcome Rule { get; set; } = new();
    public ReviewOutcome Review { get; set; } = new();

    public string Status { get; set; } = TaskState.PendingApproval;
    public string StatusReason { get; set; } = "";

    /// <summary>输入侧注入特征命中项（提示词注入/逃逸尝试）</summary>
    public List<string> InjectionFlags { get; set; } = new();

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public DateTime? DecidedAt { get; set; }
    public string DecidedBy { get; set; } = "";
    public string DecisionReason { get; set; } = "";

    public DateTime? ExecutedAt { get; set; }
    public string ResultJson { get; set; } = "";

    /// <summary>审批链接签名令牌（只存摘要，不存明文，防止日志/文件泄露后被重放）</summary>
    public string ApprovalTokenHash { get; set; } = "";
    public DateTime ApprovalExpiresAt { get; set; }
    public bool ApprovalTokenUsed { get; set; }

    /// <summary>本次执行产生的版本记录（快照 + diff），支持一键回退</summary>
    public List<string> VersionIds { get; set; } = new();
    public bool RolledBack { get; set; }
    public DateTime? RolledBackAt { get; set; }
    public string RolledBackBy { get; set; } = "";

    /// <summary>该任务是否是"回退操作"本身产生的（用于审计链闭环）</summary>
    public string RollbackOfVersionId { get; set; } = "";

    /// <summary>是否已给提交人发过"审批即将过期"提醒（避免重复提醒）</summary>
    public bool ExpiryWarned { get; set; }
}

/// <summary>
/// 版本记录 = 执行前快照 + Diff + 反向操作说明。
/// 文件操作 → 全量快照（blob 内容寻址）；数据操作 → UndoLog（反向 SQL 描述 + 旧行 JSON）；外发 → 补偿事务。
/// </summary>
public sealed class VersionRecord
{
    public string Id { get; set; } = "";
    public string TaskId { get; set; } = "";
    public string UserId { get; set; } = "";
    public string UserName { get; set; } = "";
    public string Kind { get; set; } = "file";      // file | dataset | notice
    public string Operation { get; set; } = "";     // write/create/overwrite/delete/update/insert/recall
    public string Target { get; set; } = "";        // 文件相对路径 或 dataset:行ID
    public bool PreviousExisted { get; set; }
    public string PreviousHash { get; set; } = "";  // 内容寻址的 blob 摘要（SHA-256）
    public string PreviousBlob { get; set; } = "";  // blob 文件名
    public string NewHash { get; set; } = "";
    public int PreviousSize { get; set; }
    public int NewSize { get; set; }
    public string DiffText { get; set; } = "";      // 类 Git 的 +/- 变更
    public int AddedLines { get; set; }
    public int RemovedLines { get; set; }
    public string UndoLogJson { get; set; } = "";   // 反向操作数据（旧行/旧字段值）
    public string CompensateAction { get; set; } = ""; // 补偿事务类型，如 recall_notice
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public bool Reverted { get; set; }
    public DateTime? RevertedAt { get; set; }
    public string RevertedBy { get; set; } = "";
    public string RevertTaskId { get; set; } = "";
}

/// <summary>审计日志条目。逐条哈希链：PrevHash + 本条内容 → Hash，任何篡改都会断链。</summary>
public sealed class AuditEntry
{
    public long Seq { get; set; }
    public DateTime Time { get; set; } = DateTime.UtcNow;
    public string ActorId { get; set; } = "";
    public string ActorName { get; set; } = "";
    public string ActorRole { get; set; } = "";
    public string Ip { get; set; } = "";
    public string Action { get; set; } = "";
    public string Target { get; set; } = "";
    public string Outcome { get; set; } = "";
    public string Detail { get; set; } = "";
    public string PrevHash { get; set; } = "";
    public string Hash { get; set; } = "";
}

/// <summary>出站通知（邮件/站内）。Mail.Enabled=false 时只写"出站箱"，后台可查看并模拟投递。</summary>
public sealed class OutboxMessage
{
    public string Id { get; set; } = "";
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public string To { get; set; } = "";
    public string Subject { get; set; } = "";
    public string Body { get; set; } = "";
    /// <summary>审批链接（带 HMAC 签名，防 IDOR 越权审批）</summary>
    public string ApprovalLink { get; set; } = "";
    public string TaskId { get; set; } = "";
    public string OwnerUserId { get; set; } = "";
    public string OwnerUserName { get; set; } = "";
    public string Status { get; set; } = "QUEUED"; // QUEUED | SENT | FAILED | RECALLED
    public string Error { get; set; } = "";
    public DateTime? SentAt { get; set; }
    public DateTime? CompensatedAt { get; set; }
}

/// <summary>AI 返回的意图解析结果（上游"翻译意图"，其输出永远不被信任，必须过规则引擎）。</summary>
public sealed class IntentPlan
{
    public string Intent { get; set; } = "";
    /// <summary>工具名；ask 表示只回答不调用工具</summary>
    public string Tool { get; set; } = "ask";
    public Dictionary<string, string> Args { get; set; } = new();
    /// <summary>给用户看的一句话回复</summary>
    public string Reply { get; set; } = "";
    public string Source { get; set; } = "ai"; // ai | local-fallback | biz-ui
    /// <summary>AI 认为任务已完成（多步循环的终止信号）</summary>
    public bool Done { get; set; }
}

/// <summary>多步代理循环里的一步（前端用来展示"先搜 → 再读 → 再写"的过程）。</summary>
public sealed class AgentStep
{
    public int Index { get; set; }
    public string Tool { get; set; } = "";
    public string Title { get; set; } = "";
    public string ArgsJson { get; set; } = "";
    public string Summary { get; set; } = "";
    public string Status { get; set; } = "OK";
    /// <summary>只读步骤的执行结果（截断后给前端展示）</summary>
    public string ResultJson { get; set; } = "";
    public long ElapsedMs { get; set; }
}

/// <summary>请求 DTO。</summary>
public sealed class LoginRequest
{
    public string UserName { get; set; } = "";
    public string Password { get; set; } = "";
}

public sealed class RegisterRequest
{
    public string UserName { get; set; } = "";
    public string Password { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string Email { get; set; } = "";
}

public sealed class ChatRequest
{
    public string Message { get; set; } = "";
    public string? SessionId { get; set; }
}

public sealed class ReasonRequest
{
    public string Reason { get; set; } = "";
}

public sealed class ApproveByTokenRequest
{
    public string TaskId { get; set; } = "";
    public string Token { get; set; } = "";
    public string Reason { get; set; } = "";
}

public sealed class UserAdminRequest
{
    public string UserName { get; set; } = "";
    public string Password { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string Role { get; set; } = Roles.User;
    public string Email { get; set; } = "";
}

public sealed class ResetPasswordRequest
{
    public string NewPassword { get; set; } = "";
}

public sealed class ChangeRoleRequest
{
    public string Role { get; set; } = Roles.User;
}

public sealed class UnbanRequest
{
    public string Ip { get; set; } = "";
}

public sealed class AiQuestionRequest
{
    public string Question { get; set; } = "";
}
