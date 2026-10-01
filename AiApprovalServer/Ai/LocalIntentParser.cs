using System.Text.RegularExpressions;
using AiApproval.Core;

namespace AiApproval.Ai;

/// <summary>
/// 本地意图解析（无 AI 密钥时的降级通道，也是"AI 不可用"时的可用性保障）。
///
/// 它只做**保守的关键词/正则**解析：不确定就返回 ask，绝不猜测参数。
/// 注意：本地解析的结果同样要过规则引擎与 AI 审核，安全等级与 AI 解析完全一致——
/// 降级只是"少了智能"，不是"少了防线"。
/// </summary>
public static class LocalIntentParser
{
    private static readonly Regex PathToken = new(
        @"[「『""']?(?<p>(?:\.[A-Za-z0-9_\-]{1,40})|(?:[A-Za-z0-9_\u4e00-\u9fa5\-./]{1,120}?\.(?:txt|md|json|csv|log|ini|xml|html|yml|yaml|env|conf|config|sql|sh|ps1|bat|js|ts|py|cs|java|properties|toml)))[」』""']?",
        RegexOptions.Compiled, TimeSpan.FromMilliseconds(80));

    private static readonly Regex RecordId = new(@"(?<id>[A-Za-z]\d{3,8})", RegexOptions.Compiled, TimeSpan.FromMilliseconds(80));

    private static readonly (string Dataset, string[] Keywords)[] DatasetMap =
    {
        ("customers", new[] { "客户", "customers", "档案" }),
        ("orders", new[] { "订单", "orders", "order" }),
        ("contracts", new[] { "合同", "contracts", "contract" }),
    };

    public static IntentPlan Parse(string message, string role)
    {
        var text = (message ?? "").Trim();
        var plan = new IntentPlan { Source = "local-fallback" };

        if (text.Length == 0)
            return Ask("请输入你的请求，例如：列出我的文件。");

        // 管理端只读分析（管理员视角）
        if (role == Roles.Admin)
        {
            var adminPlan = ParseAdmin(text);
            if (adminPlan is not null) return adminPlan;
        }

        // 统计类问句（"有多少客户/需求单"）
        var countPlan = ParseCount(text);
        if (countPlan is not null) return countPlan;

        // 只有关键词没有扩展名 → 先检索（"读一下日报"）
        var searchPlan = ParseFileSearch(text);
        if (searchPlan is not null) return searchPlan;

        if (Contains(text, "帮助", "能做什么", "怎么用", "指令"))
        {
            return new IntentPlan
            {
                Intent = "查看本地指令说明",
                Tool = "ask",
                Reply = HelpText(),
                Source = "local-fallback",
            };
        }

        if (Contains(text, "我的任务", "任务列表", "审批状态"))
            return Call("查看本人任务列表", "my_tasks");

        if (Contains(text, "我的权限", "我是谁", "我的角色", "有什么权限"))
            return Call("查看本人角色与权限", "my_profile");

        // 通知
        if (Contains(text, "通知", "提醒", "发消息", "发送消息"))
        {
            var to = Contains(text, "审批人", "管理员") ? "approver" : "self";
            var subject = ExtractField(text, "标题") ?? "来自执行平台的业务通知";
            var body = ExtractField(text, "内容") ?? text;
            return Call("发送一条通知", "send_notice", new()
            {
                ["to"] = to,
                ["subject"] = Clamp(subject, 80),
                ["body"] = Clamp(body, 1000),
            });
        }

        // 业务数据：修改 / 删除 / 查询
        foreach (var (dataset, keywords) in DatasetMap)
        {
            if (!Contains(text, keywords)) continue;

            if (Contains(text, "修改", "更新", "改成", "改为"))
            {
                var id = RecordId.Match(text).Groups["id"].Value;
                var (field, value) = ExtractAssignment(text);
                if (id.Length == 0 || field.Length == 0)
                    return Ask($"请写成：修改{keywords[0]} C1003 字段 改为 新值（字段名见数据集说明）");

                return Call($"修改{keywords[0]} {id} 的 {field}", "update_record", new()
                {
                    ["dataset"] = dataset,
                    ["id"] = id,
                    ["field"] = field,
                    ["value"] = value,
                    ["reason"] = Clamp(ExtractField(text, "原因") ?? "用户通过本地指令提出", 100),
                });
            }

            if (Contains(text, "删除", "移除", "清理"))
            {
                var id = RecordId.Match(text).Groups["id"].Value;
                if (id.Length == 0)
                    return Ask($"请写成：删除{keywords[0]} C1003 原因：xxx（删除业务数据必须写明原因）");

                return Call($"删除{keywords[0]} {id}", "delete_record", new()
                {
                    ["dataset"] = dataset,
                    ["id"] = id,
                    ["reason"] = Clamp(ExtractField(text, "原因") ?? "用户通过本地指令提出", 100),
                });
            }

            if (Contains(text, "查询", "查一下", "看看", "列出", "统计", "显示"))
            {
                var args = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
                {
                    ["dataset"] = dataset,
                    ["limit"] = "20",
                };

                var level = ExtractEnum(text);
                if (level is not null)
                {
                    args["field"] = dataset == "customers" ? "level" : "status";
                    args["op"] = "eq";
                    args["value"] = level;
                }

                return Call($"查询{keywords[0]}数据", "query_records", args);
            }
        }

        // 文件：删除
        // 注意：判断"意图动词"时必须限定在**路径之前**出现——否则文件名里带"删除"两个字
        // （例如「写入 待删除.txt 内容：…」）会被误判成删除意图。这是实测踩到的坑。
        if (VerbBeforePath(text, "删除", "移除", "清理") && PathToken.IsMatch(text))
        {
            var path = PathToken.Match(text).Groups["p"].Value;
            return Call($"删除文件 {path}", "delete_file", new()
            {
                ["scope"] = Contains(text, "共享") ? "share" : "private",
                ["path"] = path,
                ["reason"] = Clamp(ExtractField(text, "原因") ?? "用户通过本地指令提出", 100),
            });
        }

        // 文件：写入
        if (VerbBeforePath(text, "写入", "保存", "创建", "新建", "追加", "添加", "加上") && PathToken.IsMatch(text))
        {
            var path = PathToken.Match(text).Groups["p"].Value;
            var content = ExtractField(text, "内容");
            if (content is null)
                return Ask("写入文件请写成：写入 note.txt 内容：今天的工作记录");

            var mode = Contains(text, "追加") ? "append" : Contains(text, "覆盖") ? "overwrite" : "create";
            return Call($"写入文件 {path}", "write_file", new()
            {
                ["scope"] = Contains(text, "共享") ? "share" : "private",
                ["path"] = path,
                ["content"] = Clamp(content, 4000),
                ["mode"] = mode,
                ["reason"] = Clamp(ExtractField(text, "原因") ?? "用户通过本地指令提出", 100),
            });
        }

        // 文件：读取
        if (VerbBeforePath(text, "读取", "打开", "查看", "显示") && PathToken.IsMatch(text))
        {
            var path = PathToken.Match(text).Groups["p"].Value;
            return Call($"读取文件 {path}", "read_file", new()
            {
                ["scope"] = Contains(text, "共享") ? "share" : "private",
                ["path"] = path,
            });
        }

        // 文件：列表
        if (Contains(text, "列出", "文件列表", "有哪些文件", "目录", "看看文件"))
        {
            return Call("列出工作区文件", "list_files", new()
            {
                ["scope"] = Contains(text, "共享") ? "share" : "private",
            });
        }

        return Ask("（本地指令模式）我没能确定你的意图。" + HelpText());
    }

