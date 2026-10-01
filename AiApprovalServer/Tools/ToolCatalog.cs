using System.Text;
using AiApproval.Core;

namespace AiApproval.Tools;

/// <summary>权限键（"权限组"就是权限键的集合，角色 → 权限键映射在 RolePermissions 中固定）。</summary>
public static class Permissions
{
    public const string FileRead = "file.read";
    public const string FileWrite = "file.write";
    public const string FileDelete = "file.delete";
    public const string ShareRead = "file.share.read";
    public const string ShareWrite = "file.share.write";
    public const string DataRead = "data.read";
    public const string DataWrite = "data.write";
    public const string DataDelete = "data.delete";
    public const string NoticeSend = "notice.send";
    public const string AdminRead = "admin.read";
    /// <summary>账号名册查询（管理员专属：非管理员连"有多少用户"都不该知道）</summary>
    public const string UserRead = "user.read";
    // ---- 甲方/乙方对接业务（业务对接台）----
    public const string ClientRead = "client.read";
    /// <summary>跨客户检索（员工专属）。刻意与 ClientRead 分开：甲方也能"读客户档案"（读自己的那条），
    /// 但"按条件检索全部客户"是乙方能力，用同一个权限键会导致甲方越权拉全量客户名单。</summary>
    public const string ClientSearch = "client.search";
    public const string ClientWrite = "client.write";
    public const string ClientAssign = "client.assign";
    public const string ReqRead = "req.read";
    public const string ReqCreate = "req.create";
    public const string ReqWrite = "req.write";

    public static string Label(string key) => key switch
    {
        FileRead => "读取个人文件",
        FileWrite => "写入个人文件",
        FileDelete => "删除个人文件",
        ShareRead => "读取共享文件区",
        ShareWrite => "写入共享文件区",
        DataRead => "查询业务数据",
        DataWrite => "修改业务数据",
        DataDelete => "删除业务数据",
        NoticeSend => "发送通知",
        AdminRead => "管理端只读分析",
        UserRead => "查询账号名册",
        ClientRead => "查看我司档案",
        ClientSearch => "检索客户档案",
        ClientWrite => "维护客户档案",
        ClientAssign => "指派客户负责人",
        ReqRead => "查看需求单",
        ReqCreate => "提交需求单",
        ReqWrite => "维护需求单",
        _ => key,
    };
}

public sealed class ToolDefinition
{
    public string Name { get; init; } = "";
    public string Title { get; init; } = "";
    public string Description { get; init; } = "";
    /// <summary>静态风险等级（AI 只能提高，不能降低）</summary>
    public string RiskLevel { get; init; } = Risk.Low;
    /// <summary>是否"固定"需要人工审批（与运行时升级条件叠加，取或）</summary>
    public bool RequiresApproval { get; init; }
    public bool StateChanging { get; init; }
    public bool AdminOnly { get; init; }
    public string[] Permissions { get; init; } = Array.Empty<string>();
    public Dictionary<string, string> Params { get; init; } = new();
    public string[] RequiredParams { get; init; } = Array.Empty<string>();
    /// <summary>允许出现在参数里的键（白名单，多余键一律拒绝）</summary>
    public string[] AllowedParamNames { get; init; } = Array.Empty<string>();
}

/// <summary>
/// 工具目录：定义"AI 能表达哪些动作"。
///
/// 设计要点：
///   * 工具是**白名单**：目录里没有的动作，AI 说得再动听也无法执行（Harness 只认目录内的名字）；
///   * 风险等级与"是否必须人工审批"由代码静态声明，AI 的建议只能往上加，不能往下减；
///   * 每个工具显式列出参数白名单，未列出的参数直接拒绝（防"凭空多出一个参数"的走私）。
/// </summary>
public static class ToolCatalog
{
    public const int MaxFileContentChars = 8000;

