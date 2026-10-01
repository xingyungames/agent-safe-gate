using AiApproval.Core;
using AiApproval.Services;

namespace AiApproval.Tools;

public sealed class RuleEvaluation
{
    public RuleOutcome Outcome { get; init; } = new();
    /// <summary>人类可读的动作摘要（给审批人看，例如：覆盖文件 draft/客户名单.txt）</summary>
    public string ActionSummary { get; init; } = "";
}

/// <summary>
/// 规则引擎（硬防线）：100% 确定性的代码校验，不依赖任何模型判断。
///
/// 校验层次：
///   1) 工具是否存在于目录白名单；
///   2) 当前角色是否拥有该工具所需权限（最小权限）；
///   3) 参数白名单 + 必填 + 类型/格式/长度（未知参数一律拒绝）；
///   4) 业务级硬规则：路径白名单、系统保护路径、收件人白名单、数据集/字段白名单；
///   5) 风险升级：覆盖已有文件、共享区写入、疑似注入 → 强制人工审批（只升不降）。
///
/// 只要这一层拒绝，无论 AI 说什么都不会执行——这就是"不信任上游 AI 输出"的落点。
/// </summary>
public sealed class RuleEngine
{
    private static readonly string[] ProtectedPathMarkers =
    {
        ".env", ".ssh", ".git", "web.config", "appsettings", "secrets.json", "id_rsa", ".htaccess",
    };

    /// <summary>甲方可自行修改的档案字段（等级/状态/备注属于乙方运营口径，甲方无权改）</summary>
    private static readonly string[] ClientSelfFields = { "name", "contact", "phone", "industry", "requirement" };

    /// <summary>需求单可修改字段（编号、甲方编号、提交账号、创建时间都是只读）</summary>
    private static readonly string[] RequirementWritableFields =
    {
        "title", "detail", "budget", "expect_date", "status", "owner",
    };

    private readonly PathGuard _paths;
    private readonly DatasetStore _datasets;
    private readonly UserService _users;
    private readonly AppConfig _cfg;

    public RuleEngine(PathGuard paths, DatasetStore datasets, UserService users, AppConfig cfg)
    {
        _paths = paths;
        _datasets = datasets;
        _users = users;
        _cfg = cfg;
    }

    public async Task<RuleEvaluation> EvaluateAsync(string role, string userId, string toolName,
        Dictionary<string, string> rawArgs, bool injectionSuspected)
    {
        var outcome = new RuleOutcome();
        var args = Normalize(rawArgs);

        var tool = ToolCatalog.Get(toolName);
        if (tool is null)
            return Deny($"工具 {Sanitize(toolName)} 不在允许的目录中");

        if (tool.AdminOnly && role != Roles.Admin)
            return Deny("该工具仅管理员可用");

        var missing = tool.Permissions.Where(p => !ToolCatalog.HasPermission(role, p)).ToList();
        if (missing.Count > 0)
            return Deny($"当前角色（{Roles.Label(role)}）缺少权限：{string.Join("、", missing.Select(Permissions.Label))}");

        // 未知参数直接拒绝：防"走私参数"
        var unknown = args.Keys.Where(k => !tool.AllowedParamNames.Contains(k, StringComparer.OrdinalIgnoreCase)).ToList();
        if (unknown.Count > 0)
            return Deny($"包含未定义的参数：{string.Join("、", unknown.Select(Sanitize))}");

        foreach (var name in tool.RequiredParams)
        {
            if (!args.TryGetValue(name, out var value) || string.IsNullOrWhiteSpace(value))
                return Deny($"缺少必填参数：{name}");
        }

        outcome.RiskLevel = tool.RiskLevel;
        outcome.RequiresApproval = tool.RequiresApproval;

        var normalized = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var summary = "";

        switch (tool.Name)
        {
            case "list_files":
            case "read_file":
            {
                var check = await CheckFileAccessAsync(userId, role, args, requireShareWrite: false);
                if (check.Error is not null) return Deny(check.Error);

                normalized["scope"] = check.Scope;
                normalized["path"] = check.Relative;
                summary = tool.Name == "read_file"
                    ? $"读取文件：{Describe(check.Scope, check.Relative)}"
                    : $"列目录：{Describe(check.Scope, string.IsNullOrEmpty(check.Relative) ? "/" : check.Relative)}";

                if (tool.Name == "read_file" && check.IsDirectory)
                    return Deny("目标是一个目录，请使用 list_files");
                break;
            }

            case "write_file":
            {
                var check = await CheckFileAccessAsync(userId, role, args, requireShareWrite: true);
                if (check.Error is not null) return Deny(check.Error);

                var content = args.GetValueOrDefault("content") ?? "";
                if (content.Length > ToolCatalog.MaxFileContentChars)
                    return Deny($"写入内容过长（{content.Length} 字符，上限 {ToolCatalog.MaxFileContentChars}）");
                if (content.Any(c => char.IsControl(c) && c != '\n' && c != '\r' && c != '\t' && c != '\0'))
                    return Deny("内容包含不允许的控制字符");
                if (string.IsNullOrEmpty(check.Relative))
                    return Deny("必须指定文件名");

                // 真二进制（含 NUL 字节）：不是策略拒绝，而是**文本工具的能力边界** → 给出可执行的替代方案
                if (ContentRiskScanner.LooksLikeRealBinary(content))
                    return Deny("检测到二进制内容（含 NUL 字节），本工具只能写文本。"
                                + "如果确实要落地二进制：请改为写入 base64 文本文件（例如 app.bin.b64）并在说明里标注来源，"
                                + "或使用专门的文件上传通道。");

                var mode = (args.GetValueOrDefault("mode") ?? "create").Trim().ToLowerInvariant();
                if (mode is not ("create" or "overwrite" or "append"))
                    return Deny("mode 只能是 create / overwrite / append");

                var exists = File.Exists(check.FullPath);

                // 说"新建"但文件已存在：把选择权交回去（追加/覆盖/改名），而不是一刀切拒绝
                if (mode == "create" && exists)
                {
                    return Ask($"{check.Relative} 已经存在，请确认你要怎么做",
                        new List<string>
                        {
                            $"append：把内容追加到 {check.Relative}（保留原有内容）",
                            $"overwrite：覆盖 {check.Relative}（原内容会进快照，可回退）",
                            $"rename_file：改成别的文件名（例如加 -v2 后缀）",
                        });
                }

                // ---------- 风险分级：可执行性（后缀 ∪ 内容识别）+ 内容特征 + 区域 ----------
                var executable = ContentRiskScanner.DetectExecutable(check.Relative, content);
                var contentRisk = ContentRiskScanner.Scan(content);
                var risks = executable.Evidences.Concat(contentRisk.Hits).Distinct().ToList();
                var areaLabel = check.Scope == "share" ? "共享工作区" : "私人工作区";

                if (executable.IsExecutable)
                {
                    // 可执行/脚本（无论靠后缀还是靠内容识别出来的）→ 一律人工审批，绝不自动执行
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add($"判定为可执行/脚本内容（判定通道：{executable.Channel}）：" + executable.Summary);
                    outcome.Translations.Add("可执行性判定：" + executable.Summary);
                }

                if (contentRisk.HasRisk)
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add("内容风险扫描命中：" + contentRisk.Summary);
                    outcome.Translations.Add("片段风险扫描：" + contentRisk.Summary);
                }

                // 覆盖：私人工作区的普通文本允许（有快照可回退）；共享区与可执行/风险内容必须人工确认
                if (exists && mode == "overwrite" && (check.Scope == "share" || executable.IsExecutable || contentRisk.HasRisk))
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add("覆盖已有文件会丢失历史内容（已存快照可回退，但仍需人工确认）");
                }

                if (check.Scope == "share")
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add("写入共享工作区会影响其他成员，需要人工确认");
                }

