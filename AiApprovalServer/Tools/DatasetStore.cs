using System.Text;
using System.Text.RegularExpressions;
using AiApproval.Core;

namespace AiApproval.Tools;

public sealed class DatasetField
{
    public string Name { get; init; } = "";
    public string Label { get; init; } = "";
    /// <summary>text | int | decimal | enum | phone | date</summary>
    public string Type { get; init; } = "text";
    public bool Writable { get; init; } = true;
    public int MaxLength { get; init; } = 100;
    public string[]? EnumValues { get; init; }
}

public sealed class DatasetSchema
{
    public string Name { get; init; } = "";
    public string Label { get; init; } = "";
    public string KeyField { get; init; } = "id";
    /// <summary>主键前缀（用于自动生成编号，例如 C1007 / R4005）</summary>
    public string KeyPrefix { get; init; } = "X";
    public List<DatasetField> Fields { get; init; } = new();

    public DatasetField? Field(string name)
        => Fields.FirstOrDefault(f => string.Equals(f.Name, name, StringComparison.OrdinalIgnoreCase));
}

/// <summary>
/// 业务数据集定义（Schema 即白名单：字段名、类型、可写性都由代码固定，
/// 用户的/AI 的参数只能"命中"白名单，不能构造任意查询）。
///
/// 业务背景（星云游安全执行平台）：
///   customers 客户档案 / orders 订单 / contracts 合同
///   用户只读；员工可改字段但不能删行；删除行只有管理员能做。
/// </summary>
public static class DatasetCatalog
{
    public static readonly Dictionary<string, DatasetSchema> All = new(StringComparer.OrdinalIgnoreCase)
    {
        ["customers"] = new DatasetSchema
        {
            Name = "customers",
            Label = "甲方客户档案",
            KeyPrefix = "C",
            Fields =
            {
                new DatasetField { Name = "id", Label = "客户编号", Type = "text", Writable = false, MaxLength = 24 },
                new DatasetField { Name = "name", Label = "公司名称", Type = "text", MaxLength = 40 },
                new DatasetField { Name = "contact", Label = "联系人", Type = "text", MaxLength = 20 },
                new DatasetField { Name = "phone", Label = "联系电话", Type = "phone", MaxLength = 11 },
                new DatasetField { Name = "industry", Label = "所属行业", Type = "enum", EnumValues = new[] { "制造", "科技", "物流", "教育", "传媒", "金融", "其它" } },
                new DatasetField { Name = "level", Label = "客户等级", Type = "enum", EnumValues = new[] { "普通", "白银", "黄金", "铂金" } },
                new DatasetField { Name = "status", Label = "合作状态", Type = "enum", EnumValues = new[] { "潜在", "洽谈中", "合作中", "已暂停", "已终止" } },
                new DatasetField { Name = "owner", Label = "负责员工", Type = "text", MaxLength = 32 },
                new DatasetField { Name = "requirement", Label = "需求摘要", Type = "text", MaxLength = 200 },
                new DatasetField { Name = "remark", Label = "备注", Type = "text", MaxLength = 200 },
                new DatasetField { Name = "account_user_id", Label = "关联甲方账号", Type = "text", Writable = false, MaxLength = 32 },
                new DatasetField { Name = "updated_at", Label = "更新时间", Type = "date", Writable = false, MaxLength = 10 },
            },
        },
        ["requirements"] = new DatasetSchema
        {
            Name = "requirements",
            Label = "甲方需求单",
            KeyPrefix = "R",
            Fields =
            {
                new DatasetField { Name = "id", Label = "需求编号", Type = "text", Writable = false, MaxLength = 24 },
                new DatasetField { Name = "customer_id", Label = "甲方客户编号", Type = "text", Writable = false, MaxLength = 24 },
                new DatasetField { Name = "title", Label = "需求标题", Type = "text", MaxLength = 60 },
                new DatasetField { Name = "detail", Label = "需求描述", Type = "text", MaxLength = 500 },
                new DatasetField { Name = "budget", Label = "预算(元)", Type = "decimal", MaxLength = 16 },
                new DatasetField { Name = "expect_date", Label = "期望交付日", Type = "date", MaxLength = 10 },
                new DatasetField { Name = "status", Label = "需求状态", Type = "enum", EnumValues = new[] { "待评估", "已报价", "进行中", "已交付", "已关闭" } },
                new DatasetField { Name = "owner", Label = "负责员工", Type = "text", MaxLength = 32 },
                new DatasetField { Name = "created_by", Label = "提交账号", Type = "text", Writable = false, MaxLength = 32 },
                new DatasetField { Name = "created_at", Label = "创建日期", Type = "date", Writable = false, MaxLength = 10 },
                new DatasetField { Name = "updated_at", Label = "更新时间", Type = "date", Writable = false, MaxLength = 10 },
            },
        },
        ["orders"] = new DatasetSchema
        {
            Name = "orders",
            Label = "订单记录",
            KeyPrefix = "O",
            Fields =
            {
                new DatasetField { Name = "id", Label = "订单号", Type = "text", Writable = false, MaxLength = 24 },
                new DatasetField { Name = "customer_id", Label = "客户编号", Type = "text", MaxLength = 24 },
                new DatasetField { Name = "amount", Label = "金额(元)", Type = "decimal", MaxLength = 16 },
                new DatasetField { Name = "status", Label = "订单状态", Type = "enum", EnumValues = new[] { "待付款", "已付款", "已发货", "已完成", "已取消" } },
                new DatasetField { Name = "created_at", Label = "创建日期", Type = "date", MaxLength = 10 },
            },
        },
        ["contracts"] = new DatasetSchema
        {
            Name = "contracts",
            Label = "合同台账",
            KeyPrefix = "K",
            Fields =
            {
                new DatasetField { Name = "id", Label = "合同编号", Type = "text", Writable = false, MaxLength = 24 },
                new DatasetField { Name = "customer_id", Label = "客户编号", Type = "text", MaxLength = 24 },
                new DatasetField { Name = "amount", Label = "合同金额(元)", Type = "decimal", MaxLength = 16 },
                new DatasetField { Name = "sign_date", Label = "签署日期", Type = "date", MaxLength = 10 },
                new DatasetField { Name = "status", Label = "状态", Type = "enum", EnumValues = new[] { "草拟", "待签", "生效中", "已归档" } },
            },
        },
    };

