namespace AiApproval.Core;

/// <summary>
/// 拦截台账记录：一条"因为内容异常而没有写进飞书"的单据。
/// 目的是让管理员能追溯「谁、什么时候、想写什么、命中哪条规则」。
/// </summary>
public sealed class InterceptRecord
{
    public string Id { get; set; } = "";

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    //责任人
    public string UserId { get; set; } = "";
    public string UserName { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string EmployeeId { get; set; } = "";
    public string Role { get; set; } = "";
    public string Ip { get; set; } = "";

    //动作：submit / update / review
    public string Action { get; set; } = "";

    //命中信息
    public string Category { get; set; } = "";   // XSS / SQL注入 / 提示词注入
    public string Pattern { get; set; } = "";    // 具体规则
    public string Field { get; set; } = "";      // 事由 / 发票信息 / 审批意见
    public string Snippet { get; set; } = "";    // 命中片段（截断）

    //完整原文（截断 800 字，供管理员核看意图）
    public string Payload { get; set; } = "";

    //单据其余字段摘要，便于判断这张单原本要干什么
    public string Context { get; set; } = "";

    //处理状态：pending / confirmed（确认恶意）/ falsePositive（误报）/ ignored
    public string Handled { get; set; } = "pending";
    public string HandledBy { get; set; } = "";
    public DateTime? HandledAt { get; set; }
    public string Note { get; set; } = "";

    [System.Text.Json.Serialization.JsonIgnore]
    public bool IsPending => string.Equals(Handled, "pending", StringComparison.OrdinalIgnoreCase);
}