    public static readonly Dictionary<string, ToolDefinition> All = new(StringComparer.OrdinalIgnoreCase)
    {
        ["list_files"] = new ToolDefinition
        {
            Name = "list_files",
            Title = "列出文件",
            Description = "列出个人工作区（scope=private，默认）或共享区（scope=share，需员工权限）下的文件与子目录。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.FileRead },
            Params = new() { ["scope"] = "private 或 share（可选，默认 private）", ["path"] = "相对目录（可选，默认根目录）" },
            AllowedParamNames = new[] { "scope", "path" },
        },
        ["read_file"] = new ToolDefinition
        {
            Name = "read_file",
            Title = "读取文件",
            Description = "读取工作区内某个文本文件的内容（只读，最大 128KB）。"
                          + "若不确定确切文件名，先用 search_files 找到路径再读；一次要读多个文件用 read_files。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.FileRead },
            Params = new() { ["scope"] = "private 或 share（可选）", ["path"] = "相对文件路径（必填）" },
            RequiredParams = new[] { "path" },
            AllowedParamNames = new[] { "scope", "path" },
        },
        ["write_file"] = new ToolDefinition
        {
            Name = "write_file",
            Title = "写入文件",
            Description = "在工作区写入文本文件。mode=create 仅新建（文件已存在时会让你确认：追加/覆盖/改名）；overwrite 覆盖；append 追加。"
                          + "**私人工作区的普通文本文件：新增/追加/覆盖都允许自动执行**（覆盖前存快照可回退，审计照常记录）。"
                          + "以下情况会转人工审批：写入**共享工作区**、判定为**可执行/脚本**（后缀判断 ∪ 内容识别，例如 .bat/.ps1/.sh，"
                          + "或 .txt 里写着 PowerShell 下载执行/shebang/PE 头）、命中内容风险（外链、内网地址、编码载荷、注入话术…）。"
                          + "content 可以为空（创建空文件）；真二进制内容（含 NUL 字节）请改用 base64 文本文件。",
            RiskLevel = Risk.Medium,
            StateChanging = true,
            Permissions = new[] { Permissions.FileWrite },
            Params = new()
            {
                ["scope"] = "private 或 share（share 需要员工权限）",
                ["path"] = "相对文件路径（必填）",
                ["content"] = $"要写入的文本内容（可选，最多 {MaxFileContentChars} 字符；留空即空文件）",
                ["mode"] = "create | overwrite | append（可选，默认 create）",
                ["reason"] = "业务理由（可选，会展示给审批人）",
            },
            RequiredParams = new[] { "path" },
            AllowedParamNames = new[] { "scope", "path", "content", "mode", "reason" },
        },
        ["delete_file"] = new ToolDefinition
        {
            Name = "delete_file",
            Title = "删除文件",
            Description = "删除工作区内的文件（执行前自动保存快照，可一键回退）。"
                          + "**私人工作区的普通文件可以直接删除**；删除**共享工作区**文件或**可执行/脚本**文件必须人工审批"
                          + "（是否审批由规则引擎按区域与可执行性判定，不在这里静态写死）。"
                          + "**系统不支持批量/整目录删除**：请一次指定一个文件。",
            RiskLevel = Risk.Medium,
            // 注意：这里刻意不写 RequiresApproval=true —— 私人区普通文件的删除被允许直接执行（有快照），
            // 只有"共享工作区 / 可执行文件"才由规则引擎提升为高风险 + 必须审批。
            StateChanging = true,
            Permissions = new[] { Permissions.FileDelete },
            Params = new()
            {
                ["scope"] = "private 或 share（share 需要员工权限）",
                ["path"] = "相对文件路径（必填；系统不支持批量/整目录删除）",
                ["reason"] = "删除原因（建议填写，会展示给审批人）",
            },
            // 刻意不把 path 放进 RequiredParams：这样"清空工作区"这类请求会走到规则引擎里
            // 那条"不支持批量删除"的明确文案，而不是被泛化的"缺少必填参数"挡掉。
            AllowedParamNames = new[] { "scope", "path", "reason" },
        },
        ["query_records"] = new ToolDefinition
        {
            Name = "query_records",
            Title = "查询业务数据",
            Description = "查询业务数据集（只读）。dataset 取 customers/orders/contracts；可选按单个字段做 eq/contains/gte/lte 过滤。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.DataRead },
            Params = new()
            {
                ["dataset"] = "customers | orders | contracts（必填）",
                ["field"] = "过滤字段名（可选）",
                ["op"] = "eq | contains | gte | lte（可选，默认 eq）",
                ["value"] = "过滤值（可选）",
                ["limit"] = "返回条数上限（可选，1-50，默认 20）",
            },
            RequiredParams = new[] { "dataset" },
            AllowedParamNames = new[] { "dataset", "field", "op", "value", "limit" },
        },
        ["update_record"] = new ToolDefinition
        {
            Name = "update_record",
            Title = "修改业务数据",
            Description = "修改业务数据集的单个字段（高危：必须人工审批，执行前记录旧值以便反向回退）。",
            RiskLevel = Risk.High,
            RequiresApproval = true,
            StateChanging = true,
            Permissions = new[] { Permissions.DataWrite },
            Params = new()
            {
                ["dataset"] = "customers | orders | contracts（必填）",
                ["id"] = "记录主键（必填）",
                ["field"] = "要修改的字段名（必填，必须在白名单内）",
                ["value"] = "新的字段值（必填）",
                ["reason"] = "修改原因（建议填写）",
            },
            RequiredParams = new[] { "dataset", "id", "field", "value" },
            AllowedParamNames = new[] { "dataset", "id", "field", "value", "reason" },
        },
        ["delete_record"] = new ToolDefinition
        {
            Name = "delete_record",
            Title = "删除业务数据",
            Description = "删除业务数据集中的一行记录（极高危：仅管理员可用，必须人工审批，删除前整行转存 JSON 以便还原）。",
            RiskLevel = Risk.Critical,
            RequiresApproval = true,
            StateChanging = true,
            Permissions = new[] { Permissions.DataDelete },
            Params = new()
            {
                ["dataset"] = "customers | orders | contracts（必填）",
                ["id"] = "记录主键（必填）",
                ["reason"] = "删除原因（必填，会展示给审批人）",
            },
            RequiredParams = new[] { "dataset", "id", "reason" },
            AllowedParamNames = new[] { "dataset", "id", "reason" },
        },
        ["send_notice"] = new ToolDefinition
        {
            Name = "send_notice",
            Title = "发送通知",
            Description = "发送一条通知。收件人只允许 self（发给自己）或 approver（发给安全审批人）；"
                          + "不允许指定任意外部邮箱（防止把业务数据外发）。必须人工审批。",
            RiskLevel = Risk.Medium,
            RequiresApproval = true,
            StateChanging = true,
            Permissions = new[] { Permissions.NoticeSend },
            Params = new()
            {
                ["to"] = "self | approver（必填）",
                ["subject"] = "标题（必填，≤80 字符）",
                ["body"] = "正文（必填，≤1000 字符）",
                ["reason"] = "发送原因（可选）",
            },
            RequiredParams = new[] { "to", "subject", "body" },
            AllowedParamNames = new[] { "to", "subject", "body", "reason" },
        },
        ["my_tasks"] = new ToolDefinition
        {
            Name = "my_tasks",
            Title = "查看我的任务",
            Description = "查看自己提交过的操作任务及其审批状态（只读）。",
            RiskLevel = Risk.Low,
            Params = new(),
            AllowedParamNames = Array.Empty<string>(),
        },
        ["my_profile"] = new ToolDefinition
        {
            Name = "my_profile",
            Title = "查看我的权限",
            Description = "查看自己的角色与可用权限组（只读）。",
            RiskLevel = Risk.Low,
            Params = new(),
            AllowedParamNames = Array.Empty<string>(),
        },

        // ---------------- 甲方 / 乙方 对接业务（业务对接台） ----------------
        ["my_client"] = new ToolDefinition
        {
            Name = "my_client",
            Title = "查看我司档案",
            Description = "查看与自己账号绑定的甲方客户档案（只读）。未建档时会提示先提交建档申请。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.ClientRead },
            Params = new(),
            AllowedParamNames = Array.Empty<string>(),
        },
        ["create_client_profile"] = new ToolDefinition
        {
            Name = "create_client_profile",
            Title = "提交建档申请",
            Description = "甲方提交本公司档案（公司名称/联系人/联系电话/所属行业/需求摘要）。"
                          + "主键与账号绑定由服务端生成，客户端不能指定；必须经管理员审批通过后才会建档。",
            RiskLevel = Risk.Medium,
            RequiresApproval = true,
            StateChanging = true,
            Permissions = new[] { Permissions.ClientWrite },
            Params = new()
            {
                ["name"] = "公司名称（必填，≤40 字符）",
                ["contact"] = "联系人（必填，≤20 字符）",
                ["phone"] = "联系电话（必填，11 位手机号）",
                ["industry"] = "所属行业（可选：制造/科技/物流/教育/传媒/金融/其它）",
                ["requirement"] = "需求摘要（可选，≤200 字符）",
            },
            RequiredParams = new[] { "name", "contact", "phone" },
            AllowedParamNames = new[] { "name", "contact", "phone", "industry", "requirement" },
        },
        ["update_client_profile"] = new ToolDefinition
        {
            Name = "update_client_profile",
            Title = "修改客户档案",
            Description = "修改甲方客户档案的单个字段（高危：必须人工审批，执行前记录旧值以便回退）。"
                          + "甲方只能改自己的档案，且只能改公司名称/联系人/电话/行业/需求摘要；员工可改全部可写字段。"
                          + "**参数用名字即可**：传 client=公司名关键字（如“示例物流”），不必要求用户提供编号；"
                          + "枚举字段可以直接说口语（如“重要”“跟进”），系统会映射到合法取值。",
            RiskLevel = Risk.High,
            RequiresApproval = true,
            StateChanging = true,
            Permissions = new[] { Permissions.ClientWrite },
            Params = new()
            {
                ["client_id"] = "客户编号（可选；与 client 二选一）",
                ["client"] = "公司名/联系人关键字（推荐，员工用；甲方留空表示改自己的档案）",
                ["field"] = "字段名（甲方可用：name/contact/phone/industry/requirement；员工另可用：level/status/remark）",
                ["value"] = "新值（必填，枚举字段可写口语）",
                ["reason"] = "修改原因（建议填写，会展示给审批人）",
            },
            RequiredParams = new[] { "field", "value" },
            AllowedParamNames = new[] { "client_id", "client", "field", "value", "reason" },
        },
        ["assign_client_owner"] = new ToolDefinition
        {
            Name = "assign_client_owner",
            Title = "指派客户负责人",
            Description = "把客户档案的负责员工指派/变更为指定人员（员工与管理员可用，必须人工审批）。"
                          + "传 client=公司名关键字即可，不必提供编号。",
            RiskLevel = Risk.Medium,
            RequiresApproval = true,
            StateChanging = true,
            Permissions = new[] { Permissions.ClientAssign },
            Params = new()
            {
                ["client_id"] = "客户编号（可选；与 client 二选一）",
                ["client"] = "公司名/联系人关键字（推荐）",
                ["owner"] = "负责员工用户名（必填）",
                ["reason"] = "指派原因（建议填写）",
            },
            RequiredParams = new[] { "owner" },
            AllowedParamNames = new[] { "client_id", "client", "owner", "reason" },
        },
        ["search_clients"] = new ToolDefinition
        {
            Name = "search_clients",
            Title = "查询客户档案",
            Description = "按单个字段过滤查询甲方客户档案（只读，最多 50 条）。字段可取 name/contact/phone/industry/level/status/owner。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.ClientSearch },
            Params = new()
            {
                ["field"] = "过滤字段名（可选）",
                ["op"] = "eq | contains | gte | lte（可选，默认 eq）",
                ["value"] = "过滤值（可选）",
                ["limit"] = "返回条数（可选，1-50，默认 20）",
            },
            AllowedParamNames = new[] { "field", "op", "value", "limit" },
        },
        ["get_client_detail"] = new ToolDefinition
        {
            Name = "get_client_detail",
            Title = "查看客户详情",
            Description = "查看某个甲方客户的完整档案及其需求单（只读）。传 client=公司名关键字即可。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.ClientSearch },
            Params = new()
            {
                ["client_id"] = "客户编号（可选；与 client 二选一）",
                ["client"] = "公司名/联系人关键字（推荐）",
            },
            AllowedParamNames = new[] { "client_id", "client" },
        },
        ["submit_requirement"] = new ToolDefinition
        {
            Name = "submit_requirement",
            Title = "提交需求单",
            Description = "提交一条甲方需求单（必须人工审批后才会正式立项）。甲方只能给自己的档案提交；"
                          + "员工可以指定 customer=公司名关键字（或 customer_id）代客户提交。需求编号与提交人由服务端生成。",
            RiskLevel = Risk.Medium,
            RequiresApproval = true,
            StateChanging = true,
            Permissions = new[] { Permissions.ReqCreate },
            Params = new()
            {
                ["customer_id"] = "甲方客户编号（可选；与 customer 二选一）",
                ["customer"] = "公司名关键字（员工代客户提交时用）",
                ["title"] = "需求标题（必填，≤60 字符）",
                ["detail"] = "需求描述（必填，≤500 字符）",
                ["budget"] = "预算(元)（可选，数字）",
                ["expect_date"] = "期望交付日（可选，yyyy-MM-dd）",
            },
            RequiredParams = new[] { "title", "detail" },
            AllowedParamNames = new[] { "customer_id", "customer", "title", "detail", "budget", "expect_date" },
        },
        ["my_requirements"] = new ToolDefinition
        {
            Name = "my_requirements",
            Title = "查看我的需求单",
            Description = "查看与自己档案关联的需求单（只读）。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.ReqRead },
            Params = new(),
            AllowedParamNames = Array.Empty<string>(),
        },
        ["update_requirement"] = new ToolDefinition
        {
            Name = "update_requirement",
            Title = "维护需求单",
            Description = "修改需求单的单个字段（状态/预算/负责人/标题/描述/期望交付日；高危，必须人工审批）。"
                          + "**用标题关键字定位即可**：传 requirement=标题关键字（如“设备数据看板”），不必让用户背 R4001。",
            RiskLevel = Risk.High,
            RequiresApproval = true,
            StateChanging = true,
            Permissions = new[] { Permissions.ReqWrite },
            Params = new()
            {
                ["id"] = "需求编号（可选；与 requirement 二选一）",
                ["requirement"] = "需求标题关键字（推荐）",
                ["field"] = "字段名（title/detail/budget/expect_date/status/owner，必填）",
                ["value"] = "新值（必填，状态可写口语如“报价中/已完成”）",
                ["reason"] = "修改原因（建议填写）",
            },
            RequiredParams = new[] { "field", "value" },
            AllowedParamNames = new[] { "id", "requirement", "field", "value", "reason" },
        },

        // ---------------- 检索与统计（AI 多步干活的"眼睛"） ----------------
        ["search_files"] = new ToolDefinition
        {
            Name = "search_files",
            Title = "检索文件",
            Description = "在工作区里按关键字检索文件：可按**文件名关键字**（例如“日报”）、扩展名，或在**文件内容**里搜索。"
                          + "不知道确切文件名时先用它（例如用户说“读日报”，先搜“日报”再读命中的那个），不要凭空猜路径。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.FileRead },
            Params = new()
            {
                ["keyword"] = "文件名关键字（可选，例如 日报/周报/客户）",
                ["content"] = "在文件内容里搜索的关键字（可选）",
                ["ext"] = "扩展名过滤（可选，例如 txt / md）",
                ["path"] = "限定子目录（可选）",
                ["scope"] = "private 或 share（可选，默认 private）",
                ["limit"] = "返回条数上限（可选，1-100，默认 30）",
            },
            AllowedParamNames = new[] { "keyword", "content", "ext", "path", "scope", "limit" },
        },
        ["read_files"] = new ToolDefinition
        {
            Name = "read_files",
            Title = "批量读取文件",
            Description = "一次读取多个文本文件（最多 5 个）——需要看“全部文件/所有日报”时用它，避免一个个读。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.FileRead },
            Params = new()
            {
                ["paths"] = "相对路径列表，用逗号分隔（必填，最多 5 个）",
                ["scope"] = "private 或 share（可选，默认 private）",
            },
            RequiredParams = new[] { "paths" },
            AllowedParamNames = new[] { "paths", "scope" },
        },
        ["count_records"] = new ToolDefinition
        {
            Name = "count_records",
            Title = "业务数据统计",
            Description = "统计业务数据条数（客户 / 需求单 / 订单 / 合同）。回答“有多少客户/需求单”这类问题用它。"
                          + "甲方只能统计到本公司的数据，员工/管理员可统计全量。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.DataRead },
            Params = new() { ["dataset"] = "customers | requirements | orders | contracts（可选，默认全部）" },
            AllowedParamNames = new[] { "dataset" },
        },
        ["list_users"] = new ToolDefinition
        {
            Name = "list_users",
            Title = "查询账号名册",
            Description = "查询系统账号数量与名册（用户名、显示名、角色、状态、最后登录时间；**不返回任何口令信息**）。"
                          + "回答“有多少用户/有哪些员工”用它。仅管理员可用。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.UserRead },
            Params = new()
            {
                ["keyword"] = "按用户名/显示名过滤（可选）",
                ["role"] = "按角色过滤（可选：user / staff / admin）",
                ["limit"] = "返回条数上限（可选，1-100，默认 50）",
            },
            AllowedParamNames = new[] { "keyword", "role", "limit" },
        },
        ["find_client"] = new ToolDefinition
        {
            Name = "find_client",
            Title = "按名字找客户",
            Description = "用公司名/联系人/负责人的关键字找客户档案，返回候选（含编号）。"
                          + "**不需要用户提供 C1004 这种编号**：拿到候选后直接用公司名调用其它工具即可。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.ClientSearch },
            Params = new() { ["keyword"] = "公司名/联系人/负责人关键字（必填）", ["limit"] = "返回条数（可选，默认 10）" },
            RequiredParams = new[] { "keyword" },
            AllowedParamNames = new[] { "keyword", "limit" },
        },

        // ---------------- 文件管理（改名 / 建目录 / 文件信息） ----------------
        ["rename_file"] = new ToolDefinition
        {
            Name = "rename_file",
            Title = "重命名文件",
            Description = "在**同一目录**内给文件改名（含改扩展名）。改扩展名、或新名是脚本/可执行后缀（.bat/.ps1/.sh/.exe…）"
                          + "会被判定为风险操作并**强制转人工审批**（判定依据会写进审批上下文）；"
                          + "私人工作区里“后缀不变”的改名可以直接执行。",
            RiskLevel = Risk.Medium,
            StateChanging = true,
            Permissions = new[] { Permissions.FileWrite },
            Params = new()
            {
                ["scope"] = "private 或 share（可选，默认 private）",
                ["path"] = "原文件相对路径（必填）",
                ["new_name"] = "新文件名（必填，只写名字，不含目录）",
                ["reason"] = "改名原因（可选，会展示给审批人）",
            },
            RequiredParams = new[] { "path", "new_name" },
            AllowedParamNames = new[] { "scope", "path", "new_name", "reason" },
        },
        ["create_folder"] = new ToolDefinition
        {
            Name = "create_folder",
            Title = "新建目录",
            Description = "在工作区里新建目录（个人区默认低风险；共享区需要员工权限且需审批）。",
            RiskLevel = Risk.Low,
            StateChanging = true,
            Permissions = new[] { Permissions.FileWrite },
            Params = new()
            {
                ["scope"] = "private 或 share（可选，默认 private）",
                ["path"] = "要新建的目录相对路径（必填，可多级）",
                ["reason"] = "用途说明（可选）",
            },
            RequiredParams = new[] { "path" },
            AllowedParamNames = new[] { "scope", "path", "reason" },
        },
        ["get_file_info"] = new ToolDefinition
        {
            Name = "get_file_info",
            Title = "查看文件信息",
            Description = "查询某个文件/目录是否存在、大小、修改时间与 SHA-256（只读）。写文件前想确认是否已存在就用它。",
            RiskLevel = Risk.Low,
            Permissions = new[] { Permissions.FileRead },
            Params = new()
            {
                ["scope"] = "private 或 share（可选，默认 private）",
                ["path"] = "文件或目录相对路径（必填）",
            },
            RequiredParams = new[] { "path" },
            AllowedParamNames = new[] { "scope", "path" },
        },
        ["my_workspace"] = new ToolDefinition
        {
            Name = "my_workspace",
            Title = "我的可用区域与权限",
            Description = "说明**我能访问哪些区域**（个人区/共享区）、当前权限组、配额与文件统计（只读）。"
                          + "用户问“除了个人区还有什么/我有什么权限/我能不能用共享区”时用它回答，不要直接去访问共享区。",
            RiskLevel = Risk.Low,
            Permissions = Array.Empty<string>(),
            Params = new(),
            AllowedParamNames = Array.Empty<string>(),
        },
        ["my_notifications"] = new ToolDefinition
        {
            Name = "my_notifications",
            Title = "我的通知",
            Description = "查看发给自己的通知（含审批结果通知）（只读）。",
            RiskLevel = Risk.Low,
            Permissions = Array.Empty<string>(),
            Params = new() { ["limit"] = "返回条数（可选，1-50，默认 10）" },
            AllowedParamNames = new[] { "limit" },
        },
        ["cancel_my_task"] = new ToolDefinition
        {
            Name = "cancel_my_task",
            Title = "撤回我的申请",
            Description = "撤回**自己**尚未审批的操作申请（不会执行任何动作，只是取消排队）。用于“算了不要了/我提交错了”。",
            RiskLevel = Risk.Low,
            StateChanging = true,
            Permissions = Array.Empty<string>(),
            Params = new()
            {
                ["task_id"] = "任务号（可选；留空表示撤回自己最新一条待审批申请）",
                ["reason"] = "撤回原因（可选）",
            },
            AllowedParamNames = new[] { "task_id", "reason" },
        },

        // ---------------- 管理端 AI 专用（只读分析，绝不执行破坏性动作） ----------------
        ["a_pending_tasks"] = new ToolDefinition
        {
            Name = "a_pending_tasks",
            Title = "待审批任务清单",
            Description = "列出当前所有等待人工审批的任务摘要（管理端只读）。",
            RiskLevel = Risk.Low,
            AdminOnly = true,
            Permissions = new[] { Permissions.AdminRead },
            Params = new(),
            AllowedParamNames = Array.Empty<string>(),
        },
        ["a_task_detail"] = new ToolDefinition
        {
            Name = "a_task_detail",
            Title = "任务完整上下文",
            Description = "读取某个任务的完整上下文：用户、意图摘要、规范化动作、规则判定、AI 审核意见（管理端只读）。",
            RiskLevel = Risk.Low,
            AdminOnly = true,
            Permissions = new[] { Permissions.AdminRead },
            Params = new() { ["task_id"] = "任务 ID（必填）" },
            RequiredParams = new[] { "task_id" },
            AllowedParamNames = new[] { "task_id" },
        },
        ["a_audit_search"] = new ToolDefinition
        {
            Name = "a_audit_search",
            Title = "审计检索",
            Description = "在审计日志中按关键字检索最近记录（管理端只读）。",
            RiskLevel = Risk.Low,
            AdminOnly = true,
            Permissions = new[] { Permissions.AdminRead },
            Params = new() { ["keyword"] = "关键字（可选）", ["limit"] = "条数（可选，1-100）" },
            AllowedParamNames = new[] { "keyword", "limit" },
        },
        ["a_activity_summary"] = new ToolDefinition
        {
            Name = "a_activity_summary",
            Title = "使用情况汇总",
            Description = "按账号聚合**近期使用情况**（任务数、已执行/被拦/打回、AI 调用次数、最近动作时间），"
                          + "可选按用户名过滤与指定天数。回答“谁在用什么/最近有没有异常操作”优先用它，不要反复检索审计。",
            RiskLevel = Risk.Low,
            AdminOnly = true,
            Permissions = new[] { Permissions.AdminRead },
            Params = new()
            {
                ["user"] = "按用户名过滤（可选）",
                ["days"] = "统计最近 N 天（可选，1-90，默认 7）",
                ["limit"] = "最多返回几个账号（可选，1-50，默认 20）",
            },
            AllowedParamNames = new[] { "user", "days", "limit" },
        },
        ["a_task_list"] = new ToolDefinition
        {
            Name = "a_task_list",
            Title = "任务清单",
            Description = "查询任务清单（可按状态/提交人/工具过滤，返回条数与摘要，管理端只读）。",
            RiskLevel = Risk.Low,
            AdminOnly = true,
            Permissions = new[] { Permissions.AdminRead },
            Params = new()
            {
                ["status"] = "状态过滤（可选：PENDING_APPROVAL/EXECUTED/REJECTED_RULE/REJECTED_AI/REJECTED_ADMIN/EXPIRED/FAILED）",
                ["user"] = "按提交人过滤（可选）",
                ["tool"] = "按工具名过滤（可选）",
                ["limit"] = "返回条数（可选，1-100，默认 20）",
            },
            AllowedParamNames = new[] { "status", "user", "tool", "limit" },
        },
    };

