namespace AiApproval.Core;

//报销单策略：字段、枚举、合规与状态
public static class InvoicePolicy
{
    //字段名，与飞书表列一致

    public const string FieldBillNo = "单号";
    public const string FieldApplicant = "申请人";
    public const string FieldEmployeeId = "工号";
    public const string FieldDepartment = "部门";
    public const string FieldType = "报销类型";
    public const string FieldAmount = "金额";
    public const string FieldReason = "事由";
    public const string FieldInvoiceState = "票据状态";
    public const string FieldCity = "城市";
    public const string FieldInvoiceDate = "发票日期";
    public const string FieldSubmittedAt = "记录提交时间";
    public const string FieldCompliance = "合规结果";
    public const string FieldRiskSummary = "风险摘要";
    public const string FieldStatus = "状态";
    public const string FieldReviewer = "审核人";
    public const string FieldReviewOpinion = "审批意见";
    public const string FieldReviewedAt = "审批时间";
    public const string FieldInvoiceInfo = "发票信息";
    public const string FieldInvoiceFile = "发票附件";

    //只读列：单号与附件
    public static readonly string[] ReadOnlyFields = { FieldBillNo, FieldInvoiceFile };

    //日期列：飞书只接受毫秒时间戳，空值不能写成空串或 null
    public static readonly string[] DateFields = { FieldInvoiceDate, FieldSubmittedAt, FieldReviewedAt };

    //飞书读取时会略过空列，回退时这些列若不在快照里就说明当时是空的，要按空值写回
    public static readonly string[] ClearWhenAbsentFields = { FieldReviewer, FieldReviewOpinion };

    //服务端盖章的列
    public static readonly string[] ServerStampedFields =
        { FieldApplicant, FieldEmployeeId, FieldSubmittedAt, FieldCompliance, FieldRiskSummary, FieldStatus };

    //财务可改的列
    public static readonly string[] FinanceEditableFields =
    {
        FieldDepartment, FieldType, FieldAmount, FieldReason, FieldInvoiceState,
        FieldCity, FieldInvoiceDate, FieldCompliance, FieldRiskSummary,
    };

    //枚举字典

    public static readonly string[] Departments =
    {
        "研发部", "市场部", "财务部", "人力资源部", "行政部", "生产部",
        "销售部", "采购部", "信息技术部", "质量管理部", "客户服务部",
    };

    public static readonly string[] Types = { "差旅费", "招待费", "办公用品", "交通费" };

    public static readonly string[] InvoiceStates = { "有票", "无票", "未提" };

    public static readonly string[] ComplianceResults = { "通过", "风险" };

    public static readonly string[] Statuses = { "待审核", "审核中", "已通过", "已驳回" };

    private static readonly string[] CityTier1 = { "北京", "上海", "广州", "深圳" };

    private static readonly string[] CityTier2 =
    {
        "成都", "杭州", "武汉", "西安", "南京", "重庆", "苏州", "天津", "长沙", "郑州",
        "青岛", "宁波", "东莞", "无锡", "沈阳", "昆明", "佛山", "合肥", "济南",
    };

    private static readonly string[] CityTier3 =
    {
        "福州", "厦门", "哈尔滨", "太原", "南昌", "南宁", "贵阳", "石家庄",
        "兰州", "大连", "常州", "温州", "烟台", "泉州",
    };

    public static readonly string[] Cities = CityTier1.Concat(CityTier2).Concat(CityTier3).ToArray();

    //城市住宿分级上限 500/350/250
    public static int? CityTierCap(string city)
    {
        if (CityTier1.Contains(city)) return 500;
        if (CityTier2.Contains(city)) return 350;
        if (CityTier3.Contains(city)) return 250;
        return null;
    }

    public const decimal MaxAmount = 100_000m;
    public const decimal MinAmount = 0.01m;

    //合规预审

    //文本字段是否含 HTML/脚本标记字符
    //事由、发票信息、审批意见都是纯业务文本，不该出现尖括号。一旦放行，
    //脚本片段会被当作合法数据持久化到飞书，任何「读出来后直接当 HTML 渲染」的下游都会中招（存储型 XSS）
    public static bool ContainsMarkup(string? text)
    {
        if (string.IsNullOrEmpty(text)) return false;
        foreach (var ch in text)
        {
            if (ch is '<' or '>' or '\0') return true;
        }
        return false;
    }

    //统一拒绝话术
    public const string MarkupRejectedMessage = "不能包含 < 或 > 字符（疑似 HTML/脚本标记），请只填写纯文本";

    //预审结论
    public sealed record Compliance(bool Submittable, string Result, string RiskSummary, string? RejectReason);