    public static bool IsKnown(string? name) => !string.IsNullOrWhiteSpace(name) && All.ContainsKey(name.Trim());

    /// <summary>
    /// 生成给上游 AI 看的"数据集字段清单"。
    /// 没有这份清单，模型只能猜字段名（例如把 level 猜成"等级"），规则引擎会直接判非法——猜字段名是实现里的真实坑。
    /// </summary>
    public static string DescribeForPrompt()
    {
        var sb = new StringBuilder();
        foreach (var schema in All.Values)
        {
            sb.Append("- ").Append(schema.Name).Append("（").Append(schema.Label).Append("，主键 ")
              .Append(schema.KeyField).Append("）：");

            sb.Append(string.Join("；", schema.Fields.Select(f =>
            {
                var type = f.Type == "enum" && f.EnumValues is not null
                    ? "枚举[" + string.Join("/", f.EnumValues) + "]"
                    : f.Type switch
                    {
                        "int" => "整数",
                        "decimal" => "金额",
                        "phone" => "手机号",
                        "date" => "日期yyyy-MM-dd",
                        _ => "文本",
                    };
                return $"{f.Name}({f.Label},{type}{(f.Writable ? "" : ",只读")})";
            })));

            sb.Append('\n');
        }
        return sb.ToString();
    }

    public static List<Dictionary<string, string>> CreateSeedRows(string dataset) => dataset.ToLowerInvariant() switch
    {
        "customers" => new List<Dictionary<string, string>>
        {
            ClientRow("C1001", "星云游科技", "张三", "13800000001", "科技", "铂金", "合作中", "lisi",
                "年度框架合作，续约与容量扩容", "年度客户，季度回访", "2026-01-08"),
            ClientRow("C1002", "示例数据服务", "李四", "13800000002", "科技", "黄金", "合作中", "lisi",
                "需要数据接入与看板支持", "关注数据接入产品线", "2026-01-12"),
            ClientRow("C1003", "示例智造集团", "王五", "13800000003", "制造", "黄金", "洽谈中", "lisi",
                "车间设备数据看板，含报警推送", "需要线下拜访", "2026-02-02"),
            ClientRow("C1004", "示例物流", "赵六", "13800000004", "物流", "普通", "潜在", "",
                "调度优化试点，预算待确认", "试用期客户，尚未指派负责人", "2026-02-11"),
            ClientRow("C1005", "示例传媒", "钱七", "13800000005", "传媒", "白银", "已暂停", "lisi",
                "内容投放数据看板（暂缓）", "预算受限，暂缓推进", "2026-02-19"),
            ClientRow("C1006", "示例教育", "孙八", "13800000006", "教育", "普通", "潜在", "lisi",
                "在线课堂试点与数据统计", "待补充联系人", "2026-03-01"),
        },
        "requirements" => new List<Dictionary<string, string>>
        {
            RequirementRow("R4001", "C1003", "车间设备数据看板",
                "把三号车间的设备数据接入看板，需要报警推送与班次统计。", "380000.00", "2026-07-15", "待评估", "", "zhangsan", "2026-03-02", "2026-03-02"),
            RequirementRow("R4002", "C1001", "年度框架续约与扩容",
                "在现有框架基础上续约一年，并将并发额度提升 20%。", "1500000.00", "2026-06-30", "进行中", "lisi", "lisi", "2026-02-10", "2026-03-05"),
            RequirementRow("R4003", "C1002", "数据接入支持",
                "协助完成数据源接入，交付接入文档并完成一次培训。", "86000.00", "2026-04-30", "已报价", "lisi", "lisi", "2026-02-14", "2026-03-01"),
            RequirementRow("R4004", "C1006", "在线课堂试点",
                "面向 3 个班级的在线课堂试点，含课堂数据统计。", "45000.00", "2026-03-31", "已关闭", "lisi", "lisi", "2026-01-20", "2026-02-28"),
        },
        "orders" => new List<Dictionary<string, string>>
        {
            OrderRow("O2001", "C1001", "128000.00", "已完成", "2026-01-09"),
            OrderRow("O2002", "C1002", "86000.50", "已发货", "2026-01-15"),
            OrderRow("O2003", "C1003", "43000.00", "已付款", "2026-02-05"),
            OrderRow("O2004", "C1004", "9800.00", "待付款", "2026-02-12"),
            OrderRow("O2005", "C1005", "15600.00", "已取消", "2026-02-20"),
            OrderRow("O2006", "C1001", "204000.00", "已付款", "2026-03-02"),
        },
        "contracts" => new List<Dictionary<string, string>>
        {
            ContractRow("K3001", "C1001", "1500000.00", "2026-01-10", "生效中"),
            ContractRow("K3002", "C1002", "620000.00", "2026-01-20", "生效中"),
            ContractRow("K3003", "C1003", "380000.00", "2026-02-08", "待签"),
            ContractRow("K3004", "C1005", "120000.00", "2026-02-25", "草拟"),
        },
        _ => new List<Dictionary<string, string>>(),
    };