                // ---------- 私人工作区的普通文本文件：增/改/覆写都允许免人工审批（快照 + 审计照样留） ----------
                var isPrivate = check.Scope == "private";
                var isPlainText = !executable.IsExecutable && !contentRisk.HasRisk;
                var smallEnough = content.Length <= 20000;
                if (isPrivate && isPlainText && !injectionSuspected && smallEnough)
                {
                    outcome.SandboxAutoAllowed = true;
                    outcome.Translations.Add($"私人工作区普通文本文件（{ModeLabel(mode)}）→ 允许免人工审批"
                        + (exists && mode == "overwrite" ? "，覆盖前已存快照可一键回退" : "")
                        + "（审计照常记录）");
                }

                normalized["scope"] = check.Scope;
                normalized["path"] = check.Relative;
                normalized["content"] = content;
                normalized["mode"] = mode;
                normalized["area"] = areaLabel;
                if (risks.Count > 0) normalized["risk_hits"] = string.Join("；", risks);

                summary = $"{ModeLabel(mode)}文件：{Describe(check.Scope, check.Relative)}（{content.Length} 字符，{areaLabel}）"
                          + (risks.Count > 0 ? $"【风险：{string.Join("；", risks)}】" : "");
                break;
            }

            case "rename_file":
            {
                var check = await CheckFileAccessAsync(userId, role, args, requireShareWrite: true);
                if (check.Error is not null) return Deny(check.Error);
                if (string.IsNullOrEmpty(check.Relative)) return Deny("必须指定要改名的文件");
                if (!File.Exists(check.FullPath)) return Deny($"文件不存在：{check.Relative}");

                var newName = (args.GetValueOrDefault("new_name") ?? "").Trim();
                if (newName.Length == 0 || newName.Length > 120) return Deny("新文件名长度需在 1-120 字符之间");
                if (newName.Contains('/') || newName.Contains('\\') || newName.Contains(".."))
                    return Deny("新文件名只能是文件名本身（不含目录、不含 ..）");
                if (newName.IndexOfAny(new[] { ':', '*', '?', '"', '<', '>', '|', '\0' }) >= 0)
                    return Deny("新文件名包含非法字符");

                var oldExt = Path.GetExtension(check.Relative).TrimStart('.').ToLowerInvariant();
                var newExt = Path.GetExtension(newName).TrimStart('.').ToLowerInvariant();

                // 改名（含改后缀）始终允许；但"变成可执行/脚本后缀"必须人工审批（后缀判断 ∪ 原内容识别）
                var newNameVerdict = ContentRiskScanner.DetectExecutable(newName, null);
                var oldContentVerdict = ContentRiskScanner.DetectExecutable(check.Relative, null);
                var extChanged = !string.Equals(oldExt, newExt, StringComparison.OrdinalIgnoreCase);

                if (newNameVerdict.IsExecutable || (!extChanged && oldContentVerdict.IsExecutable))
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add($"目标文件名判定为可执行/脚本：{newNameVerdict.Summary}（改名会改变文件的执行语义）");
                    outcome.Translations.Add("可执行性判定（新名）：" + newNameVerdict.Summary);
                }

                if (extChanged)
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add($"修改扩展名属于风险操作：{(oldExt.Length == 0 ? "（无后缀）" : "." + oldExt)} → {(newExt.Length == 0 ? "（无后缀）" : "." + newExt)}");
                }

                if (check.Scope == "share")
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add("共享工作区改名会影响其他成员");
                }

                // 私人工作区、后缀不变、名字无风险 → 允许免人工审批
                if (check.Scope == "private" && !extChanged && !newNameVerdict.IsExecutable && !injectionSuspected)
                {
                    outcome.SandboxAutoAllowed = true;
                    outcome.Translations.Add("私人工作区同后缀改名 → 允许免人工审批（仍留存审计）");
                }

                normalized["scope"] = check.Scope;
                normalized["path"] = check.Relative;
                normalized["new_name"] = newName;
                summary = $"重命名：{Describe(check.Scope, check.Relative)} → {newName}";
                break;
            }

            case "create_folder":
            {
                var check = await CheckFileAccessAsync(userId, role, args, requireShareWrite: true);
                if (check.Error is not null) return Deny(check.Error);
                if (string.IsNullOrEmpty(check.Relative)) return Deny("必须指定要新建的目录名");
                if (Directory.Exists(check.FullPath)) return Deny($"目录已存在：{check.Relative}");
                if (File.Exists(check.FullPath)) return Deny($"同名文件已存在：{check.Relative}");

                if (check.Scope == "share")
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add("在共享区新建目录会影响其他成员");
                }
                else
                {
                    outcome.SandboxAutoAllowed = !injectionSuspected;
                    outcome.Translations.Add("沙箱内低风险操作：个人工作区新建目录 → 允许免人工审批");
                }

                normalized["scope"] = check.Scope;
                normalized["path"] = check.Relative;
                summary = $"新建目录：{Describe(check.Scope, check.Relative)}";
                break;
            }

            case "get_file_info":
            {
                var check = await CheckFileAccessAsync(userId, role, args, requireShareWrite: false);
                if (check.Error is not null) return Deny(check.Error);
                if (string.IsNullOrEmpty(check.Relative)) return Deny("必须指定文件名或目录名");

                normalized["scope"] = check.Scope;
                normalized["path"] = check.Relative;
                summary = $"查看文件信息：{Describe(check.Scope, check.Relative)}";
                break;
            }

            case "my_workspace":
            {
                summary = "查看我的可用区域与权限";
                break;
            }

            case "my_notifications":
            {
                var limit = 10;
                if (args.TryGetValue("limit", out var limitRaw) && !string.IsNullOrWhiteSpace(limitRaw)
                    && int.TryParse(limitRaw, out var parsed)) limit = Math.Clamp(parsed, 1, 50);
                normalized["limit"] = limit.ToString();
                summary = "查看我的通知";
                break;
            }

            case "cancel_my_task":
            {
                var taskId = (args.GetValueOrDefault("task_id") ?? "").Trim();
                if (taskId.Length > 0 && (taskId.Length > 64 || !taskId.StartsWith("t_", StringComparison.OrdinalIgnoreCase)))
                    return Deny("任务号格式非法（形如 t_xxxxxxxx）");

                normalized["task_id"] = taskId;
                normalized["reason"] = (args.GetValueOrDefault("reason") ?? "").Trim();

                // 撤回只是取消排队，不产生任何副作用 → 免人工审批（提升体验，也避免"提交错了却撤不回来"）
                outcome.SandboxAutoAllowed = !injectionSuspected;
                outcome.Translations.Add("撤回申请无副作用（只取消排队，不执行任何动作）→ 允许免人工审批");

                summary = taskId.Length > 0 ? $"撤回我的申请：{taskId}" : "撤回我最新一条待审批申请";
                break;
            }

            case "delete_file":
            {
                var check = await CheckFileAccessAsync(userId, role, args, requireShareWrite: true);
                if (check.Error is not null) return Deny(check.Error);
                if (string.IsNullOrEmpty(check.Relative))
                    return Deny("必须指定要删除的文件。系统**不支持批量/整目录删除**：请一次指定一个文件，每条删除都会单独审批并留存快照。");
                if (Directory.Exists(check.FullPath) && !File.Exists(check.FullPath))
                    return Deny("目标是一个目录。系统**不支持批量/整目录删除**（避免一次误操作清空工作区）：请逐个文件删除，每次都会单独审批。");
                if (!File.Exists(check.FullPath)) return Deny("目标文件不存在");

                var verdict = ContentRiskScanner.DetectExecutable(check.Relative, null);

                if (check.Scope == "share")
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add("删除共享工作区的文件会影响其他成员");
                }

                if (verdict.IsExecutable)
                {
                    outcome.RequiresApproval = true;
                    outcome.RiskLevel = Risk.Max(outcome.RiskLevel, Risk.High);
                    outcome.Reasons.Add("目标是可执行/脚本文件（" + verdict.Summary + "），删除需要人工确认");
                    outcome.Translations.Add("可执行性判定：" + verdict.Summary);
                }

                // 私人工作区的普通文件：允许 AI 直接删除（删除前照例存快照，可一键回退）
                if (check.Scope == "private" && !verdict.IsExecutable && !injectionSuspected)
                {
                    outcome.SandboxAutoAllowed = true;
                    outcome.Translations.Add("私人工作区普通文件删除 → 允许免人工审批（删除前已存快照，可一键回退）");
                }

                normalized["scope"] = check.Scope;
                normalized["path"] = check.Relative;
                summary = $"删除文件：{Describe(check.Scope, check.Relative)}";
                break;
            }

            case "query_records":
            {
                var dataset = args.GetValueOrDefault("dataset") ?? "";
                if (!DatasetCatalog.IsKnown(dataset))
                    return Deny("dataset 只能是 customers / orders / contracts");

                var limit = 20;
                if (args.TryGetValue("limit", out var limitRaw) && !string.IsNullOrWhiteSpace(limitRaw))
                {
                    if (!int.TryParse(limitRaw, out limit) || limit < 1)
                        return Deny("limit 必须是正整数");
                    limit = Math.Min(limit, 50);
                }

                normalized["dataset"] = dataset.ToLowerInvariant();
                normalized["limit"] = limit.ToString();
                foreach (var key in new[] { "field", "op", "value" })
                    if (args.TryGetValue(key, out var v) && !string.IsNullOrWhiteSpace(v))
                        normalized[key] = v.Trim();

                if (normalized.ContainsKey("field"))
                {
                    var schema = DatasetCatalog.All[dataset];
                    if (schema.Field(normalized["field"]) is null)
                        return Deny($"数据集 {schema.Label} 不存在字段 {Sanitize(normalized["field"])}");
                }

                summary = $"查询数据：{dataset}" + (normalized.TryGetValue("field", out var f) ? $"（{f} {normalized.GetValueOrDefault("op", "eq")} {normalized.GetValueOrDefault("value", "")}）" : "");
                break;
            }

            case "update_record":
            {
                var dataset = args.GetValueOrDefault("dataset") ?? "";
                if (!DatasetCatalog.IsKnown(dataset)) return Deny("dataset 只能是 customers / orders / contracts");

                var schema = DatasetCatalog.All[dataset];
                var id = (args.GetValueOrDefault("id") ?? "").Trim();
                var field = (args.GetValueOrDefault("field") ?? "").Trim();
                var value = args.GetValueOrDefault("value") ?? "";

                if (id.Length is 0 or > 24) return Deny("记录主键格式非法");
                var valueError = _datasets.ValidateValue(schema, field, value, out var normalizedValue);
                if (valueError is not null) return Deny(valueError);

                var row = await _datasets.FindRowAsync(dataset, id).ConfigureAwait(false);
                if (row is null) return Deny($"记录不存在：{dataset}/{Sanitize(id)}");

                normalized["dataset"] = dataset.ToLowerInvariant();
                normalized["id"] = id;
                normalized["field"] = field;
                normalized["value"] = normalizedValue;
                summary = $"修改数据：{schema.Label} {id} 的 {field} 改为 {normalizedValue}";
                break;
            }

            case "delete_record":
            {
                var dataset = args.GetValueOrDefault("dataset") ?? "";
                if (!DatasetCatalog.IsKnown(dataset)) return Deny("dataset 只能是 customers / orders / contracts");

                var schema = DatasetCatalog.All[dataset];
                var id = (args.GetValueOrDefault("id") ?? "").Trim();
                var reason = (args.GetValueOrDefault("reason") ?? "").Trim();
                if (reason.Length < 2) return Deny("删除业务数据必须填写不少于 2 个字符的原因");

                var row = await _datasets.FindRowAsync(dataset, id).ConfigureAwait(false);
                if (row is null) return Deny($"记录不存在：{dataset}/{Sanitize(id)}");

                normalized["dataset"] = dataset.ToLowerInvariant();
                normalized["id"] = id;
                normalized["reason"] = reason;
                summary = $"删除数据：{schema.Label} 中的 {id}（原因：{reason}）";
                break;
            }

            case "send_notice":
            {
                var to = (args.GetValueOrDefault("to") ?? "").Trim().ToLowerInvariant();
                if (to is not ("self" or "approver"))
                    return Deny("收件人只允许 self（本人）或 approver（安全审批人）");

                var subject = (args.GetValueOrDefault("subject") ?? "").Trim();
                var body = args.GetValueOrDefault("body") ?? "";
                if (subject.Length is 0 or > 80) return Deny("标题长度需在 1-80 字符之间");
                if (body.Length is 0 or > 1000) return Deny("正文长度需在 1-1000 字符之间");

                if (to == "self")
                {
                    var user = await _users.FindByIdAsync(userId).ConfigureAwait(false);
                    var email = user is null ? "" : _users.DecryptEmail(user);
                    if (string.IsNullOrWhiteSpace(email))
                        return Deny("你的账号尚未登记邮箱，无法发送给自己（可由管理员在后台补充）");
                }
                else if (string.IsNullOrWhiteSpace(_cfg.Mail.ApproverAddress))
                {
                    return Deny("系统未配置安全审批人邮箱");
                }

                normalized["to"] = to;
                normalized["subject"] = subject;
                normalized["body"] = body;
                summary = $"发送通知给 {(to == "self" ? "本人" : "安全审批人")}：{subject}";
                break;
            }

            // ------------------------------------------------ 甲方 / 乙方 对接业务
            case "my_client":
            {
                var mine = await FindOwnClientAsync(userId).ConfigureAwait(false);
                if (mine is null)
                    return Deny("你的账号尚未建立甲方档案，请先提交建档申请（公司名称/联系人/联系电话）");

                normalized["client_id"] = Get(mine, "id");
                summary = $"查看我司档案：{Get(mine, "name")}（{Get(mine, "id")}）";
                break;
            }

            case "create_client_profile":
            {
                if (role != Roles.User)
                    return Deny("建档仅限甲方账号使用；员工请用“查询客户档案 / 修改客户档案”处理客户资料");

                var existing = await FindOwnClientAsync(userId).ConfigureAwait(false);
                if (existing is not null)
                    return Deny($"你的账号已绑定档案 {Get(existing, "id")}，如需变更请使用“修改客户档案”");

                var schema = DatasetCatalog.All["customers"];
                var profile = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                foreach (var fieldName in new[] { "name", "contact", "phone", "industry", "requirement" })
                {
                    var raw = (args.GetValueOrDefault(fieldName) ?? "").Trim();
                    if (raw.Length == 0)
                    {
                        if (fieldName == "industry") { profile[fieldName] = "其它"; continue; }
                        if (fieldName is "name" or "contact" or "phone") return Deny($"{schema.Field(fieldName)!.Label}不能为空");
                        profile[fieldName] = "";
                        continue;
                    }

                    var error = _datasets.ValidateValue(schema, fieldName, raw, out var normalizedValue);
                    if (error is not null) return Deny(error);
                    profile[fieldName] = normalizedValue;
                }

                foreach (var kv in profile) normalized[kv.Key] = kv.Value;
                summary = $"提交建档申请：{profile["name"]}（联系人 {profile["contact"]}，{profile["phone"]}）";
                break;
            }

            case "update_client_profile":
            {
                var field = (args.GetValueOrDefault("field") ?? "").Trim();
                var value = (args.GetValueOrDefault("value") ?? "").Trim();
                if (field.Length == 0 || value.Length == 0) return Deny("字段名与新值都不能为空");

                // 字段名也做一次"口语 → 字段名"的收敛：说"等级/名字/电话"也能用
                field = MapFieldName("customers", field);

                string clientId;
                if (role == Roles.User)
                {
                    var mine = await FindOwnClientAsync(userId).ConfigureAwait(false);
                    if (mine is null) return Deny("你的账号尚未建立甲方档案，请先提交建档申请");

                    var requested = (args.GetValueOrDefault("client_id") ?? args.GetValueOrDefault("client") ?? "").Trim();
                    if (requested.Length > 0 && !requested.Equals(Get(mine, "id"), StringComparison.OrdinalIgnoreCase)
                        && !Get(mine, "name").Equals(requested, StringComparison.OrdinalIgnoreCase))
                        return Deny("你只能修改本公司的档案，不能修改其他客户的档案");

                    clientId = Get(mine, "id");
                    if (!ClientSelfFields.Contains(field, StringComparer.OrdinalIgnoreCase))
                        return Deny($"甲方只能修改：公司名称、联系人、联系电话、所属行业、需求摘要（{Sanitize(field)} 属于乙方维护字段）");
                }
                else
                {
                    var resolved = await ResolveClientAsync(args.GetValueOrDefault("client_id"), args.GetValueOrDefault("client")).ConfigureAwait(false);
                    if (resolved.Candidates.Count > 0)
                        return Ask($"「{resolved.Keyword}」匹配到多个客户，请指明要改哪一个", DescribeClients(resolved.Candidates));
                    if (resolved.Error is not null) return Deny(resolved.Error);
                    if (resolved.Row is null) return Deny("请指明要修改的客户（传 client=公司名关键字即可，例如「示例物流」）");

                    clientId = Get(resolved.Row, "id");
                    if (resolved.Translation.Length > 0) outcome.Translations.Add(resolved.Translation);
                    if (field.Equals("owner", StringComparison.OrdinalIgnoreCase))
                        return Deny("负责人变更请使用“指派客户负责人”，以便留下独立的审批与审计记录");
                }

                var schema = DatasetCatalog.All["customers"];
                if (schema.Field(field) is null)
                    return Deny($"客户档案不存在字段：{Sanitize(field)}（可用字段：{string.Join(" / ", schema.Fields.Where(f => f.Writable).Select(f => f.Name))}）");

                var valueError = _datasets.ValidateValue(schema, field, value, out var normalizedValue);
                if (valueError is not null) return Deny(valueError);

                var target = await _datasets.FindRowAsync("customers", clientId).ConfigureAwait(false);
                if (target is null) return Deny($"客户档案不存在：{clientId}");

                var oldValue = Get(target, field);
                if (oldValue.Equals(normalizedValue, StringComparison.Ordinal))
                    return Deny($"{schema.Field(field)!.Label}的新值与当前值相同（{oldValue}），无需提交审批");

                if (!oldValue.Equals(value, StringComparison.Ordinal) && !normalizedValue.Equals(value, StringComparison.OrdinalIgnoreCase))
                    outcome.Translations.Add($"{schema.Field(field)!.Label}：「{value}」→「{normalizedValue}」");

                normalized["client_id"] = clientId;
                normalized["field"] = field;
                normalized["value"] = normalizedValue;
                normalized["old_value"] = oldValue;
                summary = $"修改客户档案 {clientId}（{Get(target, "name")}）的 {schema.Field(field)!.Label}：{oldValue} → {normalizedValue}";
                break;
            }

            case "assign_client_owner":
            {
                var owner = (args.GetValueOrDefault("owner") ?? "").Trim();
                if (owner.Length is 0 or > 32) return Deny("负责员工用户名长度需在 1-32 字符之间");

                var resolved = await ResolveClientAsync(args.GetValueOrDefault("client_id"), args.GetValueOrDefault("client")).ConfigureAwait(false);
                if (resolved.Candidates.Count > 0)
                    return Ask($"「{resolved.Keyword}」匹配到多个客户，请指明要给哪一个指派负责人", DescribeClients(resolved.Candidates));
                if (resolved.Error is not null) return Deny(resolved.Error);
                if (resolved.Row is null) return Deny("请指明客户（传 client=公司名关键字即可）");

                var clientId = Get(resolved.Row, "id");
                if (resolved.Translation.Length > 0) outcome.Translations.Add(resolved.Translation);

                var current = Get(resolved.Row, "owner");
                if (current.Equals(owner, StringComparison.OrdinalIgnoreCase))
                    return Deny($"该客户当前负责人已是 {owner}");

                normalized["client_id"] = clientId;
                normalized["owner"] = owner;
                normalized["old_value"] = current;
                summary = $"指派客户 {clientId}（{Get(resolved.Row, "name")}）的负责人：{(current.Length == 0 ? "（未指派）" : current)} → {owner}";
                break;
            }

            case "find_client":
            {
                var keyword = (args.GetValueOrDefault("keyword") ?? "").Trim();
                if (keyword.Length is 0 or > 40) return Deny("关键字长度需在 1-40 字符之间");

                var limit = 10;
                if (args.TryGetValue("limit", out var limitRaw) && !string.IsNullOrWhiteSpace(limitRaw)
                    && int.TryParse(limitRaw, out var parsed)) limit = Math.Clamp(parsed, 1, 50);

                normalized["keyword"] = keyword;
                normalized["limit"] = limit.ToString();
                summary = $"按关键字查找客户：{Sanitize(keyword)}";
                break;
            }

            case "get_client_detail":
            {
                var resolved = await ResolveClientAsync(args.GetValueOrDefault("client_id"), args.GetValueOrDefault("client")).ConfigureAwait(false);
                if (resolved.Candidates.Count > 0)
                    return Ask($"「{resolved.Keyword}」匹配到多个客户，请指明看哪一个", DescribeClients(resolved.Candidates));
                if (resolved.Error is not null) return Deny(resolved.Error);
                if (resolved.Row is null) return Deny("请指明客户（传 client=公司名关键字即可）");

                normalized["client_id"] = Get(resolved.Row, "id");
                if (resolved.Translation.Length > 0) outcome.Translations.Add(resolved.Translation);
                summary = $"查看客户详情：{Get(resolved.Row, "name")}（{Get(resolved.Row, "id")}）";
                break;
            }

            case "submit_requirement":
            {
                string customerId;
                if (role == Roles.User)
                {
                    var mine = await FindOwnClientAsync(userId).ConfigureAwait(false);
                    if (mine is null) return Deny("你的账号尚未建立甲方档案，请先提交建档申请，再提交需求");

                    var requested = (args.GetValueOrDefault("customer_id") ?? args.GetValueOrDefault("customer") ?? "").Trim();
                    if (requested.Length > 0 && !requested.Equals(Get(mine, "id"), StringComparison.OrdinalIgnoreCase)
                        && !Get(mine, "name").Equals(requested, StringComparison.OrdinalIgnoreCase))
                        return Deny("你只能为自己的公司提交需求单");

                    customerId = Get(mine, "id");
                }
                else
                {
                    var resolved = await ResolveClientAsync(args.GetValueOrDefault("customer_id"), args.GetValueOrDefault("customer")).ConfigureAwait(false);
                    if (resolved.Candidates.Count > 0)
                        return Ask($"「{resolved.Keyword}」匹配到多个客户，请指明给哪一家提交需求", DescribeClients(resolved.Candidates));
                    if (resolved.Error is not null) return Deny(resolved.Error);
                    if (resolved.Row is null) return Deny("请指明要代哪家客户提交需求（传 customer=公司名关键字即可）——或让客户自己在业务对接台提交");

                    customerId = Get(resolved.Row, "id");
                    if (resolved.Translation.Length > 0) outcome.Translations.Add(resolved.Translation);
                }

                var title = (args.GetValueOrDefault("title") ?? "").Trim();
                var detail = (args.GetValueOrDefault("detail") ?? "").Trim();
                if (title.Length == 0) return Deny("需求标题不能为空");
                if (detail.Length == 0) return Deny("需求描述不能为空");

                var schema = DatasetCatalog.All["requirements"];
                var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                foreach (var fieldName in new[] { "title", "detail", "budget", "expect_date" })
                {
                    var raw = args.GetValueOrDefault(fieldName) ?? "";
                    if (string.IsNullOrWhiteSpace(raw)) { if (fieldName is "title" or "detail") return Deny($"{schema.Field(fieldName)!.Label}不能为空"); values[fieldName] = ""; continue; }

                    var error = _datasets.ValidateValue(schema, fieldName, raw.Trim(), out var normalizedValue);
                    if (error is not null) return Deny(error);
                    values[fieldName] = normalizedValue;
                }

                normalized["customer_id"] = customerId;
                foreach (var kv in values) normalized[kv.Key] = kv.Value;
                summary = $"提交需求单：{values["title"]}（甲方 {customerId}"
                          + (values["budget"].Length > 0 ? $"，预算 {values["budget"]} 元" : "")
                          + (values["expect_date"].Length > 0 ? $"，期望交付 {values["expect_date"]}" : "") + "）";
                break;
            }

            case "my_requirements":
            {
                var mine = await FindOwnClientAsync(userId).ConfigureAwait(false);
                if (mine is null)
                    return Deny("你的账号尚未建立甲方档案，请先提交建档申请");

                normalized["client_id"] = Get(mine, "id");
                summary = $"查看我司需求单：{Get(mine, "name")}（{Get(mine, "id")}）";
                break;
            }

            case "update_requirement":
            {
                var field = MapFieldName("requirements", (args.GetValueOrDefault("field") ?? "").Trim());
                var value = (args.GetValueOrDefault("value") ?? "").Trim();
                if (field.Length == 0 || value.Length == 0) return Deny("字段名与新值都不能为空");

                if (!RequirementWritableFields.Contains(field, StringComparer.OrdinalIgnoreCase))
                    return Deny($"需求单可修改字段：{string.Join(" / ", RequirementWritableFields)}");

                var resolved = await ResolveRequirementAsync(args.GetValueOrDefault("id"), args.GetValueOrDefault("requirement")).ConfigureAwait(false);
                if (resolved.Candidates.Count > 0)
                    return Ask($"「{resolved.Keyword}」匹配到多个需求单，请指明要改哪一个", DescribeRequirements(resolved.Candidates));
                if (resolved.Error is not null) return Deny(resolved.Error);
                if (resolved.Row is null) return Deny("请指明需求单（传 requirement=标题关键字即可）");

                if (resolved.Translation.Length > 0) outcome.Translations.Add(resolved.Translation);
                var id = Get(resolved.Row, "id");

                var schema = DatasetCatalog.All["requirements"];
                var valueError = _datasets.ValidateValue(schema, field, value, out var normalizedValue);
                if (valueError is not null) return Deny(valueError);

                var oldValue = Get(resolved.Row, field);
                if (oldValue.Equals(normalizedValue, StringComparison.Ordinal))
                    return Deny($"{schema.Field(field)!.Label}的新值与当前值相同（{oldValue}），无需提交审批");

                if (!oldValue.Equals(value, StringComparison.Ordinal) && !normalizedValue.Equals(value, StringComparison.OrdinalIgnoreCase))
                    outcome.Translations.Add($"{schema.Field(field)!.Label}：「{value}」→「{normalizedValue}」");

                normalized["id"] = id;
                normalized["field"] = field;
                normalized["value"] = normalizedValue;
                normalized["old_value"] = oldValue;
                summary = $"维护需求单 {id}（{Get(resolved.Row, "title")}）的 {schema.Field(field)!.Label}：{oldValue} → {normalizedValue}";
                break;
            }

            // ------------------------------------------------ 检索与统计
            case "search_files":
            {
                var scope = (args.GetValueOrDefault("scope") ?? "private").Trim().ToLowerInvariant();
                if (scope is not ("private" or "share")) return Deny("scope 只能是 private 或 share");
                if (scope == "share" && !ToolCatalog.HasPermission(role, Permissions.ShareRead))
                    return Deny($"当前角色（{Roles.Label(role)}）无权检索共享文件区");

                var keyword = (args.GetValueOrDefault("keyword") ?? "").Trim();
                var content = (args.GetValueOrDefault("content") ?? "").Trim();
                var ext = (args.GetValueOrDefault("ext") ?? "").Trim().TrimStart('.');
                var path = (args.GetValueOrDefault("path") ?? "").Trim();

                if (keyword.Length > 60 || content.Length > 60) return Deny("关键字过长（≤60 字符）");
                if (ext.Length > 0 && !System.Text.RegularExpressions.Regex.IsMatch(ext, "^[A-Za-z0-9]{1,8}$"))
                    return Deny("扩展名只能是字母数字（例如 txt / md）");

                var resolved = _paths.Resolve(userId, scope, path);
                if (!resolved.Ok) return Deny(resolved.Error);

                var limit = 30;
                if (args.TryGetValue("limit", out var limitRaw) && !string.IsNullOrWhiteSpace(limitRaw)
                    && int.TryParse(limitRaw, out var parsed)) limit = Math.Clamp(parsed, 1, 100);

                normalized["scope"] = scope;
                normalized["path"] = resolved.Relative;
                if (keyword.Length > 0) normalized["keyword"] = keyword;
                if (content.Length > 0) normalized["content"] = content;
                if (ext.Length > 0) normalized["ext"] = ext.ToLowerInvariant();
                normalized["limit"] = limit.ToString();

                summary = "检索文件：" + (keyword.Length > 0 ? $"文件名含「{Sanitize(keyword)}」" : "全部文件")
                          + (content.Length > 0 ? $"、内容含「{Sanitize(content)}」" : "")
                          + (ext.Length > 0 ? $"、扩展名 {ext}" : "")
                          + $"（{(scope == "share" ? "共享区" : "个人区")}/{resolved.Relative}）";
                break;
            }

            case "read_files":
            {
                var scope = (args.GetValueOrDefault("scope") ?? "private").Trim().ToLowerInvariant();
                if (scope is not ("private" or "share")) return Deny("scope 只能是 private 或 share");
                if (scope == "share" && !ToolCatalog.HasPermission(role, Permissions.ShareRead))
                    return Deny($"当前角色（{Roles.Label(role)}）无权读取共享文件区");

                var raw = (args.GetValueOrDefault("paths") ?? "").Trim();
                if (raw.Length == 0) return Deny("请提供要读取的文件路径列表（最多 5 个，逗号分隔）");

                var parts = raw.Split(new[] { ',', '，', ';', '；', '、', '\n', ' ' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                if (parts.Length == 0) return Deny("请提供要读取的文件路径列表");
                if (parts.Length > 5) return Deny($"一次最多读取 5 个文件（收到 {parts.Length} 个）");

                var canonicalPaths = new List<string>();
                foreach (var part in parts)
                {
                    var resolved = _paths.Resolve(userId, scope, part);
                    if (!resolved.Ok) return Deny(resolved.Error);
                    if (string.IsNullOrEmpty(resolved.Relative)) return Deny("必须指定文件名");
                    if (!File.Exists(resolved.FullPath)) return Deny($"文件不存在：{resolved.Relative}（可先用 search_files 找到正确路径）");
                    canonicalPaths.Add(resolved.Relative);
                }

                normalized["scope"] = scope;
                normalized["paths"] = string.Join(",", canonicalPaths);
                summary = $"批量读取 {canonicalPaths.Count} 个文件：{string.Join("、", canonicalPaths)}";
                break;
            }

            case "count_records":
            {
                var dataset = (args.GetValueOrDefault("dataset") ?? "").Trim();
                if (dataset.Length > 0 && !DatasetCatalog.IsKnown(dataset))
                    return Deny("dataset 只能是 customers / requirements / orders / contracts（或留空统计全部）");

                // 数据范围按角色收敛：甲方只统计本公司，员工/管理员统计全量
                normalized["dataset"] = dataset.ToLowerInvariant();
                normalized["scope"] = role == Roles.User ? "mine" : "all";

                if (role == Roles.User)
                {
                    var mine = await FindOwnClientAsync(userId).ConfigureAwait(false);
                    normalized["client_id"] = mine is null ? "" : Get(mine, "id");
                }

                summary = "统计业务数据条数" + (dataset.Length > 0 ? $"（{dataset}）" : "（全部）")
                          + (role == Roles.User ? "，范围：本公司" : "，范围：全部客户");
                break;
            }

            case "list_users":
            {
                var keyword = (args.GetValueOrDefault("keyword") ?? "").Trim();
                if (keyword.Length > 40) return Deny("关键字过长（≤40 字符）");

                var userRole = (args.GetValueOrDefault("role") ?? "").Trim().ToLowerInvariant();
                if (userRole.Length > 0 && !Roles.IsValid(userRole))
                    return Deny("role 只能是 user / staff / admin（或留空）");

                var limit = 50;
                if (args.TryGetValue("limit", out var limitRaw) && !string.IsNullOrWhiteSpace(limitRaw)
                    && int.TryParse(limitRaw, out var parsed)) limit = Math.Clamp(parsed, 1, 100);

                normalized["keyword"] = keyword;
                normalized["role"] = userRole;
                normalized["limit"] = limit.ToString();
                summary = $"查询账号名册（{Roles.Label(role)}）"
                          + (keyword.Length > 0 ? $"：关键字「{Sanitize(keyword)}」" : "")
                          + (userRole.Length > 0 ? $"，角色 {Roles.Label(userRole)}" : "");
                break;
            }

            case "my_tasks":
                summary = "查看本人任务列表";
                break;

            case "search_clients":
            {
                var limit = 20;
                if (args.TryGetValue("limit", out var limitRaw) && !string.IsNullOrWhiteSpace(limitRaw))
                {
                    if (!int.TryParse(limitRaw, out limit) || limit < 1) return Deny("limit 必须是正整数");
                    limit = Math.Min(limit, 50);
                }

                var schema = DatasetCatalog.All["customers"];
                if (args.TryGetValue("field", out var fieldRaw) && !string.IsNullOrWhiteSpace(fieldRaw))
                {
                    var field = MapFieldName("customers", fieldRaw.Trim());
                    if (schema.Field(field) is null) return Deny($"客户档案不存在字段：{Sanitize(fieldRaw)}");
                    normalized["field"] = field;

                    foreach (var key in new[] { "op", "value" })
                        if (args.TryGetValue(key, out var v) && !string.IsNullOrWhiteSpace(v)) normalized[key] = v.Trim();

                    // 过滤值也做口语映射（"查重要的客户"→ level=铂金）
                    if (normalized.TryGetValue("value", out var filterValue) &&
                        schema.Field(field) is { Type: "enum", EnumValues: { } options })
                    {
                        var mapped = options.FirstOrDefault(o => o.Equals(filterValue, StringComparison.OrdinalIgnoreCase))
                                     ?? DatasetStore.MapEnumValue(schema.Field(field)!.Label, filterValue, options);
                        if (mapped is not null && !mapped.Equals(filterValue, StringComparison.Ordinal))
                        {
                            normalized["value"] = mapped;
                            outcome.Translations.Add($"{schema.Field(field)!.Label}筛选：「{filterValue}」→「{mapped}」");
                        }
                    }
                }

                normalized["limit"] = limit.ToString();
                summary = "查询客户档案" + (normalized.TryGetValue("field", out var f)
                    ? $"（{f} {normalized.GetValueOrDefault("op", "eq")} {normalized.GetValueOrDefault("value", "")}）"
                    : "（全部）");
                break;
            }

            case "my_profile":
                summary = "查看本人权限";
                break;

            case "a_pending_tasks":
                summary = "（管理端）拉取待审批任务清单";
                break;

            case "a_task_detail":
            {
                var taskId = (args.GetValueOrDefault("task_id") ?? "").Trim();
                if (taskId.Length is 0 or > 64) return Deny("task_id 格式非法");
                normalized["task_id"] = taskId;
                summary = $"（管理端）读取任务 {taskId} 的完整上下文";
                break;
            }

            case "a_audit_search":
            {
                var keyword = (args.GetValueOrDefault("keyword") ?? "").Trim();
                if (keyword.Length > 60) return Deny("关键字过长");
                var limit = 30;
                if (args.TryGetValue("limit", out var l) && !string.IsNullOrWhiteSpace(l) && int.TryParse(l, out var parsed))
                    limit = Math.Clamp(parsed, 1, 100);
                normalized["keyword"] = keyword;
                normalized["limit"] = limit.ToString();
                summary = $"（管理端）审计检索：{Sanitize(keyword)}";
                break;
            }

            case "a_activity_summary":
            {
                var user = (args.GetValueOrDefault("user") ?? "").Trim();
                if (user.Length > 32) return Deny("用户名过长");
                var days = 7;
                if (args.TryGetValue("days", out var d) && !string.IsNullOrWhiteSpace(d) && int.TryParse(d, out var parsedDays))
                    days = Math.Clamp(parsedDays, 1, 90);
                var limit = 20;
                if (args.TryGetValue("limit", out var l2) && !string.IsNullOrWhiteSpace(l2) && int.TryParse(l2, out var parsedLimit))
                    limit = Math.Clamp(parsedLimit, 1, 50);

                normalized["user"] = user;
                normalized["days"] = days.ToString();
                normalized["limit"] = limit.ToString();
                summary = $"（管理端）使用情况汇总：最近 {days} 天" + (user.Length > 0 ? $"，账号 {Sanitize(user)}" : "，全部账号");
                break;
            }

            case "a_task_list":
            {
                var status = (args.GetValueOrDefault("status") ?? "").Trim();
                if (status.Length > 0 && status is not ("PENDING_APPROVAL" or "EXECUTING" or "EXECUTED"
                    or "REJECTED_RULE" or "REJECTED_AI" or "REJECTED_ADMIN" or "EXPIRED" or "FAILED"))
                    return Deny("status 取值非法（可用：PENDING_APPROVAL/EXECUTING/EXECUTED/REJECTED_RULE/REJECTED_AI/REJECTED_ADMIN/EXPIRED/FAILED）");

                var user = (args.GetValueOrDefault("user") ?? "").Trim();
                var filterTool = (args.GetValueOrDefault("tool") ?? "").Trim();
                if (user.Length > 32) return Deny("用户名过长");
                if (filterTool.Length > 40) return Deny("工具名过长");

                var limit = 20;
                if (args.TryGetValue("limit", out var l3) && !string.IsNullOrWhiteSpace(l3) && int.TryParse(l3, out var parsedLimit2))
                    limit = Math.Clamp(parsedLimit2, 1, 100);

                normalized["status"] = status;
                normalized["user"] = user;
                normalized["tool"] = filterTool;
                normalized["limit"] = limit.ToString();
                summary = $"（管理端）任务清单：{(status.Length > 0 ? status : "全部状态")}"
                          + (user.Length > 0 ? $"，提交人 {Sanitize(user)}" : "")
                          + (filterTool.Length > 0 ? $"，工具 {Sanitize(filterTool)}" : "");
                break;
            }

            default:
                return Deny("该工具尚未接入规则引擎");
        }

        // 疑似提示词注入：不直接拒绝（避免误杀正常讨论），但强制转人工，绝不允许自动执行
        if (injectionSuspected && tool.StateChanging)
        {
            outcome.RequiresApproval = true;
            outcome.Reasons.Add("输入包含疑似提示词注入/越权诱导特征，需人工确认");
        }

        var canonical = new SortedDictionary<string, string>(StringComparer.Ordinal);
        foreach (var kv in normalized) canonical[kv.Key] = kv.Value;
        outcome.NormalizedArgs = normalized;
        outcome.CanonicalAction = AppJson.Serialize(new { tool = tool.Name, args = canonical });

        if (tool.RequiresApproval && outcome.Reasons.Count == 0)
            outcome.Reasons.Add("该操作按策略必须经人工审批");

        return new RuleEvaluation { Outcome = outcome, ActionSummary = summary };
    }

    private Task<(string? Error, string FullPath, string Relative, string Scope, bool IsDirectory)> CheckFileAccessAsync(
        string userId, string role, Dictionary<string, string> args, bool requireShareWrite)
    {
        var scope = (args.GetValueOrDefault("scope") ?? "private").Trim().ToLowerInvariant();
        if (scope is not ("private" or "share"))
            return Task.FromResult<(string?, string, string, string, bool)>(("scope 只能是 private 或 share", "", "", "", false));

        if (scope == "share")
        {
            var needed = requireShareWrite ? Permissions.ShareWrite : Permissions.ShareRead;
            if (!ToolCatalog.HasPermission(role, needed))
                return Task.FromResult<(string?, string, string, string, bool)>(($"当前角色（{Roles.Label(role)}）无权访问共享文件区", "", "", "", false));
        }

        var resolved = _paths.Resolve(userId, scope, args.GetValueOrDefault("path"));
        if (!resolved.Ok)
            return Task.FromResult<(string?, string, string, string, bool)>((resolved.Error, "", "", scope, false));

        foreach (var marker in ProtectedPathMarkers)
        {
            if (resolved.Relative.Contains(marker, StringComparison.OrdinalIgnoreCase))
                return Task.FromResult<(string?, string, string, string, bool)>(($"路径命中系统保护关键字（{marker}），已拒绝", "", "", scope, false));
        }

        var isDir = Directory.Exists(resolved.FullPath);
        return Task.FromResult<(string?, string, string, string, bool)>((null, resolved.FullPath, resolved.Relative, scope, isDir));
    }

    private static RuleEvaluation Deny(string reason) => new()
    {
        Outcome = new RuleOutcome
        {
            Decision = "DENY",
            RiskLevel = Risk.High,
            Reasons = { reason },
        },
        ActionSummary = "已被规则引擎拒绝",
    };

    private static Dictionary<string, string> Normalize(Dictionary<string, string>? raw)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (raw is null) return result;

        foreach (var kv in raw)
        {
            if (string.IsNullOrWhiteSpace(kv.Key)) continue;
            var key = kv.Key.Trim();
            if (key.Length > 40) continue;
            result[key] = (kv.Value ?? "").Trim();
        }
        return result;
    }

    private static string Describe(string scope, string relative)
        => (scope == "share" ? "共享区/" : "个人区/") + (string.IsNullOrEmpty(relative) ? "" : relative);

    private static string ModeLabel(string mode) => mode switch
    {
        "create" => "新建",
        "overwrite" => "覆盖",
        "append" => "追加",
        _ => "写入",
    };

    /// <summary>日志与错误信息里只保留可打印字符，避免把控制字符写进审计文件。</summary>
    private static string Sanitize(string? value)
    {
        if (string.IsNullOrEmpty(value)) return "";
        var cleaned = new string(value.Where(c => !char.IsControl(c)).ToArray());
        return cleaned.Length <= 60 ? cleaned : cleaned[..60] + "...";
    }

    /// <summary>找出与当前账号绑定的甲方档案（account_user_id 由服务端维护，客户端改不了）。</summary>
    private async Task<Dictionary<string, string>?> FindOwnClientAsync(string userId)
        => await _datasets.FindByFieldAsync("customers", "account_user_id", userId).ConfigureAwait(false);

    /// <summary>"需要澄清"：不落任务、不执行，把候选交回上游（AI 可据此再问用户或换一个更精确的关键字）。</summary>
    private static RuleEvaluation Ask(string question, List<string> candidates) => new()
    {
        Outcome = new RuleOutcome
        {
            Decision = "ASK",
            RiskLevel = Risk.Low,
            Reasons = { question },
            Translations = candidates,
        },
        ActionSummary = "信息不足，需要澄清：" + question,
    };

    private sealed class ResolveResult
    {
        public Dictionary<string, string>? Row { get; init; }
        public string? Error { get; init; }
        public List<Dictionary<string, string>> Candidates { get; init; } = new();
        public string Translation { get; init; } = "";
        public string Keyword { get; init; } = "";
    }

    /// <summary>
    /// 定位客户：优先用编号；否则用**公司名/联系人关键字**模糊匹配。
    /// 命中多条不报错，而是回"候选"，让 AI（或用户）确认——这就是"不用记编号"的落点。
    /// </summary>
    private async Task<ResolveResult> ResolveClientAsync(string? clientId, string? keyword)
    {
        if (!string.IsNullOrWhiteSpace(clientId))
        {
            var row = await _datasets.FindRowAsync("customers", clientId.Trim()).ConfigureAwait(false);
            return row is null
                ? new ResolveResult { Error = $"客户档案不存在：{Sanitize(clientId)}" }
                : new ResolveResult { Row = row };
        }

        var k = (keyword ?? "").Trim();
        if (k.Length == 0) return new ResolveResult();

        var hits = await _datasets.FindClientsByKeywordAsync(k).ConfigureAwait(false);
        if (hits.Count == 0) return new ResolveResult { Error = $"没有找到公司名/联系人包含「{Sanitize(k)}」的客户", Keyword = k };
        if (hits.Count > 1) return new ResolveResult { Candidates = hits, Keyword = k };

        return new ResolveResult
        {
            Row = hits[0],
            Keyword = k,
            Translation = $"名称「{k}」→ 客户 {Get(hits[0], "id")}（{Get(hits[0], "name")}）",
        };
    }

    /// <summary>定位需求单：优先编号，否则用标题关键字模糊匹配（同样支持"多候选 → 澄清"）。</summary>
    private async Task<ResolveResult> ResolveRequirementAsync(string? id, string? keyword)
    {
        if (!string.IsNullOrWhiteSpace(id))
        {
            var row = await _datasets.FindRowAsync("requirements", id.Trim()).ConfigureAwait(false);
            return row is null
                ? new ResolveResult { Error = $"需求单不存在：{Sanitize(id)}" }
                : new ResolveResult { Row = row };
        }

        var k = (keyword ?? "").Trim();
        if (k.Length == 0) return new ResolveResult { Error = "请提供需求标题关键字（例如「设备数据看板」）或需求编号" };

        var hits = await _datasets.FindRequirementsByKeywordAsync(k).ConfigureAwait(false);
        if (hits.Count == 0) return new ResolveResult { Error = $"没有找到标题/描述包含「{Sanitize(k)}」的需求单", Keyword = k };
        if (hits.Count > 1) return new ResolveResult { Candidates = hits, Keyword = k };

        return new ResolveResult
        {
            Row = hits[0],
            Keyword = k,
            Translation = $"标题「{k}」→ 需求单 {Get(hits[0], "id")}（{Get(hits[0], "title")}）",
        };
    }

    /// <summary>把客户候选渲染成给 AI/用户看的短列表（形如 C1004 示例物流（潜在））。</summary>
    private static List<string> DescribeClients(IEnumerable<Dictionary<string, string>> rows)
        => rows.Select(r => $"{Get(r, "id")} {Get(r, "name")}（{Get(r, "status")}，负责人 {(Get(r, "owner").Length == 0 ? "未指派" : Get(r, "owner"))}）").ToList();

    private static List<string> DescribeRequirements(IEnumerable<Dictionary<string, string>> rows)
        => rows.Select(r => $"{Get(r, "id")} {Get(r, "title")}（{Get(r, "status")}，甲方 {Get(r, "customer_id")}）").ToList();

    /// <summary>
    /// 字段名口语收敛：用户/AI 说"等级""电话""负责人""预算"也能落到 Schema 的英文字段名上。
    /// 命中不了就原样返回（由后续白名单校验给出"可用字段"错误）。
    /// </summary>
    private static string MapFieldName(string dataset, string field)
    {
        if (!DatasetCatalog.IsKnown(dataset)) return field;

        var schema = DatasetCatalog.All[dataset];
        var name = (field ?? "").Trim();
        if (name.Length == 0) return name;

        var direct = schema.Fields.FirstOrDefault(f => f.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
        if (direct is not null) return direct.Name;

        var aliases = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
        {
            ["name"] = new[] { "公司名称", "公司名", "客户名称", "客户名", "名字", "名称" },
            ["contact"] = new[] { "联系人", "对接人", "联系对象" },
            ["phone"] = new[] { "电话", "联系电话", "手机", "手机号" },
            ["industry"] = new[] { "行业", "所属行业", "领域" },
            ["level"] = new[] { "等级", "客户等级", "级别", "重要性" },
            ["status"] = new[] { "状态", "合作状态", "需求状态", "进展" },
            ["owner"] = new[] { "负责人", "负责员工", "跟进人", "归属" },
            ["requirement"] = new[] { "需求", "需求摘要", "诉求" },
            ["remark"] = new[] { "备注", "说明", "附注" },
            ["title"] = new[] { "标题", "需求标题" },
            ["detail"] = new[] { "描述", "需求描述", "详情", "内容" },
            ["budget"] = new[] { "预算", "金额", "报价" },
            ["expect_date"] = new[] { "期望交付日", "交付日期", "期望日期", "交付时间" },
        };

        foreach (var kv in aliases)
        {
            if (!schema.Fields.Any(f => f.Name.Equals(kv.Key, StringComparison.OrdinalIgnoreCase) && f.Writable)) continue;
            if (kv.Value.Any(a => a.Equals(name, StringComparison.OrdinalIgnoreCase))) return kv.Key;
        }

        foreach (var kv in aliases)
        {
            if (!schema.Fields.Any(f => f.Name.Equals(kv.Key, StringComparison.OrdinalIgnoreCase) && f.Writable)) continue;
            if (kv.Value.Any(a => name.Contains(a, StringComparison.OrdinalIgnoreCase))) return kv.Key;
        }

        return name;
    }

    private static string Get(Dictionary<string, string> row, string field)
    {
        var kv = row.FirstOrDefault(x => string.Equals(x.Key, field, StringComparison.OrdinalIgnoreCase));
        return kv.Key is null ? "" : kv.Value;
    }
}
