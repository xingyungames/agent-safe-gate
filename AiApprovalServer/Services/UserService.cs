using System.Text.RegularExpressions;
using AiApproval.Core;
using AiApproval.Security;

namespace AiApproval.Services;

public sealed class LoginResult
{
    public bool Success { get; init; }
    public UserRecord? User { get; init; }
    public string Error { get; init; } = "";
    public bool Locked { get; init; }
    public DateTime? LockedUntil { get; init; }
    public bool AccountNotFound { get; init; }
}

//单账号同步处置
public sealed record SyncItem(
    string UserName,
    string DisplayName,
    string EmployeeId,
    string Role,
    string Action,
    string Detail,
    bool NeedsPasswordReset = false);

//同步结果汇总
public sealed record SyncReport(
    bool Ok,
    bool DryRun,
    string Message,
    int Read,
    int Created,
    int Adopted,
    int Exists,
    int Skipped,
    int NeedsPasswordReset,
    List<SyncItem> Items);

//用户服务：账号增删改查与登录
public sealed class UserService
{
    private static readonly Regex UserNameRx = new("^[a-zA-Z0-9_.-]{3,32}$", RegexOptions.Compiled);
    private static readonly Regex EmailRx = new("^[^@\\s]{1,64}@[^@\\s]{1,128}\\.[a-zA-Z]{2,12}$", RegexOptions.Compiled);
    private static readonly string[] CommonPasswords =
    {
        "password", "12345678", "qwerty", "admin123", "admin888", "letmein", "iloveyou",
        "passw0rd", "123456789", "1qaz2wsx", "abc123456", "admin@123",
    };

    private readonly JsonStore _store;
    private readonly AppConfig _cfg;
    private readonly AuditLog _audit;
    private readonly FeishuMirror _mirror;
    private readonly byte[] _emailKey;
    private readonly byte[] _passwordPepper;

    public UserService(JsonStore store, AppConfig cfg, AuditLog audit, FeishuMirror mirror)
    {
        _store = store;
        _cfg = cfg;
        _audit = audit;
        _mirror = mirror;
        _emailKey = cfg.DeriveKey("email");
        _passwordPepper = cfg.DeriveKey("password-pepper");
    }

    //查询全部账号
    public Task<List<UserRecord>> AllAsync() => _store.ReadListAsync<UserRecord>(StoreNames.Users);

    //当前超管数量
    public async Task<int> CountActiveSuperAdminsAsync()
    {
        var all = await AllAsync().ConfigureAwait(false);
        return all.Count(u => u.Role == Roles.SuperAdmin && !u.Disabled);
    }

    //按名字查账号
    public async Task<UserRecord?> FindByNameAsync(string userName)
    {
        if (string.IsNullOrWhiteSpace(userName)) return null;
        var name = userName.Trim();
        var all = await _store.ReadListAsync<UserRecord>(StoreNames.Users).ConfigureAwait(false);
        return all.FirstOrDefault(u => string.Equals(u.UserName, name, StringComparison.OrdinalIgnoreCase));
    }

    //按 id 查账号
    public async Task<UserRecord?> FindByIdAsync(string id)
    {
        if (string.IsNullOrWhiteSpace(id)) return null;
        var all = await _store.ReadListAsync<UserRecord>(StoreNames.Users).ConfigureAwait(false);
        return all.FirstOrDefault(u => string.Equals(u.Id, id, StringComparison.Ordinal));
    }

    //校验用户名
    public static string? ValidateUserName(string? userName)
    {
        var name = (userName ?? "").Trim();
        if (!UserNameRx.IsMatch(name)) return "用户名必须为 3-32 位，只允许字母、数字、下划线、点、短横线";
        return null;
    }

    //校验口令强度
    public static string? ValidatePassword(string? password, string userName)
    {
        var pwd = password ?? "";
        if (pwd.Length < 10) return "口令长度至少 10 位";
        if (pwd.Length > 128) return "口令长度不得超过 128 位";

        var classes = 0;
        if (pwd.Any(char.IsLower)) classes++;
        if (pwd.Any(char.IsUpper)) classes++;
        if (pwd.Any(char.IsDigit)) classes++;
        if (pwd.Any(c => !char.IsLetterOrDigit(c))) classes++;
        if (classes < 3) return "口令需至少包含大写字母、小写字母、数字、符号中的三类";

        var lower = pwd.ToLowerInvariant();
        if (CommonPasswords.Any(c => lower.Contains(c))) return "口令包含常见弱口令片段";
        if (!string.IsNullOrEmpty(userName) && lower.Contains(userName.ToLowerInvariant())) return "口令不得包含用户名";
        if (lower.Contains("admin") && !string.Equals(userName, "admin", StringComparison.OrdinalIgnoreCase))
            return "口令不得包含 admin";

        return null;
    }