    private static Dictionary<string, string> ClientRow(string id, string name, string contact, string phone, string industry,
        string level, string status, string owner, string requirement, string remark, string updatedAt)
        => new(StringComparer.OrdinalIgnoreCase)
        {
            ["id"] = id, ["name"] = name, ["contact"] = contact, ["phone"] = phone,
            ["industry"] = industry, ["level"] = level, ["status"] = status, ["owner"] = owner,
            ["requirement"] = requirement, ["remark"] = remark, ["account_user_id"] = "", ["updated_at"] = updatedAt,
        };

    private static Dictionary<string, string> RequirementRow(string id, string customerId, string title, string detail,
        string budget, string expectDate, string status, string owner, string createdBy, string createdAt, string updatedAt)
        => new(StringComparer.OrdinalIgnoreCase)
        {
            ["id"] = id, ["customer_id"] = customerId, ["title"] = title, ["detail"] = detail,
            ["budget"] = budget, ["expect_date"] = expectDate, ["status"] = status, ["owner"] = owner,
            ["created_by"] = createdBy, ["created_at"] = createdAt, ["updated_at"] = updatedAt,
        };

    private static Dictionary<string, string> OrderRow(string id, string customerId, string amount, string status, string createdAt)
        => new(StringComparer.OrdinalIgnoreCase)
        {
            ["id"] = id, ["customer_id"] = customerId, ["amount"] = amount,
            ["status"] = status, ["created_at"] = createdAt,
        };

    private static Dictionary<string, string> ContractRow(string id, string customerId, string amount, string signDate, string status)
        => new(StringComparer.OrdinalIgnoreCase)
        {
            ["id"] = id, ["customer_id"] = customerId, ["amount"] = amount,
            ["sign_date"] = signDate, ["status"] = status,
        };
}

/// <summary>
/// 业务数据读写（JSON 文件充当数据库表）。
///
/// 这里刻意不做任何字符串拼接式查询：字段名必须命中 Schema 白名单，值必须通过类型校验，
/// 因此"注入"在数据层没有落点（这也是"JSON 当数据库"的额外好处）。
/// </summary>
public sealed class DatasetStore
{
    private static readonly Regex PhoneRx = new("^1[3-9]\\d{9}$", RegexOptions.Compiled);
    private static readonly Regex DateRx = new("^\\d{4}-\\d{2}-\\d{2}$", RegexOptions.Compiled);