    /// <summary>角色 → 权限组（最小权限原则：用户拿不到 data.write / data.delete）。</summary>
    public static readonly Dictionary<string, string[]> RolePermissions = new(StringComparer.Ordinal)
    {
        // 甲方（用户）：能维护**自己**的档案与需求单，看不到别的客户
        [Roles.User] = new[]
        {
            Permissions.FileRead, Permissions.FileWrite, Permissions.FileDelete,
            Permissions.DataRead, Permissions.NoticeSend,
            Permissions.ClientRead, Permissions.ClientWrite,
            Permissions.ReqRead, Permissions.ReqCreate,
        },
        // 乙方员工：能看/改全部客户档案、指派负责人、维护需求单，但不能删数据
        [Roles.Staff] = new[]
        {
            Permissions.FileRead, Permissions.FileWrite, Permissions.FileDelete,
            Permissions.ShareRead, Permissions.ShareWrite,
            Permissions.DataRead, Permissions.DataWrite, Permissions.NoticeSend,
            Permissions.ClientRead, Permissions.ClientSearch, Permissions.ClientWrite, Permissions.ClientAssign,
            Permissions.ReqRead, Permissions.ReqWrite, Permissions.ReqCreate,
        },
        [Roles.Admin] = new[]
        {
            Permissions.FileRead, Permissions.FileWrite, Permissions.FileDelete,
            Permissions.ShareRead, Permissions.ShareWrite,
            Permissions.DataRead, Permissions.DataWrite, Permissions.DataDelete,
            Permissions.NoticeSend, Permissions.AdminRead, Permissions.UserRead,
            Permissions.ClientRead, Permissions.ClientSearch, Permissions.ClientWrite, Permissions.ClientAssign,
            Permissions.ReqRead, Permissions.ReqWrite, Permissions.ReqCreate,
        },
    };