    private static IntentPlan? ParseAdmin(string text)
    {
        if (text.Contains("待审批", StringComparison.Ordinal) || text.Contains("待处理", StringComparison.Ordinal)
            || text.Contains("待确认", StringComparison.Ordinal) || text.Contains("待办", StringComparison.Ordinal))
            return Call("拉取待审批任务清单", "a_pending_tasks");

        // 有多少用户 / 账号名册
        if (Contains(text, "多少用户", "多少账号", "用户数", "账号数", "用户列表", "账号列表", "名册", "有哪些人", "多少个人"))
        {
            var role = Contains(text, "员工") ? "staff" : Contains(text, "管理员") ? "admin" : Contains(text, "甲方", "客户账号") ? "user" : "";
            var args = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase) { ["limit"] = "100" };
            if (role.Length > 0) args["role"] = role;
            return Call("统计系统账号数量与名册", "list_users", args);
        }

        var taskMatch = Regex.Match(text, @"(?<id>t_[0-9a-f]{6,32})", RegexOptions.IgnoreCase);
        if (taskMatch.Success)
            return Call($"读取任务 {taskMatch.Groups["id"].Value} 的上下文", "a_task_detail",
                new() { ["task_id"] = taskMatch.Groups["id"].Value });

        if (Contains(text, "审计", "日志", "记录"))
        {
            var keyword = ExtractField(text, "关键字") ?? "";
            return Call("检索审计日志", "a_audit_search", new() { ["keyword"] = keyword, ["limit"] = "30" });
        }