    /// <summary>
    /// 枚举字段的"口语别名"表：让用户与 AI 都不必背枚举值。
    /// 例：客户等级说"重要 / 高价值 / 大客户" → 铂金；合作状态说"跟进 / 在谈" → 洽谈中。
    ///
    /// 设计取舍：只做**同义收敛**，不做语义自由发挥——匹配不上就照旧报错并列出可选值，
    /// 绝不让系统"自己编一个枚举值"填进去（审批人看到的必须是可解释的映射）。
    /// </summary>
    private static readonly Dictionary<string, (string[] Aliases, string Value)[]> EnumAliases = new(StringComparer.OrdinalIgnoreCase)
    {
        ["客户等级"] = new (string[], string)[]
        {
            (new[] { "重要", "很重要", "高价值", "大客户", "重点", "vip", "顶级", "最高" }, "铂金"),
            (new[] { "较高", "中高", "优质", "核心" }, "黄金"),
            (new[] { "中等", "一般", "常规" }, "白银"),
            (new[] { "低", "次要", "小客户", "低价值" }, "普通"),
        },
        ["合作状态"] = new (string[], string)[]
        {
            (new[] { "意向", "线索", "待开发", "陌生" }, "潜在"),
            (new[] { "洽谈", "在谈", "跟进", "沟通中", "谈判" }, "洽谈中"),
            (new[] { "合作", "成交", "签约", "已签约", "在合作", "活跃" }, "合作中"),
            (new[] { "暂停", "搁置", "暂缓", "待定" }, "已暂停"),
            (new[] { "终止", "流失", "结束", "不再合作" }, "已终止"),
        },
        ["需求状态"] = new (string[], string)[]
        {
            (new[] { "评估", "评估中", "评审中", "新提交" }, "待评估"),
            (new[] { "报价", "出价", "已给报价" }, "已报价"),
            (new[] { "进行", "在做", "开发中", "执行中", "已开工" }, "进行中"),
            (new[] { "交付", "完成", "已完成", "上线", "搞定了" }, "已交付"),
            (new[] { "关闭", "取消", "作废", "不做了" }, "已关闭"),
        },
        ["订单状态"] = new (string[], string)[]
        {
            (new[] { "未付款", "待付", "待支付" }, "待付款"),
            (new[] { "已付", "付款完成", "已支付" }, "已付款"),
            (new[] { "发货", "已发出", "在途" }, "已发货"),
            (new[] { "完成", "已收货", "结单" }, "已完成"),
            (new[] { "取消", "作废" }, "已取消"),
        },
        ["合同状态"] = new (string[], string)[]
        {
            (new[] { "起草", "草稿", "拟定" }, "草拟"),
            (new[] { "等签", "审批中" }, "待签"),
            (new[] { "生效", "执行中", "有效" }, "生效中"),
            (new[] { "归档", "完成" }, "已归档"),
        },
        ["所属行业"] = new (string[], string)[]
        {
            (new[] { "制造业", "工厂", "工业", "车间" }, "制造"),
            (new[] { "it", "互联网", "软件", "科技公司", "技术" }, "科技"),
            (new[] { "运输", "快递", "仓储" }, "物流"),
            (new[] { "学校", "培训", "教育机构" }, "教育"),
            (new[] { "广告", "媒体", "文化" }, "传媒"),
            (new[] { "银行", "保险", "证券" }, "金融"),
            (new[] { "其他", "其它行业" }, "其它"),
        },
    };

    private readonly JsonStore _store;

    public DatasetStore(JsonStore store, AppConfig cfg)
    {
        _store = store;
        _ = cfg;
    }

    private static string FileOf(string dataset) => $"{StoreNames.Datasets}/{dataset.ToLowerInvariant()}.json";

    public async Task<List<Dictionary<string, string>>> RowsAsync(string dataset)
        => await _store.ReadListAsync<Dictionary<string, string>>(FileOf(dataset)).ConfigureAwait(false);

    public async Task EnsureSeedAsync()
    {
        foreach (var name in DatasetCatalog.All.Keys)
        {
            var rows = await RowsAsync(name).ConfigureAwait(false);
            if (rows.Count == 0)
            {
                var seed = DatasetCatalog.CreateSeedRows(name);
                if (seed.Count > 0)
                    await _store.WriteAtomicAsync(FileOf(name), seed).ConfigureAwait(false);
            }
        }
    }

    /// <summary>按某字段精确查找第一行（例如按 account_user_id 找甲方档案）。</summary>
    public async Task<Dictionary<string, string>?> FindByFieldAsync(string dataset, string field, string value)
        => (await ListByFieldAsync(dataset, field, value, 1).ConfigureAwait(false)).FirstOrDefault();

    public async Task<List<Dictionary<string, string>>> ListByFieldAsync(string dataset, string field, string value, int limit = 50)
    {
        var rows = await RowsAsync(dataset).ConfigureAwait(false);
        return rows.Where(r => Get(r, field).Equals(value, StringComparison.OrdinalIgnoreCase))
            .Take(Math.Clamp(limit, 1, 200))
            .ToList();
    }

    /// <summary>生成下一个主键（C1007 / R4005 之类）：同前缀内取最大数字 +1。</summary>
    public async Task<string> NextKeyAsync(string dataset)
    {
        var schema = DatasetCatalog.All[dataset];
        var rows = await RowsAsync(dataset).ConfigureAwait(false);

        var max = 1000;
        foreach (var row in rows)
        {
            var id = Get(row, schema.KeyField);
            if (id.Length < 2 || !char.IsLetter(id[0])) continue;
            if (!string.Equals(id[0].ToString(), schema.KeyPrefix, StringComparison.OrdinalIgnoreCase)) continue;
            if (int.TryParse(id[1..], out var number) && number > max) max = number;
        }

        return schema.KeyPrefix + (max + 1).ToString();
    }

