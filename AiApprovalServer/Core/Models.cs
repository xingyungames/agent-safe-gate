using System.Text.Json.Serialization;

namespace AiApproval.Core;

//四种角色常量
public static class Roles
{
    public const string SuperAdmin = "superadmin";
    public const string Admin = "admin";
    public const string Finance = "finance";
    public const string Employee = "employee";

    //旧角色，仅用于数据迁移
    public const string LegacyUser = "user";
    public const string LegacyStaff = "staff";

    public static string Label(string role) => role switch
    {
        SuperAdmin => "超级管理员",
        Admin => "管理员",
        Finance => "财务部",
        Employee => "员工",
        LegacyUser => "用户（旧）",
        LegacyStaff => "员工（旧）",
        _ => "未知角色",
    };

    public static bool IsValid(string role) => role is SuperAdmin or Admin or Finance or Employee;

    //旧角色映射到新角色
    public static string Migrate(string role) => role switch
    {
        LegacyUser => Employee,
        LegacyStaff => Finance,
        _ => role,
    };

    //是否管理侧
    public static bool IsAdminSide(string role) => role is SuperAdmin or Admin;

    //是否可写业务数据
    public static bool IsFinanceSide(string role) => role is SuperAdmin or Admin or Finance;

    //映射到飞书的角色标签
    public static string ToFeishuLabel(string role) => role switch
    {
        SuperAdmin or Admin => "管理员",
        Finance => "财务",
        _ => "员工",
    };

    //飞书角色标签反向映射，不导入超管
    public static string FromFeishuLabel(string label)
    {
        var value = (label ?? "").Trim();
        if (value.Length == 0) return Employee;
        if (value.Contains("管理", StringComparison.Ordinal)) return Admin;
        if (value.Contains("财务", StringComparison.Ordinal)) return Finance;
        return Employee;
    }

    public static readonly string[] All = { SuperAdmin, Admin, Finance, Employee };
}

//任务状态机
public static class TaskState
{
    //等待人工审批
    public const string PendingApproval = "PENDING_APPROVAL";
    //执行中，防并发重复执行
    public const string Executing = "EXECUTING";
    //已执行
    public const string Executed = "EXECUTED";
    //规则硬拦截
    public const string RejectedRule = "REJECTED_RULE";
    //AI 审核打回
    public const string RejectedAi = "REJECTED_AI";
    //管理员人工打回
    public const string RejectedAdmin = "REJECTED_ADMIN";
    //审批超时作废
    public const string Expired = "EXPIRED";
    //申请人撤回
    public const string Cancelled = "CANCELLED";
    //执行失败
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

//风险等级，只升不降
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