        return null;
    }

    /// <summary>统计类问句（"有多少客户/需求单/订单"，本地模式也能答）。</summary>
    private static IntentPlan? ParseCount(string text)
    {
        if (!Contains(text, "多少", "几条", "几笔", "几个", "数量", "统计")) return null;

        string dataset = "";
        if (Contains(text, "客户", "甲方公司")) dataset = "customers";
        else if (Contains(text, "需求")) dataset = "requirements";
        else if (Contains(text, "订单")) dataset = "orders";
        else if (Contains(text, "合同")) dataset = "contracts";

        var args = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (dataset.Length > 0) args["dataset"] = dataset;
        return Call(dataset.Length > 0 ? $"统计{dataset}条数" : "统计业务数据条数", "count_records", args);
    }

    /// <summary>只有关键词、没有扩展名的文件读取请求 → 先检索（"读一下日报"）。</summary>
    private static IntentPlan? ParseFileSearch(string text)
    {
        if (!Contains(text, "读取", "打开", "查看", "找", "搜", "看看")) return null;

        // 已经能识别成"带扩展名的具体文件"时，交给读取分支处理
        if (PathToken.IsMatch(text)) return null;

        var match = Regex.Match(text, @"(?:读取|打开|查看|找一下|查找|搜索|找|搜|看看)\s*[「『""']?(?<k>[\u4e00-\u9fa5A-Za-z0-9_\-]{2,20})",
            RegexOptions.Compiled, TimeSpan.FromMilliseconds(50));
        if (!match.Success) return null;

        var keyword = match.Groups["k"].Value.Trim();
        foreach (var noise in new[] { "一下", "文件", "我的", "这个", "那个", "所有", "全部" })
            keyword = keyword.Replace(noise, "", StringComparison.Ordinal);

        if (keyword.Length < 2) return null;

        return Call($"检索文件名或内容包含「{keyword}」的文件", "search_files",
            new() { ["keyword"] = keyword, ["content"] = keyword, ["limit"] = "30" });
    }

    private static IntentPlan Call(string intent, string tool, Dictionary<string, string>? args = null)
        => new()
        {
            Intent = intent,
            Tool = tool,
            Args = args ?? new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase),
            Reply = "",
            Source = "local-fallback",
        };

    private static IntentPlan Ask(string reply)
        => new() { Intent = "未识别到可执行意图", Tool = "ask", Reply = reply, Source = "local-fallback" };

    public static string HelpText() =>
        "可用本地指令（未配置 AI 密钥时的降级模式）："
        + "① 列出文件 / 列出共享区文件；"
        + "② 读取 note.txt；"
        + "③ 写入 note.txt 内容：xxx（覆盖用“覆盖”，追加用“追加”）；"
        + "④ 删除 note.txt 原因：xxx；"
        + "⑤ 查询客户 / 查询订单 / 查询合同（可加“等级=黄金”）；"
        + "⑥ 修改客户 C1003 等级 改为 铂金；"
        + "⑦ 删除客户 C1003 原因：重复录入；"
        + "⑧ 通知我 标题：xxx 内容：xxx / 通知审批人 …；"
        + "⑨ 我的任务 / 我的权限。";

    private static bool Contains(string text, params string[] keywords)
        => keywords.Any(k => text.Contains(k, StringComparison.OrdinalIgnoreCase));

    /// <summary>
    /// 意图动词必须出现在**路径之前**（或句子里根本没有路径）才算命中。
    /// 例：「写入 待删除.txt 内容：马上删掉」→ 动词"写入"在路径前 → 写入；
    ///     而「删除 待删除.txt」→ "删除"在索引 0 → 删除。
    /// 这样文件名/内容里出现的"删除""写入"字样就不会劫持意图。
    /// </summary>
    private static bool VerbBeforePath(string text, params string[] verbs)
    {
        var pathMatch = PathToken.Match(text);
        var pathIndex = pathMatch.Success ? pathMatch.Index : -1;

        foreach (var verb in verbs)
        {
            var index = text.IndexOf(verb, StringComparison.OrdinalIgnoreCase);
            if (index < 0) continue;
            if (pathIndex < 0 || index < pathIndex) return true;
        }

        return false;
    }

    private static string? ExtractField(string text, string fieldName)
    {
        var rx = new Regex($"{fieldName}\\s*[:：]\\s*(?<v>[^\\n]+?)(?=(\\s*(标题|内容|原因|字段|改为|改成)\\s*[:：])|$)",
            RegexOptions.Compiled, TimeSpan.FromMilliseconds(50));
        var m = rx.Match(text);
        if (!m.Success) return null;

        var value = m.Groups["v"].Value.Trim();
        return value.Length == 0 ? null : value;
    }

    private static (string Field, string Value) ExtractAssignment(string text)
    {
        var rx = new Regex(@"(?<f>[A-Za-z_\u4e00-\u9fa5]{1,20})[^\S\n]*(?:改为|改成|=|:|：)[^\S\n]*(?<v>[^\n，。;；]{1,60})",
            RegexOptions.Compiled, TimeSpan.FromMilliseconds(50));
        var m = rx.Match(text);
        if (!m.Success) return ("", "");

        var field = m.Groups["f"].Value.Trim();
        var value = m.Groups["v"].Value.Trim();

        // "把…的等级改为" 之类的口语，取最后一个字段名更稳
        if (field is "把" or "将" or "修改" or "更新") 
        {
            var tail = text[..m.Index];
            var last = Regex.Matches(tail, @"(?<f>[A-Za-z_\u4e00-\u9fa5]{2,20})").Cast<Match>().LastOrDefault();
            if (last is not null) field = last.Groups["f"].Value.Trim();
        }

        return (field, value);
    }

    private static string? ExtractEnum(string text)
    {
        foreach (var candidate in new[] { "普通", "白银", "黄金", "铂金", "待付款", "已付款", "已发货", "已完成", "已取消", "草拟", "待签", "生效中", "已归档" })
        {
            if (text.Contains(candidate, StringComparison.Ordinal)) return candidate;
        }
        return null;
    }

    private static string Clamp(string value, int max)
        => value.Length <= max ? value : value[..max];
}