    /// <summary>
    /// 新建一行（业务建档 / 建单专用）。
    /// 字段与取值都必须过 Schema 校验，**主键由服务端生成**，客户端无法指定主键，也就无法"顺手覆盖已有记录"。
    /// 只读字段（主键、关联账号、创建时间…）由服务端填写，客户端传值一律忽略。
    /// </summary>
    public async Task<(bool Ok, string Error, string Id)> CreateRowAsync(string dataset, Dictionary<string, string> values)
    {
        if (!DatasetCatalog.IsKnown(dataset)) return (false, "未知数据集", "");

        var schema = DatasetCatalog.All[dataset];
        var row = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var today = DateTime.UtcNow.ToString("yyyy-MM-dd");
        var readOnlyValues = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        foreach (var kv in values)
            readOnlyValues[kv.Key] = kv.Value ?? "";

        foreach (var field in schema.Fields)
        {
            if (!field.Writable)
            {
                row[field.Name] = field.Name switch
                {
                    "created_at" or "updated_at" => today,
                    _ => "",
                };
                continue;
            }

            var raw = readOnlyValues.TryGetValue(field.Name, out var v) ? v : "";

            if (string.IsNullOrWhiteSpace(raw))
            {
                if (field.Type == "enum" && field.EnumValues is not null && field.Name == "status")
                    row[field.Name] = field.EnumValues[0];
                else
                    row[field.Name] = "";
                continue;
            }

            var error = ValidateValue(schema, field.Name, raw.Trim(), out var normalized);
            if (error is not null) return (false, error, "");

            row[field.Name] = normalized;
        }

        var id = await NextKeyAsync(dataset).ConfigureAwait(false);

        await _store.MutateAsync<Dictionary<string, string>, bool>(FileOf(dataset), rows =>
        {
            row[schema.KeyField] = id;
            rows.Add(row);
            return true;
        }).ConfigureAwait(false);

        return (true, "", id);
    }

    public async Task<(bool Ok, string Error, List<Dictionary<string, string>> Rows, string Query)> QueryAsync(
        string dataset, string? field, string? op, string? value, int limit)
    {
        if (!DatasetCatalog.IsKnown(dataset))
            return (false, $"未知数据集：{dataset}", new(), "");

        var schema = DatasetCatalog.All[dataset];
        limit = Math.Clamp(limit <= 0 ? 20 : limit, 1, 50);

        var rows = await RowsAsync(dataset).ConfigureAwait(false);

        var where = "";
        if (!string.IsNullOrWhiteSpace(field))
        {
            var f = schema.Field(field);
            if (f is null) return (false, $"数据集 {schema.Label} 不存在字段：{field}", new(), "");

            var normalizedOp = (op ?? "eq").Trim().ToLowerInvariant() switch
            {
                "eq" or "=" => "eq",
                "contains" or "like" => "contains",
                "gte" or ">=" => "gte",
                "lte" or "<=" => "lte",
                _ => "",
            };
            if (normalizedOp.Length == 0) return (false, $"不支持的比较方式：{op}", new(), "");

            var rawValue = (value ?? "").Trim();
            if (rawValue.Length > 100) return (false, "查询值过长", new(), "");

            rows = normalizedOp switch
            {
                "eq" => rows.Where(r => Get(r, f.Name).Equals(rawValue, StringComparison.OrdinalIgnoreCase)).ToList(),
                "contains" => rows.Where(r => Get(r, f.Name).Contains(rawValue, StringComparison.OrdinalIgnoreCase)).ToList(),
                "gte" => rows.Where(r => CompareDecimal(Get(r, f.Name), rawValue) >= 0).ToList(),
                "lte" => rows.Where(r => CompareDecimal(Get(r, f.Name), rawValue) <= 0).ToList(),
                _ => rows,
            };

            where = normalizedOp switch
            {
                "eq" => $"{f.Name} = '{rawValue}'",
                "contains" => $"{f.Name} LIKE '%{rawValue}%'",
                "gte" => $"{f.Name} >= {rawValue}",
                _ => $"{f.Name} <= {rawValue}",
            };
        }

        var query = $"SELECT * FROM {dataset}" + (where.Length > 0 ? $" WHERE {where}" : "") + $" LIMIT {limit}";
        return (true, "", rows.Take(limit).ToList(), query);
    }

    public async Task<Dictionary<string, string>?> FindRowAsync(string dataset, string id)
    {
        var rows = await RowsAsync(dataset).ConfigureAwait(false);
        var key = DatasetCatalog.All[dataset].KeyField;
        return rows.FirstOrDefault(r => Get(r, key).Equals(id, StringComparison.OrdinalIgnoreCase));
    }