    //取更高的风险等级
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

//用户记录
public sealed class UserRecord
{
    public string Id { get; set; } = "";
    public string UserName { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string Role { get; set; } = Roles.Employee;

    //工号，服务端生成且唯一
    public string EmployeeId { get; set; } = "";

    //口令摘要 base64
    public string PasswordHash { get; set; } = "";
    public string PasswordSalt { get; set; } = "";
    public int PasswordIterations { get; set; }

    //口令方案，空表示旧方案
    public string PasswordScheme { get; set; } = "";

    //账号来源
    public string Origin { get; set; } = "local";

    //飞书镜像的演示口令密文
    public string MirrorPasswordCipher { get; set; } = "";

    //镜像状态
    public string MirrorState { get; set; } = "pending";
    public string MirrorError { get; set; } = "";
    public DateTime? MirrorAt { get; set; }

    //飞书行 record_id
    public string MirrorRecordId { get; set; } = "";

    //写进飞书的哈希与盐
    public string MirrorHashHex { get; set; } = "";
    public string MirrorSaltHex { get; set; } = "";

    //邮箱密文
    public string EmailCipher { get; set; } = "";
    public string EmailMasked { get; set; } = "";

    public bool Disabled { get; set; }
    public int FailedLoginCount { get; set; }
    public DateTime? LockedUntil { get; set; }
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public string CreatedBy { get; set; } = "system";
    public DateTime? LastLoginAt { get; set; }
    public string LastLoginIp { get; set; } = "";

    //AI 每日调用配额（Agent 对话）
    public string AiQuotaDate { get; set; } = "";
    public int AiCallsToday { get; set; }

    //报销单 AI 预审的独立每日配额（与对话配额分开，旧文件缺字段取默认 0）
    public string InvoiceAiQuotaDate { get; set; } = "";
    public int InvoiceAiCallsToday { get; set; }

    //令牌世代，+1 使旧令牌失效
    public int TokenEpoch { get; set; }

    [JsonIgnore] public bool IsLocked => LockedUntil.HasValue && LockedUntil.Value > DateTime.UtcNow;
}

//规则引擎判定结果
public sealed class RuleOutcome
{
    //ALLOW/DENY/ASK 判定
    public string Decision { get; set; } = "ALLOW";
    //是否必须人工审批
    public bool RequiresApproval { get; set; }
    public string RiskLevel { get; set; } = Risk.Low;
    public List<string> Reasons { get; set; } = new();
    //规范化后的参数，下游只认这份
    public Dictionary<string, string> NormalizedArgs { get; set; } = new();
    public string CanonicalAction { get; set; } = "";
    //参数翻译痕迹，审批时展示
    public List<string> Translations { get; set; } = new();

    //沙箱低风险写可自动执行
    public bool SandboxAutoAllowed { get; set; }
}

//AI 审核结果，物理隔离
public sealed class ReviewOutcome
{
    //允许/拒绝/转人工/不可用
    public string Verdict { get; set; } = "unavailable";
    public string RiskLevel { get; set; } = Risk.Medium;
    public string Category { get; set; } = "";
    public string Reason { get; set; } = "";
    //审核是否可用
    public bool Available { get; set; }
    public string Raw { get; set; } = "";
}

//审批任务
public sealed class AgentTask
{
    public string Id { get; set; } = "";
    public string UserId { get; set; } = "";
    public string UserName { get; set; } = "";
    public string UserRole { get; set; } = Roles.Employee;

    //意图摘要
    public string Intent { get; set; } = "";
    //规范化后的动作
    public string ActionJson { get; set; } = "";
    public string ToolName { get; set; } = "";

    //原始输入密文
    public string RawInputCipher { get; set; } = "";
    //原始输入字符数
    public int RawInputLength { get; set; }

    public string RiskLevel { get; set; } = Risk.Low;
    public RuleOutcome Rule { get; set; } = new();
    public ReviewOutcome Review { get; set; } = new();

    public string Status { get; set; } = TaskState.PendingApproval;
    public string StatusReason { get; set; } = "";

    //注入特征命中项
    public List<string> InjectionFlags { get; set; } = new();

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public DateTime? DecidedAt { get; set; }
    public string DecidedBy { get; set; } = "";
    public string DecisionReason { get; set; } = "";

    public DateTime? ExecutedAt { get; set; }
    public string ResultJson { get; set; } = "";

    //审批令牌摘要，不存明文
    public string ApprovalTokenHash { get; set; } = "";
    public DateTime ApprovalExpiresAt { get; set; }
    public bool ApprovalTokenUsed { get; set; }

    //本次执行的版本记录
    public List<string> VersionIds { get; set; } = new();
    public bool RolledBack { get; set; }
    public DateTime? RolledBackAt { get; set; }
    public string RolledBackBy { get; set; } = "";

    //是否由回退操作产生
    public string RollbackOfVersionId { get; set; } = "";