    //新建账号
    public async Task<(UserRecord? User, string Error)> CreateAsync(
        string userName, string password, string displayName, string role, string email, string createdBy)
    {
        var nameError = ValidateUserName(userName);
        if (nameError is not null) return (null, nameError);

        if (!Roles.IsValid(role)) return (null, "角色非法");

        var pwdError = ValidatePassword(password, userName.Trim());
        if (pwdError is not null) return (null, pwdError);

        if (!string.IsNullOrWhiteSpace(email) && !EmailRx.IsMatch(email.Trim()))
            return (null, "邮箱格式非法");

        var created = await _store.MutateAsync<UserRecord, UserRecord?>(StoreNames.Users, list =>
        {
            if (list.Any(u => string.Equals(u.UserName, userName.Trim(), StringComparison.OrdinalIgnoreCase)))
                return null;

            var (hash, salt, iterations, scheme) = Crypto.HashPassword(password, _passwordPepper, _cfg.PasswordIterations);
            var user = new UserRecord
            {
                Id = "u_" + Crypto.RandomHex(6),
                UserName = userName.Trim(),
                DisplayName = string.IsNullOrWhiteSpace(displayName) ? userName.Trim() : displayName.Trim()[..Math.Min(displayName.Trim().Length, 32)],
                Role = role,
                EmployeeId = NextEmployeeId(list.Select(u => u.EmployeeId)),
                PasswordHash = hash,
                PasswordSalt = salt,
                PasswordIterations = iterations,
                PasswordScheme = scheme,
                CreatedAt = DateTime.UtcNow,
                CreatedBy = createdBy,
                EmailMasked = string.IsNullOrWhiteSpace(email) ? "" : MaskEmail(email.Trim()),
            };

            if (!string.IsNullOrWhiteSpace(email))
                user.EmailCipher = Crypto.AesGcmEncrypt(email.Trim(), _emailKey, Aad(user.Id));

            list.Add(user);
            return user;
        }).ConfigureAwait(false);

        if (created is null) return (null, "用户名已存在");

        await _audit.WriteAsync(createdBy, createdBy, "", "", "user.create",
            created.UserName, "SUCCESS",
            $"角色={Roles.Label(created.Role)} 工号={created.EmployeeId}").ConfigureAwait(false);

        //镜像失败静默，不影响业务
        await MirrorAsync(created, password, "").ConfigureAwait(false);
        return (created, "");
    }

    //生成工号（E + 4 位）
    public static string NextEmployeeId(IEnumerable<string> existingIds)
    {
        var max = 0;
        foreach (var raw in existingIds)
        {
            var id = (raw ?? "").Trim();
            if (id.Length == 5 && (id[0] == 'E' || id[0] == 'e') && int.TryParse(id[1..], out var n))
                max = Math.Max(max, n);
        }
        if (max >= 9999) throw new InvalidOperationException("工号已用尽（E9999）");
        return "E" + (max + 1).ToString("D4");
    }