    /// <summary>
    /// 服务端专用字段写入（不是用户/ AI 工具）：用于写只读字段，例如 account_user_id（账号↔档案绑定）、created_by、时间戳。
    /// 只读字段用户端与 AI 都无法改写，只有这里能写，避免"自己把别人的档案绑定到自己账号上"这类越权。
    /// </summary>
    public async Task<bool> SetFieldServerSideAsync(string dataset, string rowId, string field, string value)
    {
        if (!DatasetCatalog.IsKnown(dataset)) return false;
        var schema = DatasetCatalog.All[dataset];

        return await _store.MutateAsync<Dictionary<string, string>, bool>(FileOf(dataset), rows =>
        {
            var row = rows.FirstOrDefault(r => Get(r, schema.KeyField).Equals(rowId, StringComparison.OrdinalIgnoreCase));
            if (row is null) return false;

            var key = row.Keys.FirstOrDefault(k => string.Equals(k, field, StringComparison.OrdinalIgnoreCase)) ?? field;
            row[key] = value;
            return true;
        }).ConfigureAwait(false);
    }

    /// <summary>
    /// 旧数据迁移：给已存在的行补齐 Schema 里新增的字段（缺字段会让列表/详情出现空洞）。
    /// 只补空值，不覆盖任何已有数据。
    /// </summary>
    public async Task<int> EnsureSchemaFieldsAsync(string dataset)
    {
        if (!DatasetCatalog.IsKnown(dataset)) return 0;
        var schema = DatasetCatalog.All[dataset];

        return await _store.MutateAsync<Dictionary<string, string>, int>(FileOf(dataset), rows =>
        {
            var patched = 0;
            foreach (var row in rows)
            {
                foreach (var field in schema.Fields)
                {
                    if (row.Keys.Any(k => string.Equals(k, field.Name, StringComparison.OrdinalIgnoreCase))) continue;

                    row[field.Name] = field.Name switch
                    {
                        "industry" => "其它",
                        "status" when field.EnumValues is { Length: > 0 } => field.EnumValues[0],
                        _ => "",
                    };
                    patched++;
                }
            }
            return patched;
        }).ConfigureAwait(false);
    }

    public string? ValidateValue(DatasetSchema schema, string field, string value, out string normalized)
    {
        normalized = "";
        var f = schema.Field(field);
        if (f is null) return $"数据集 {schema.Label} 不存在字段：{field}";
        if (!f.Writable) return $"字段 {f.Label} 只读，不允许修改";
        if (value is null) return "值不能为空";
        if (value.Length > f.MaxLength) return $"字段 {f.Label} 长度不得超过 {f.MaxLength}";
        if (value.Any(c => char.IsControl(c) && c != '\t')) return "值包含控制字符";

        switch (f.Type)
        {
            case "int":
                if (!int.TryParse(value, out var iv) || iv < 0 || iv > 1_000_000_000) return $"{f.Label} 必须是 0-1000000000 的整数";
                normalized = iv.ToString();
                break;
            case "decimal":
                if (!decimal.TryParse(value, out var dv) || dv < 0 || dv > 1_000_000_000m) return $"{f.Label} 必须是非负金额";
                normalized = dv.ToString("0.00");
                break;
            case "enum":
            {
                var options = f.EnumValues ?? Array.Empty<string>();
                var exact = options.FirstOrDefault(o => o.Equals(value, StringComparison.OrdinalIgnoreCase));
                if (exact is not null) { normalized = exact; break; }

                // 口语说法自动收敛到枚举（"重要"→铂金、"跟进"→洽谈中）；映射不上就报错并列出可选值
                var mapped = MapEnumValue(f.Label, value, options);
                if (mapped is null)
                    return $"{f.Label}只能是：{string.Join(" / ", options)}（也接受常见说法，例如“重要”“跟进”“已完成”）";
                normalized = mapped;
                break;
            }
            case "phone":
                if (!PhoneRx.IsMatch(value)) return $"{f.Label} 必须是 11 位手机号";
                normalized = value;
                break;
            case "date":
                if (!DateRx.IsMatch(value) || !DateTime.TryParse(value, out _)) return $"{f.Label} 必须是 yyyy-MM-dd 格式的有效日期";
                normalized = value;
                break;
            default:
                if (value.Length == 0) return $"{f.Label} 不能为空";
                normalized = value;
                break;
        }

        return null;
    }