    //合规预审：无票阻断，其余记风险
    public static Compliance Evaluate(string type, decimal amount, string invoiceState, string city,
        DateOnly? invoiceDate, DateOnly today)
    {
        if (invoiceState == "无票")
            return new Compliance(false, "", "", "票据状态为「无票」：缺少合规发票，提交前无法放行，请补齐发票后再提交");

        var risks = new List<string>();
        if (invoiceState == "未提") risks.Add("票据状态未提，提交前需确认合规发票");

        if (type == "招待费" && amount > 2000m)
            risks.Add("单次招待费超2000元，需总经理事前审批");

        if (type == "办公用品" && amount > 5000m)
            risks.Add("超5000元，走采购流程，不适用个人报销");

        if (type == "差旅费")
        {
            var cap = CityTierCap(city);
            if (cap is not null)
            {
                if (amount > cap.Value) risks.Add($"{city}属住宿分级上限{cap.Value}元/晚，超标需事前审批");
            }
            else if (amount > 3000m)
            {
                risks.Add("差旅费金额较大，请确认住宿城市与晚数是否符合分级标准");
            }
        }

        if (invoiceDate.HasValue && today.DayNumber - invoiceDate.Value.DayNumber > 30)
            risks.Add("距开票超30天，需附情况说明");

        return risks.Count == 0
            ? new Compliance(true, "通过", "无", null)
            : new Compliance(true, "风险", string.Join("；", risks), null);
    }

    //提交校验

    //提交草稿
    public sealed record SubmitDraft(
        string Department, string Type, decimal Amount, string Reason,
        string InvoiceState, string City, DateOnly? InvoiceDate, string InvoiceInfo);

    //校验提交草稿，null 表示通过
    public static string? ValidateSubmit(SubmitDraft draft, out Compliance compliance, DateOnly today)
    {
        compliance = new Compliance(false, "", "", null);

        if (!Departments.Contains(draft.Department))
            return $"部门必须是以下之一：{string.Join("、", Departments)}";
        if (!Types.Contains(draft.Type))
            return $"报销类型必须是以下之一：{string.Join("、", Types)}";
        if (!InvoiceStates.Contains(draft.InvoiceState))
            return $"票据状态必须是以下之一：{string.Join("、", InvoiceStates)}";
        if (draft.Amount < MinAmount || draft.Amount > MaxAmount)
            return $"金额必须在 {MinAmount} 到 {MaxAmount} 之间";
        if (draft.Reason.Trim().Length is < 2 or > 200)
            return "事由需为 2 到 200 个字符，请写清真实业务用途";

        //纯文本校验：挡住「把脚本片段当业务数据存进去」的存储型 XSS
        if (ContainsMarkup(draft.Reason)) return "事由" + MarkupRejectedMessage;

        if (draft.Type == "差旅费")
        {
            if (string.IsNullOrWhiteSpace(draft.City))
                return "差旅费必须填写城市（用于住宿分级校验）";
            if (!Cities.Contains(draft.City))
                return "城市不在允许的分级城市清单内";
        }
        else if (!string.IsNullOrWhiteSpace(draft.City) && !Cities.Contains(draft.City))
        {
            return "城市不在允许的分级城市清单内";
        }

        if (draft.InvoiceInfo.Length > 128)
            return "发票信息（号码；销售方）不得超过 128 个字符";

        if (ContainsMarkup(draft.InvoiceInfo)) return "发票信息" + MarkupRejectedMessage;

        compliance = Evaluate(draft.Type, draft.Amount, draft.InvoiceState, draft.City, draft.InvoiceDate, today);
        return compliance.Submittable ? null : compliance.RejectReason;
    }

    //状态流转

    //审核动作是否合法
    public static string? ValidateReviewTransition(string currentStatus, string newStatus)
    {
        if (!Statuses.Contains(newStatus)) return $"目标状态必须是以下之一：{string.Join("、", Statuses)}";
        if (newStatus == "待审核") return "审核动作不能把状态改回「待审核」";

        return currentStatus switch
        {
            "待审核" => null,
            "审核中" => null,
            "已通过" => "该单据已通过，不能再改状态（如需更正请走管理员回退）",
            "已驳回" => "该单据已驳回，不能再改状态（如需更正请走管理员回退）",
            _ => $"当前状态无法审核：{currentStatus}",
        };
    }

    //数据边界

    //员工只能看自己的单据
    public static bool CanSeeAll(string role) => Roles.IsFinanceSide(role);

    //归属判定：按工号，旧数据按姓名
    public static bool BelongsTo(string role, string employeeId, string applicantName,
        string recordEmployeeId, string recordApplicant)
    {
        if (CanSeeAll(role)) return true;

        var myEid = (employeeId ?? "").Trim();
        var rowEid = (recordEmployeeId ?? "").Trim();
        if (myEid.Length > 0 && rowEid.Length > 0)
            return string.Equals(myEid, rowEid, StringComparison.OrdinalIgnoreCase);

        //旧数据无工号，按姓名兜底
        return string.Equals((applicantName ?? "").Trim(), (recordApplicant ?? "").Trim(), StringComparison.Ordinal);
    }
}