    public static string[] PermissionsOf(string role)
        => RolePermissions.TryGetValue(role, out var perms) ? perms : Array.Empty<string>();

    public static bool HasPermission(string role, string permission)
        => PermissionsOf(role).Contains(permission, StringComparer.Ordinal);

    public static ToolDefinition? Get(string name)
        => !string.IsNullOrWhiteSpace(name) && All.TryGetValue(name.Trim(), out var tool) ? tool : null;

    /// <summary>某角色可用的工具集合（管理端只读工具只对 admin 可见）。</summary>
    public static List<ToolDefinition> ForRole(string role)
        => All.Values
            .Where(t => !t.AdminOnly || role == Roles.Admin)
            .Where(t => t.Permissions.All(p => HasPermission(role, p)))
            .OrderBy(t => t.Name, StringComparer.Ordinal)
            .ToList();

    /// <summary>生成给 AI 看的工具说明（同时也决定了 AI "能想到什么"）。</summary>
    public static string SchemaForPrompt(string role)
    {
        var sb = new StringBuilder();
        foreach (var tool in ForRole(role))
        {
            sb.Append("- ").Append(tool.Name).Append("（").Append(tool.Title).Append("，风险：")
              .Append(Risk.Label(tool.RiskLevel))
              .Append(tool.RequiresApproval ? "，需要人工审批" : "")
              .Append("）：").Append(tool.Description).Append('\n');

            if (tool.Params.Count > 0)
            {
                sb.Append("    参数：");
                sb.Append(string.Join("；", tool.Params.Select(kv => $"{kv.Key}={kv.Value}")));
                sb.Append('\n');
            }

            if (tool.RequiredParams.Length > 0)
                sb.Append("    必填：").Append(string.Join(", ", tool.RequiredParams)).Append('\n');
        }
        return sb.ToString();
    }
}