    /// <summary>更新单字段。返回旧值供"反向 SQL / Undo Log"使用。</summary>
    public async Task<(bool Ok, string Error, string OldValue, Dictionary<string, string>? Row)> UpdateFieldAsync(
        string dataset, string id, string field, string value)
    {
        if (!DatasetCatalog.IsKnown(dataset)) return (false, "未知数据集", "", null);
        var schema = DatasetCatalog.All[dataset];

        var error = ValidateValue(schema, field, value, out var normalized);
        if (error is not null) return (false, error, "", null);

        var result = await _store.MutateAsync<Dictionary<string, string>, (bool, string, string, Dictionary<string, string>?)>(
            FileOf(dataset), rows =>
            {
                var key = schema.KeyField;
                var row = rows.FirstOrDefault(r => Get(r, key).Equals(id, StringComparison.OrdinalIgnoreCase));
                if (row is null) return (false, "记录不存在", "", null);

                var old = Get(row, field);
                var target = row.FirstOrDefault(kv => string.Equals(kv.Key, field, StringComparison.OrdinalIgnoreCase));
                if (string.IsNullOrEmpty(target.Key)) row[field] = normalized;
                else row[target.Key] = normalized;

                if (schema.Field("updated_at") is not null)
                {
                    var stamp = row.FirstOrDefault(kv => string.Equals(kv.Key, "updated_at", StringComparison.OrdinalIgnoreCase));
                    if (!string.IsNullOrEmpty(stamp.Key)) row[stamp.Key] = DateTime.UtcNow.ToString("yyyy-MM-dd");
                }

                var snapshot = new Dictionary<string, string>(row, StringComparer.OrdinalIgnoreCase);
                return (true, "", old, snapshot);
            }).ConfigureAwait(false);

        return (result.Item1, result.Item2, result.Item3, result.Item4);
    }

    public async Task<(bool Ok, string Error, string RowJson)> DeleteRowAsync(string dataset, string id)
    {
        if (!DatasetCatalog.IsKnown(dataset)) return (false, "未知数据集", "");

        var schema = DatasetCatalog.All[dataset];
        return await _store.MutateAsync<Dictionary<string, string>, (bool, string, string)>(FileOf(dataset), rows =>
        {
            var key = schema.KeyField;
            var row = rows.FirstOrDefault(r => Get(r, key).Equals(id, StringComparison.OrdinalIgnoreCase));
            if (row is null) return (false, "记录不存在", "");

            var json = AppJson.Serialize(row, disk: true);
            rows.Remove(row);
            return (true, "", json);
        }).ConfigureAwait(false);
    }

    public async Task<(bool Ok, string Error)> InsertRowAsync(string dataset, string rowJson)
    {
        if (!DatasetCatalog.IsKnown(dataset)) return (false, "未知数据集");

        var row = AppJson.Deserialize<Dictionary<string, string>>(rowJson);
        if (row is null) return (false, "行数据非法");

        var schema = DatasetCatalog.All[dataset];
        row = new Dictionary<string, string>(row, StringComparer.OrdinalIgnoreCase);
        if (!row.ContainsKey(schema.KeyField)) return (false, "缺少主键");

        return await _store.MutateAsync<Dictionary<string, string>, (bool, string)>(FileOf(dataset), rows =>
        {
            var key = schema.KeyField;
            rows.RemoveAll(r => Get(r, key).Equals(Get(row, key), StringComparison.OrdinalIgnoreCase));
            rows.Add(row);
            return (true, "");
        }).ConfigureAwait(false);
    }

    /// <summary>按字段集合恢复一行（回退 update 用）。</summary>
    public async Task<(bool Ok, string Error)> RestoreFieldsAsync(string dataset, string id, Dictionary<string, string> fields)
    {
        if (!DatasetCatalog.IsKnown(dataset)) return (false, "未知数据集");

        var schema = DatasetCatalog.All[dataset];
        return await _store.MutateAsync<Dictionary<string, string>, (bool, string)>(FileOf(dataset), rows =>
        {
            var key = schema.KeyField;
            var row = rows.FirstOrDefault(r => Get(r, key).Equals(id, StringComparison.OrdinalIgnoreCase));
            if (row is null) return (false, "记录不存在，无法回退");

            foreach (var kv in fields)
            {
                var target = row.FirstOrDefault(x => string.Equals(x.Key, kv.Key, StringComparison.OrdinalIgnoreCase));
                if (string.IsNullOrEmpty(target.Key)) row[kv.Key] = kv.Value;
                else row[target.Key] = kv.Value;
            }
            return (true, "");
        }).ConfigureAwait(false);
    }

    private static string Get(Dictionary<string, string> row, string field)
    {
        var kv = row.FirstOrDefault(x => string.Equals(x.Key, field, StringComparison.OrdinalIgnoreCase));
        return kv.Key is null ? "" : kv.Value;
    }

    private static int CompareDecimal(string left, string right)
    {
        var okLeft = decimal.TryParse(left, out var l);
        var okRight = decimal.TryParse(right, out var r);
        if (okLeft && okRight) return l.CompareTo(r);
        return string.CompareOrdinal(left, right);
    }