    //登录判断
    //时序：账号不存在 / 已禁用 / 口令错误 三条路径都只跑一次等价的 PBKDF2，响应时间不可区分
    //锁定：不再按账号全局锁定（会被用来低成本打掉他人账号），改由 AuthApi 按 (来源IP + 账号) 组合锁定
    public async Task<LoginResult> LoginAsync(string userName, string password, string ip)
    {
        var user = await FindByNameAsync(userName).ConfigureAwait(false);

        var ok = false;
        if (user is null)
        {
            //付一次等价代价后返回，避免"用户不存在更快"被枚举
            Crypto.VerifyPassword(password, "", Convert.ToBase64String(new byte[16]), _cfg.PasswordIterations,
                Crypto.PasswordSchemeCurrent, _passwordPepper, dummy: true);
        }
        else
        {
            ok = Crypto.VerifyPassword(password, user.PasswordHash, user.PasswordSalt, user.PasswordIterations,
                user.PasswordScheme, _passwordPepper);
        }

        if (user is null)
            return new LoginResult { Success = false, Error = "用户名或口令错误", AccountNotFound = true };

        if (user.Disabled)
            return new LoginResult { Success = false, Error = "该账号已被禁用，请联系管理员" };

        if (!ok)
        {
            //账号维度只留计数供审计，不再据此锁定
            var fails = await _store.MutateAsync<UserRecord, int>(StoreNames.Users, list =>
            {
                var target = list.FirstOrDefault(u => u.Id == user.Id);
                if (target is null) return 0;
                target.FailedLoginCount++;
                return target.FailedLoginCount;
            }).ConfigureAwait(false);

            await _audit.WriteAsync(user.Id, user.UserName, user.Role, ip, "auth.login", user.UserName,
                "FAIL", $"口令错误，账号累计失败 {fails} 次").ConfigureAwait(false);

            return new LoginResult { Success = false, Error = "用户名或口令错误" };
        }

        //旧口令方案就地升级
        var upgraded = Crypto.NeedsUpgrade(user.PasswordScheme, user.PasswordIterations, _cfg.PasswordIterations);
        var replacement = upgraded
            ? Crypto.HashPassword(password, _passwordPepper, _cfg.PasswordIterations)
            : default;

        await _store.MutateAsync<UserRecord, bool>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == user.Id);
            if (target is null) return false;
            target.FailedLoginCount = 0;
            target.LockedUntil = null;
            target.LastLoginAt = DateTime.UtcNow;
            target.LastLoginIp = ip;
            if (upgraded)
            {
                target.PasswordHash = replacement.hash;
                target.PasswordSalt = replacement.salt;
                target.PasswordIterations = replacement.iterations;
                target.PasswordScheme = replacement.scheme;
            }
            return true;
        }).ConfigureAwait(false);

        if (upgraded)
        {
            //返回的实例也换成新方案
            user.PasswordHash = replacement.hash;
            user.PasswordSalt = replacement.salt;
            user.PasswordIterations = replacement.iterations;
            user.PasswordScheme = replacement.scheme;

            await _audit.WriteAsync(user.Id, user.UserName, user.Role, ip, "auth.password-upgraded", user.UserName,
                "SUCCESS", $"口令存储已升级为 {replacement.scheme}（{replacement.iterations} 次迭代）").ConfigureAwait(false);
        }

        return new LoginResult { Success = true, User = user };
    }

    //改口令
    public async Task<bool> ChangePasswordAsync(string userId, string newPassword, string actor, string ip)
    {
        var user = await FindByIdAsync(userId).ConfigureAwait(false);
        if (user is null) return false;

        var error = ValidatePassword(newPassword, user.UserName);
        if (error is not null)
        {
            await _audit.WriteAsync(actor, actor, "", ip, "user.password", user.UserName, "FAIL", error).ConfigureAwait(false);
            return false;
        }

        var (hash, salt, iterations, scheme) = Crypto.HashPassword(newPassword, _passwordPepper, _cfg.PasswordIterations);
        await _store.MutateAsync<UserRecord, bool>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == userId);
            if (target is null) return false;
            target.PasswordHash = hash;
            target.PasswordSalt = salt;
            target.PasswordIterations = iterations;
            target.PasswordScheme = scheme;
            target.TokenEpoch++;          //旧令牌立即失效
            target.FailedLoginCount = 0;
            target.LockedUntil = null;
            return true;
        }).ConfigureAwait(false);

        await _audit.WriteAsync(actor, actor, "", ip, "user.password", user.UserName, "SUCCESS", "已重置口令并使旧令牌失效").ConfigureAwait(false);

        //演示口令随之轮换
        var refreshed = await FindByIdAsync(userId).ConfigureAwait(false);
        if (refreshed is not null)
        {
            refreshed.MirrorPasswordCipher = "";   //丢弃旧演示口令，重新生成
            refreshed.MirrorHashHex = "";
            refreshed.MirrorSaltHex = "";
            await MirrorAsync(refreshed, newPassword, ip).ConfigureAwait(false);
        }
        return true;
    }

    //改角色
    public async Task<bool> SetRoleAsync(string userId, string role, string actor, string ip)
    {
        if (!Roles.IsValid(role)) return false;

        var (found, name) = await _store.MutateAsync<UserRecord, (bool, string)>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == userId);
            if (target is null) return (false, "");
            target.Role = role;
            target.TokenEpoch++;
            return (true, target.UserName);
        }).ConfigureAwait(false);

        if (found)
            await _audit.WriteAsync(actor, actor, "", ip, "user.role", name, "SUCCESS", $"新角色={Roles.Label(role)}").ConfigureAwait(false);

        if (found)
        {
            var refreshed = await FindByIdAsync(userId).ConfigureAwait(false);
            if (refreshed is not null) await MirrorAsync(refreshed, null, ip).ConfigureAwait(false);
        }
        return found;
    }

    //启用 / 停用账号
    public async Task<bool> SetDisabledAsync(string userId, bool disabled, string actor, string ip)
    {
        var (found, name, role) = await _store.MutateAsync<UserRecord, (bool, string, string)>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == userId);
            if (target is null) return (false, "", "");
            target.Disabled = disabled;
            target.TokenEpoch++;
            return (true, target.UserName, target.Role);
        }).ConfigureAwait(false);

        if (found)
            await _audit.WriteAsync(actor, actor, "", ip, disabled ? "user.disable" : "user.enable",
                name, "SUCCESS", "").ConfigureAwait(false);

        if (found)
        {
            var refreshed = await FindByIdAsync(userId).ConfigureAwait(false);
            if (refreshed is not null) await MirrorAsync(refreshed, null, ip).ConfigureAwait(false);
        }
        return found;
    }

    //删除账号
    public async Task<bool> DeleteAsync(string userId, string actor, string ip)
    {
        var removed = await _store.MutateAsync<UserRecord, UserRecord?>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == userId);
            if (target is null) return null;
            list.Remove(target);
            return target;
        }).ConfigureAwait(false);

        if (removed is null) return false;

        await _audit.WriteAsync(actor, actor, "", ip, "user.delete", removed.UserName, "SUCCESS",
            $"角色={Roles.Label(removed.Role)} 工号={removed.EmployeeId}（已删除本机账号）").ConfigureAwait(false);
        return true;
    }

    //写镜像（失败静默）
    public async Task<MirrorResult> MirrorAsync(UserRecord user, string? realPassword, string ip)
    {
        if (!_mirror.Enabled)
        {
            //未配置也要落盘状态
            user.MirrorState = "skipped";
            user.MirrorError = "未配置飞书（Feishu:AppId/AppSecret/AppToken/UserTableId）";
            user.MirrorAt = DateTime.UtcNow;
            await PersistMirrorStateAsync(user).ConfigureAwait(false);
            return MirrorResult.Skip(user.MirrorError);
        }

        MirrorResult result;
        try
        {
            result = await _mirror.SyncAsync(user, realPassword).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            //再兜一次异常
            result = MirrorResult.Fail(ex.Message);
            user.MirrorState = "failed";
            user.MirrorError = ex.Message;
        }

        await PersistMirrorStateAsync(user).ConfigureAwait(false);
        if (!result.Ok) await _mirror.AuditFailureAsync(user, ip).ConfigureAwait(false);
        return result;
    }

    //镜像状态落盘
    private async Task PersistMirrorStateAsync(UserRecord snapshot)
    {
        await _store.MutateAsync<UserRecord, bool>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == snapshot.Id);
            if (target is null) return false;
            target.MirrorState = snapshot.MirrorState;
            target.MirrorError = snapshot.MirrorError;
            target.MirrorAt = snapshot.MirrorAt;
            target.MirrorRecordId = snapshot.MirrorRecordId;
            target.MirrorHashHex = snapshot.MirrorHashHex;
            target.MirrorSaltHex = snapshot.MirrorSaltHex;
            target.MirrorPasswordCipher = snapshot.MirrorPasswordCipher;
            return true;
        }).ConfigureAwait(false);
    }

    //重试镜像
    public async Task<MirrorResult> RetryMirrorAsync(string userId, string ip)
    {
        var user = await FindByIdAsync(userId).ConfigureAwait(false);
        if (user is null) return MirrorResult.Fail("账号不存在");
        return await MirrorAsync(user, null, ip).ConfigureAwait(false);
    }

    //取回演示口令
    public string? RevealDemoPassword(UserRecord user) => _mirror.RevealDemoPassword(user);

    //同步累加器
    private sealed class ImportState
    {
        public readonly List<SyncItem> Items = new();
        public int Created, Adopted, Exists, Skipped, NeedsReset;
    }

    private static readonly Regex EmployeeIdRx = new("^[Ee][0-9]{4}$", RegexOptions.Compiled);

    //导入外部账号（手工触发）
    public async Task<SyncReport> ImportFromFeishuAsync(string actorRole, string actor, string ip, bool dryRun = false)
    {
        if (!_mirror.Enabled)
        {
            return new SyncReport(false, dryRun,
                "未配置飞书（Feishu:AppId / AppSecret / AppToken / UserTableId 四者缺一即视为未接入），无法同步账号",
                0, 0, 0, 0, 0, 0, new List<SyncItem>());
        }

        var rows = await _mirror.ListAccountRowsAsync().ConfigureAwait(false);
        if (rows.Count == 0)
        {
            await _audit.WriteAsync(actor, actor, actorRole, ip, "user.sync-feishu", "用户表", "FAIL",
                "读取飞书用户表返回 0 行（表为空或读取失败被静默处理）").ConfigureAwait(false);
            return new SyncReport(false, dryRun,
                "从飞书用户表读到 0 个账号：可能是那张表还没建/还没数据，也可能是凭证或表号不对（读取失败按静默处理，具体原因见服务日志）",
                0, 0, 0, 0, 0, 0, new List<SyncItem>());
        }

        var state = new ImportState();

        if (dryRun)
        {
            //预览：只读快照试算，不落盘
            var snapshot = await AllAsync().ConfigureAwait(false);
            ApplyImport(snapshot, rows, state, actorRole, apply: false);
        }
        else
        {
            await _store.MutateAsync<UserRecord, bool>(StoreNames.Users, list =>
            {
                ApplyImport(list, rows, state, actorRole, apply: true);
                return true;
            }).ConfigureAwait(false);
        }

        var message = state.Created + state.Adopted == 0
            ? $"飞书用户表读到 {rows.Count} 个账号，本机没有需要补的账号（已存在 {state.Exists} 个，跳过 {state.Skipped} 个）"
            : $"飞书用户表读到 {rows.Count} 个账号：新建 {state.Created} 个，认领 {state.Adopted} 个，已存在 {state.Exists} 个";

        if (state.NeedsReset > 0)
            message += $"；其中 {state.NeedsReset} 个对面没有可用口令哈希，需管理员重置口令后才能登录";

        await _audit.WriteAsync(actor, actor, actorRole, ip, "user.sync-feishu", "用户表",
            dryRun ? "PREVIEW" : "SUCCESS",
            $"{(dryRun ? "预览" : "执行")}同步：读到 {rows.Count}，新建 {state.Created}，认领 {state.Adopted}，" +
            $"已存在 {state.Exists}，跳过 {state.Skipped}，待重置口令 {state.NeedsReset}").ConfigureAwait(false);

        return new SyncReport(true, dryRun, message, rows.Count, state.Created, state.Adopted,
            state.Exists, state.Skipped, state.NeedsReset, state.Items);
    }

    //算每个账号的处置方式
    private void ApplyImport(List<UserRecord> list, List<MirrorAccountRow> rows, ImportState state,
        string actorRole, bool apply)
    {
        var byName = new Dictionary<string, UserRecord>(StringComparer.OrdinalIgnoreCase);
        foreach (var u in list.Where(u => !string.IsNullOrWhiteSpace(u.UserName)))
            byName.TryAdd(u.UserName.Trim(), u);

        var usedEmployeeIds = new HashSet<string>(
            list.Select(u => u.EmployeeId).Where(id => !string.IsNullOrWhiteSpace(id)), StringComparer.OrdinalIgnoreCase);

        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var row in rows)
        {
            var name = (row.UserName ?? "").Trim();

            if (!IsUsableUserName(name))
            {
                state.Skipped++;
                state.Items.Add(new SyncItem(name, row.DisplayName, row.EmployeeId, Roles.Label(Roles.Employee), "skipped",
                    name.Length == 0 ? "对面这一行没填「用户名」，本系统无法建登录账号" : "用户名含空白/控制字符或超长，已跳过"));
                continue;
            }

            if (!seen.Add(name))
            {
                state.Skipped++;
                state.Items.Add(new SyncItem(name, row.DisplayName, row.EmployeeId, Roles.Label(Roles.Employee), "skipped",
                    "飞书用户表里存在重复用户名，只处理第一条"));
                continue;
            }

            if (byName.TryGetValue(name, out var existing))
            {
                //已有账号只补 record_id，不覆盖口令与角色
                if (apply && string.IsNullOrWhiteSpace(existing.MirrorRecordId) && !string.IsNullOrWhiteSpace(row.RecordId))
                {
                    existing.MirrorRecordId = row.RecordId;
                    existing.MirrorState = "synced";
                    existing.MirrorError = "";
                    existing.MirrorAt = DateTime.UtcNow;
                    if (string.IsNullOrWhiteSpace(existing.MirrorHashHex) && row.HasPassword)
                    {
                        existing.MirrorHashHex = row.HashHex;
                        existing.MirrorSaltHex = row.SaltHex;
                    }
                    state.Adopted++;
                    state.Items.Add(new SyncItem(name, existing.DisplayName, existing.EmployeeId, Roles.Label(existing.Role), "adopted",
                        $"本机已有该账号，已认领飞书第 {Short(row.RecordId)} 行（之后本机改动会更新到那一行）"));
                }
                else
                {
                    state.Exists++;
                    state.Items.Add(new SyncItem(name, existing.DisplayName, existing.EmployeeId, Roles.Label(existing.Role), "exists",
                        "本机已有该账号，本机是权威：口令/角色/启停均保持不变"));
                }
                continue;
            }

            var role = Roles.FromFeishuLabel(row.RoleLabel);

            //权限矩阵优先，越权则逐行跳过
            var policyError = AccountPolicy.CanCreateAccount(actorRole, role);
            if (policyError is not null)
            {
                state.Skipped++;
                state.Items.Add(new SyncItem(name, row.DisplayName, row.EmployeeId, Roles.Label(role), "skipped",
                    $"对面角色「{row.RoleLabel}」需要 {Roles.Label(role)}，但{policyError}"));
                continue;
            }

            //工号一致则沿用，冲突则本机新发
            var employeeId = "";
            if (EmployeeIdRx.IsMatch(row.EmployeeId) && !usedEmployeeIds.Contains(row.EmployeeId))
                employeeId = row.EmployeeId.ToUpperInvariant();
            else
                employeeId = NextEmployeeId(list.Select(x => x.EmployeeId).Concat(usedEmployeeIds));

            var user = new UserRecord
            {
                Id = "u_" + Crypto.RandomHex(6),
                UserName = name,
                DisplayName = string.IsNullOrWhiteSpace(row.DisplayName) ? name : Trim(row.DisplayName, 32),
                EmployeeId = employeeId,
                Role = role,
                Origin = "feishu-sync",
                CreatedAt = row.CreatedAt ?? DateTime.UtcNow,
                CreatedBy = "feishu-sync",
                LastLoginAt = row.LastLoginAt,
                Disabled = row.Disabled,
                MirrorRecordId = row.RecordId,
                MirrorState = "synced",
                MirrorAt = DateTime.UtcNow,
            };

            var needsReset = false;
            if (row.HasPassword)
            {
                try
                {
                    user.PasswordHash = Convert.ToBase64String(Convert.FromHexString(row.HashHex));
                    user.PasswordSalt = Convert.ToBase64String(Convert.FromHexString(row.SaltHex));
                    //按外部方案校验，首次登录后自动升级
                    user.PasswordIterations = Math.Max(1, _cfg.Feishu.MirrorPbkdf2Iterations);
                    user.PasswordScheme = Crypto.PasswordSchemePartner;
                    user.MirrorHashHex = row.HashHex;
                    user.MirrorSaltHex = row.SaltHex;
                }
                catch (FormatException)
                {
                    user.PasswordHash = "";
                    user.PasswordSalt = "";
                    needsReset = true;
                }
            }
            else
            {
                needsReset = true;
            }

            if (needsReset) state.NeedsReset++;
            if (apply) list.Add(user);
            usedEmployeeIds.Add(employeeId);
            state.Created++;
            state.Items.Add(new SyncItem(name, user.DisplayName, employeeId, Roles.Label(role), "created",
                $"已导入（飞书角色「{row.RoleLabel}」→ {Roles.Label(role)}；工号 {employeeId}；"
                + (needsReset ? "对面没有可用口令哈希，需重置口令后才能登录" : "沿用对面口令哈希，可用原口令登录")
                + (row.Disabled ? "；对面标记为停用，本机同样置为停用" : "") + "）",
                needsReset));
        }
    }

    //导入时的用户名校验
    private static bool IsUsableUserName(string name)
        => name.Length is > 0 and <= 64
           && !name.Any(c => char.IsWhiteSpace(c) || char.IsControl(c))
           && !name.Contains('<') && !name.Contains('>');

    //截断超长文本
    private static string Trim(string value, int max)
    {
        var v = (value ?? "").Trim();
        return v.Length <= max ? v : v[..max];
    }

    //截短记录号用于展示
    private static string Short(string recordId)
        => recordId.Length <= 10 ? recordId : recordId[..10];


    //AI 每日配额
    public async Task<(bool Allowed, int Used)> TryConsumeAiQuotaAsync(string userId)
    {
        var today = DateTime.UtcNow.ToString("yyyy-MM-dd");
        return await _store.MutateAsync<UserRecord, (bool, int)>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == userId);
            if (target is null) return (false, 0);

            if (!string.Equals(target.AiQuotaDate, today, StringComparison.Ordinal))
            {
                target.AiQuotaDate = today;
                target.AiCallsToday = 0;
            }

            if (target.AiCallsToday >= _cfg.Ai.DailyCallsPerUser) return (false, target.AiCallsToday);

            target.AiCallsToday++;
            return (true, target.AiCallsToday);
        }).ConfigureAwait(false);
    }

    //报销单 AI 预审的独立每日配额（与对话配额分开，互不挤占）
    public async Task<(bool Allowed, int Used)> TryConsumeInvoiceAiQuotaAsync(string userId)
    {
        var today = DateTime.UtcNow.ToString("yyyy-MM-dd");
        return await _store.MutateAsync<UserRecord, (bool, int)>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == userId);
            if (target is null) return (false, 0);

            if (!string.Equals(target.InvoiceAiQuotaDate, today, StringComparison.Ordinal))
            {
                target.InvoiceAiQuotaDate = today;
                target.InvoiceAiCallsToday = 0;
            }

            if (target.InvoiceAiCallsToday >= _cfg.InvoiceAi.DailyCallsPerUser) return (false, target.InvoiceAiCallsToday);

            target.InvoiceAiCallsToday++;
            return (true, target.InvoiceAiCallsToday);
        }).ConfigureAwait(false);
    }

    //解密邮箱
    public string DecryptEmail(UserRecord user)    {
        if (string.IsNullOrEmpty(user.EmailCipher)) return "";
        return Crypto.AesGcmDecrypt(user.EmailCipher, _emailKey, Aad(user.Id)) ?? "";
    }

    //邮箱附加认证数据
    private static string Aad(string userId) => "aiapproval:user-email:" + userId;

    //邮箱脱敏显示
    private static string MaskEmail(string email)
    {
        var at = email.IndexOf('@');
        if (at <= 0) return Crypto.Mask(email);
        var head = email[..at];
        var domain = email[(at + 1)..];
        return (head.Length <= 2 ? head[..1] + "*" : head[..2] + new string('*', Math.Min(4, head.Length - 2))) + "@" + domain;
    }

    //播种初始超级管理员
    public async Task<string?> SeedBootstrapSuperAdminAsync()
    {
        var opt = _cfg.BootstrapAdmin;
        if (!opt.Enabled || opt.UserName.Length == 0) return null;

        var existing = await FindByNameAsync(opt.UserName).ConfigureAwait(false);
        if (existing is not null)
        {
            //已存在：只补工号
            if (string.IsNullOrWhiteSpace(existing.EmployeeId))
            {
                await _store.MutateAsync<UserRecord, bool>(StoreNames.Users, list =>
                {
                    var target = list.FirstOrDefault(u => u.Id == existing.Id);
                    if (target is null) return false;
                    target.EmployeeId = NextEmployeeId(list.Select(x => x.EmployeeId));
                    return true;
                }).ConfigureAwait(false);
            }
            return null;
        }

        var hash = opt.PasswordHash;
        var salt = opt.PasswordSalt;
        var iterations = opt.PasswordIterations;
        var scheme = opt.PasswordScheme;

        if (hash.Length == 0 || salt.Length == 0)
        {
            //只有明文则现算哈希
            (hash, salt, iterations, scheme) = Crypto.HashPassword(opt.Password, _passwordPepper, _cfg.PasswordIterations);
        }

        var created = await _store.MutateAsync<UserRecord, UserRecord?>(StoreNames.Users, list =>
        {
            if (list.Any(u => string.Equals(u.UserName, opt.UserName, StringComparison.OrdinalIgnoreCase))) return null;

            var user = new UserRecord
            {
                Id = "u_" + Crypto.RandomHex(6),
                UserName = opt.UserName,
                DisplayName = opt.DisplayName.Length == 0 ? opt.UserName : Trim(opt.DisplayName, 32),
                EmployeeId = NextEmployeeId(list.Select(u => u.EmployeeId)),
                Role = Roles.SuperAdmin,
                Origin = "bootstrap",
                PasswordHash = hash,
                PasswordSalt = salt,
                PasswordIterations = iterations,
                PasswordScheme = scheme,
                CreatedAt = DateTime.UtcNow,
                CreatedBy = "bootstrap",
                //初始超管不镜像
                MirrorState = "skipped",
                MirrorError = "初始超级管理员不镜像（口令只存在于本机配置文件）",
                MirrorAt = DateTime.UtcNow,
            };
            list.Add(user);
            return user;
        }).ConfigureAwait(false);

        if (created is null) return null;

        await _audit.WriteAsync("system", "system", Roles.SuperAdmin, "", "system.bootstrap-admin", created.UserName,
            "SUCCESS", $"已按本地配置创建初始超级管理员（工号 {created.EmployeeId}）；口令取自 Bootstrap:Admin 配置").ConfigureAwait(false);

        return $"初始超级管理员 {created.UserName}（工号 {created.EmployeeId}）已创建，口令来自 BootstrapAdmin 配置——"
             + "首次登录后请立刻改成强口令，并在交付/上传前清空 config/secrets.local.json 里的 BootstrapAdmin 段";
    }

    //播种演示账号
    public async Task<List<string>> EnsureSeedAsync()
    {
        var notes = await MigrateLegacyAsync().ConfigureAwait(false);
        var all = await _store.ReadListAsync<UserRecord>(StoreNames.Users).ConfigureAwait(false);
        if (all.Count > 0)
        {
            //补齐旧库缺失的工号
            await BackfillEmployeeIdsAsync().ConfigureAwait(false);
            return notes;
        }

        var seeds = new (string Name, string Password, string Role, string Display)[]
        {
            ("employee", "Staff#2026abc", Roles.Employee, "张三"),
            ("finance", "Money#2026abc", Roles.Finance, "李四"),
            ("admin", "SysRoot#2026ak", Roles.Admin, "王五"),
            ("root", "Overlord#2026ak", Roles.SuperAdmin, "赵六"),
        };

        foreach (var seed in seeds)
        {
            var (user, error) = await CreateAsync(seed.Name, seed.Password, seed.Display, seed.Role, "", "seed").ConfigureAwait(false);
            if (user is not null)
                notes.Add($"演示账号 {seed.Name} / {seed.Password}（{Roles.Label(seed.Role)}，工号 {user.EmployeeId}）");
            else
                notes.Add($"演示账号 {seed.Name} 创建失败：{error}");
        }

        //建用户工作区目录
        foreach (var user in await _store.ReadListAsync<UserRecord>(StoreNames.Users).ConfigureAwait(false))
            Directory.CreateDirectory(Path.Combine(_cfg.WorkspacesDir, "u_" + user.Id));

        if (notes.Count > 0)
            await _audit.WriteAsync("system", "system", "system", "", "system.seed", "users.json", "SUCCESS",
                $"已播种 {notes.Count} 个演示账号").ConfigureAwait(false);

        return notes;
    }

    //旧角色迁移
    private async Task<List<string>> MigrateLegacyAsync()
    {
        var notes = new List<string>();
        var migrated = await _store.MutateAsync<UserRecord, List<string>>(StoreNames.Users, list =>
        {
            var changed = new List<string>();
            var used = new HashSet<string>(list.Select(u => u.EmployeeId).Where(id => !string.IsNullOrEmpty(id)), StringComparer.OrdinalIgnoreCase);
            foreach (var u in list)
            {
                var mapped = Roles.Migrate(u.Role);
                if (mapped != u.Role)
                {
                    changed.Add($"{u.UserName}：{Roles.Label(u.Role)} → {Roles.Label(mapped)}");
                    u.Role = mapped;
                    u.TokenEpoch++;
                }
                if (string.IsNullOrWhiteSpace(u.EmployeeId))
                {
                    u.EmployeeId = NextEmployeeId(list.Select(x => x.EmployeeId));
                    used.Add(u.EmployeeId);
                }
            }
            return changed;
        }).ConfigureAwait(false);

        foreach (var line in migrated)
            notes.Add($"角色迁移：{line}");

        if (migrated.Count > 0)
            await _audit.WriteAsync("system", "system", "system", "", "system.migrate-roles", "users.json", "SUCCESS",
                string.Join("；", migrated)).ConfigureAwait(false);
        return notes;
    }

    //给旧账号补工号
    private async Task BackfillEmployeeIdsAsync()
    {
        await _store.MutateAsync<UserRecord, bool>(StoreNames.Users, list =>
        {
            var used = new HashSet<string>(list.Select(u => u.EmployeeId).Where(id => !string.IsNullOrEmpty(id)), StringComparer.OrdinalIgnoreCase);
            foreach (var u in list.Where(x => string.IsNullOrWhiteSpace(x.EmployeeId)))
            {
                u.EmployeeId = NextEmployeeId(list.Select(x => x.EmployeeId));
                used.Add(u.EmployeeId);
            }
            return true;
        }).ConfigureAwait(false);
    }
}
