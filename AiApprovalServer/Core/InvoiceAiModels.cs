namespace AiApproval.Core;

//报销单 AI 预审的评分结果（只做提示，永不参与自动执行）
public sealed class InvoiceAiScore
{
    public string RecordId { get; set; } = "";

    //单据内容指纹：变了才重算
    public string Fingerprint { get; set; } = "";

    //1..4，由确定性规则算出（InvoicePolicy.Evaluate 重算）
    public int RuleLevel { get; set; }

    //1..4，AI 给出；0 表示 AI 不可用或未参与
    public int AiLevel { get; set; }

    //最终等级 = max(RuleLevel, AiLevel)，只升不降
    public int FinalLevel { get; set; }

    public string RuleSummary { get; set; } = "";
    public string AiReason { get; set; } = "";

    //approve / reject / ask，仅建议
    public string Suggest { get; set; } = "";

    //注入命中、脏数据等标记
    public List<string> Flags { get; set; } = new();

    public DateTime ScoredAt { get; set; } = DateTime.UtcNow;
    public string Model { get; set; } = "";
}

//落盘缓存：App_Data/invoice_ai.json
public sealed class InvoiceAiCache
{
    public List<InvoiceAiScore> Scores { get; set; } = new();
    public DateTime UpdatedAt { get; set; } = DateTime.UtcNow;
}

//等级与 Risk 的互转，四档复用具 Risk
public static class InvoiceAiLevel
{
    public const int Min = 1;
    public const int Max = 4;

    //数字等级 → Risk 常量
    public static string ToRisk(int level) => level switch
    {
        <= 1 => Risk.Low,
        2 => Risk.Medium,
        3 => Risk.High,
        _ => Risk.Critical,
    };

    //Risk 常量 → 数字等级
    public static int FromRisk(string? risk) => Risk.Rank(risk) switch
    {
        0 => 1,
        1 => 2,
        2 => 3,
        3 => 4,
        _ => 1,
    };

    //裁剪到 1..4
    public static int Clamp(int level) => level < Min ? Min : level > Max ? Max : level;

    //中文标签，与 Risk.Label 保持一致口径
    public static string Label(int level) => level switch
    {
        1 => "正常",
        2 => "注意",
        3 => "危险",
        _ => "严重",
    };
}