    /// <summary>
    /// 把口语说法映射到枚举值（"重要"→铂金、"跟进"→洽谈中、"搞定了"→已交付…）。
    /// 匹配不上返回 null —— 由调用方给出"可选值"错误，绝不让系统自己编一个值填进去。
    /// </summary>
    public static string? MapEnumValue(string fieldLabel, string input, string[] options)
    {
        if (options.Length == 0) return null;
        var v = Squash(input);
        if (v.Length == 0) return null;

        var exact = options.FirstOrDefault(o => Squash(o) == v);
        if (exact is not null) return exact;

        foreach (var table in EnumAliases)
        {
            if (!LabelMatches(fieldLabel, table.Key)) continue;

            // ① 别名全等
            foreach (var (aliases, value) in table.Value)
            {
                if (!options.Contains(value, StringComparer.Ordinal)) continue;
                if (aliases.Any(a => Squash(a) == v)) return value;
            }

            // ② 别名包含（"把它标记为重要客户" / "已完成状态"）
            foreach (var (aliases, value) in table.Value)
            {
                if (!options.Contains(value, StringComparer.Ordinal)) continue;
                if (aliases.Any(a => v.Contains(Squash(a), StringComparison.Ordinal))) return value;
                if (v.Contains(Squash(value), StringComparison.Ordinal)) return value;
            }
        }

        return null;
    }

    private static bool LabelMatches(string fieldLabel, string tableKey)
        => fieldLabel.Contains(tableKey, StringComparison.OrdinalIgnoreCase)
           || tableKey.Contains(fieldLabel, StringComparison.OrdinalIgnoreCase);

    /// <summary>去空白 + 转小写，用于"口语 vs 枚举"的比较（不影响入库值）。</summary>
    private static string Squash(string? s)
        => new string((s ?? "").Where(c => !char.IsWhiteSpace(c) && c != '"' && c != '\'').ToArray()).ToLowerInvariant();

    /// <summary>
    /// 用"公司名 / 联系人 / 关键字"找客户（员工与甲方都不用记 C1004 这种编号）。
    /// 返回候选列表：命中 1 条即唯一，命中多条时由上层要求用户澄清，0 条表示找不到。
    /// </summary>
    public async Task<List<Dictionary<string, string>>> FindClientsByKeywordAsync(string keyword, int limit = 10)
    {
        var rows = await RowsAsync("customers").ConfigureAwait(false);
        var k = (keyword ?? "").Trim();
        if (k.Length == 0) return new List<Dictionary<string, string>>();

        var exact = rows.Where(r => Get(r, "id").Equals(k, StringComparison.OrdinalIgnoreCase)
                                    || Get(r, "name").Equals(k, StringComparison.OrdinalIgnoreCase)).ToList();
        if (exact.Count == 1) return exact;

        var hits = rows.Where(r =>
                Get(r, "name").Contains(k, StringComparison.OrdinalIgnoreCase) ||
                Get(r, "contact").Contains(k, StringComparison.OrdinalIgnoreCase) ||
                Get(r, "industry").Contains(k, StringComparison.OrdinalIgnoreCase) ||
                Get(r, "owner").Contains(k, StringComparison.OrdinalIgnoreCase) ||
                Get(r, "id").Contains(k, StringComparison.OrdinalIgnoreCase))
            .Take(Math.Clamp(limit, 1, 50))
            .ToList();

        return hits.Count > 0 ? hits : exact;
    }

    /// <summary>用"需求标题关键字"找需求单（避免让员工背 R4001 这种编号）。</summary>
    public async Task<List<Dictionary<string, string>>> FindRequirementsByKeywordAsync(string keyword, int limit = 10)
    {
        var rows = await RowsAsync("requirements").ConfigureAwait(false);
        var k = (keyword ?? "").Trim();
        if (k.Length == 0) return new List<Dictionary<string, string>>();

        var exact = rows.Where(r => Get(r, "id").Equals(k, StringComparison.OrdinalIgnoreCase)
                                    || Get(r, "title").Equals(k, StringComparison.OrdinalIgnoreCase)).ToList();
        if (exact.Count == 1) return exact;

        var hits = rows.Where(r =>
                Get(r, "title").Contains(k, StringComparison.OrdinalIgnoreCase) ||
                Get(r, "detail").Contains(k, StringComparison.OrdinalIgnoreCase) ||
                Get(r, "id").Contains(k, StringComparison.OrdinalIgnoreCase))
            .Take(Math.Clamp(limit, 1, 50))
            .ToList();

        return hits.Count > 0 ? hits : exact;
    }

    public static string DescribeRow(Dictionary<string, string> row)    {
        var sb = new StringBuilder();
        foreach (var kv in row) sb.Append(kv.Key).Append('=').Append(kv.Value).Append("  ");
        return sb.ToString().TrimEnd();
    }
}