    //已发过期提醒
    public bool ExpiryWarned { get; set; }
}

//版本记录：快照与反向操作
public sealed class VersionRecord
{
    public string Id { get; set; } = "";
    public string TaskId { get; set; } = "";
    public string UserId { get; set; } = "";
    public string UserName { get; set; } = "";
    public string Kind { get; set; } = "file";      //file/dataset/notice
    public string Operation { get; set; } = "";     //写/新建/覆盖/删除/改/插入/撤回
    public string Target { get; set; } = "";        //目标：文件路径或数据集行
    public bool PreviousExisted { get; set; }
    public string PreviousHash { get; set; } = "";  //旧内容的 blob 摘要
    public string PreviousBlob { get; set; } = "";  //blob 文件名
    public string NewHash { get; set; } = "";
    public int PreviousSize { get; set; }
    public int NewSize { get; set; }
    public string DiffText { get; set; } = "";      //Git 式 +/- 变更
    public int AddedLines { get; set; }
    public int RemovedLines { get; set; }
    public string UndoLogJson { get; set; } = "";   //反向操作数据
    public string CompensateAction { get; set; } = ""; //补偿事务类型
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public bool Reverted { get; set; }
    public DateTime? RevertedAt { get; set; }
    public string RevertedBy { get; set; } = "";
    public string RevertTaskId { get; set; } = "";
}

//审计条目，逐条哈希链
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

//出站通知
public sealed class OutboxMessage
{
    public string Id { get; set; } = "";
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public string To { get; set; } = "";
    public string Subject { get; set; } = "";
    public string Body { get; set; } = "";
    //审批链接，HMAC 签名防越权
    public string ApprovalLink { get; set; } = "";
    public string TaskId { get; set; } = "";
    public string OwnerUserId { get; set; } = "";
    public string OwnerUserName { get; set; } = "";
    public string Status { get; set; } = "QUEUED"; //队列/已发/失败/已撤回
    public string Error { get; set; } = "";
    public DateTime? SentAt { get; set; }
    public DateTime? CompensatedAt { get; set; }
}

//AI 意图解析结果
public sealed class IntentPlan
{
    public string Intent { get; set; } = "";
    //工具名，ask 表示只回答
    public string Tool { get; set; } = "ask";
    public Dictionary<string, string> Args { get; set; } = new();
    //给用户的回复
    public string Reply { get; set; } = "";
    public string Source { get; set; } = "ai"; //来源：ai/本地/业务界面
    //任务是否已完成
    public bool Done { get; set; }
}

//代理循环的一步
public sealed class AgentStep
{
    public int Index { get; set; }
    public string Tool { get; set; } = "";
    public string Title { get; set; } = "";
    public string ArgsJson { get; set; } = "";
    public string Summary { get; set; } = "";
    public string Status { get; set; } = "OK";
    //只读步骤的执行结果
    public string ResultJson { get; set; } = "";
    public long ElapsedMs { get; set; }
}

//请求 DTO
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
    //角色，缺省为员工
    public string Role { get; set; } = "";
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
    public string Role { get; set; } = Roles.Employee;
    public string Email { get; set; } = "";
}

public sealed class ResetPasswordRequest
{
    public string NewPassword { get; set; } = "";
}

public sealed class ChangeRoleRequest
{
    public string Role { get; set; } = Roles.Employee;
    //变更理由，必填
    public string Reason { get; set; } = "";
}

//从飞书同步账号，dryRun 只预览
public sealed class FeishuSyncRequest
{
    public bool DryRun { get; set; }
    //操作理由，选填
    public string Reason { get; set; } = "";
}

//管理员重置他人口令
public sealed class AdminResetPasswordRequest
{
    public string NewPassword { get; set; } = "";
    public string Reason { get; set; } = "";
}

//账号启用/停用请求
public sealed class AccountStatusRequest
{
    public bool Disabled { get; set; }
    public string Reason { get; set; } = "";
}

public sealed class UnbanRequest
{
    public string Ip { get; set; } = "";
}

public sealed class AiQuestionRequest
{
    public string Question { get; set; } = "";
}
