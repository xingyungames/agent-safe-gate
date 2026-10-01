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

/// <summary>
/// 用户服务：用户 CRUD、口令校验与账号锁定、AI 配额、令牌世代管理。
/// users.json 就是"数据库表"，所有写操作走 JsonStore 的原子写 + 文件级锁。
/// </summary>
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
    private readonly byte[] _emailKey;

    public UserService(JsonStore store, AppConfig cfg, AuditLog audit)
    {
        _store = store;
        _cfg = cfg;
        _audit = audit;
        _emailKey = cfg.DeriveKey("email");
    }

    public Task<List<UserRecord>> AllAsync() => _store.ReadListAsync<UserRecord>(StoreNames.Users);

    public async Task<UserRecord?> FindByNameAsync(string userName)
    {
        if (string.IsNullOrWhiteSpace(userName)) return null;
        var name = userName.Trim();
        var all = await _store.ReadListAsync<UserRecord>(StoreNames.Users).ConfigureAwait(false);
        return all.FirstOrDefault(u => string.Equals(u.UserName, name, StringComparison.OrdinalIgnoreCase));
    }

    public async Task<UserRecord?> FindByIdAsync(string id)
    {
        if (string.IsNullOrWhiteSpace(id)) return null;
        var all = await _store.ReadListAsync<UserRecord>(StoreNames.Users).ConfigureAwait(false);
        return all.FirstOrDefault(u => string.Equals(u.Id, id, StringComparison.Ordinal));
    }

    // ------------------------------------------------------------- 口令策略

    public static string? ValidateUserName(string? userName)
    {
        var name = (userName ?? "").Trim();
        if (!UserNameRx.IsMatch(name)) return "用户名必须为 3-32 位，只允许字母、数字、下划线、点、短横线";
        return null;
    }

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

    // ------------------------------------------------------------- 创建 / 维护

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

            var (hash, salt, iterations) = Crypto.HashPassword(password);
            var user = new UserRecord
            {
                Id = "u_" + Crypto.RandomHex(6),
                UserName = userName.Trim(),
                DisplayName = string.IsNullOrWhiteSpace(displayName) ? userName.Trim() : displayName.Trim()[..Math.Min(displayName.Trim().Length, 32)],
                Role = role,
                PasswordHash = hash,
                PasswordSalt = salt,
                PasswordIterations = iterations,
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

        await _audit.WriteAsync(createdBy, createdBy, Roles.Admin, "", "user.create",
            created.UserName, "SUCCESS", $"角色={Roles.Label(created.Role)}").ConfigureAwait(false);
        return (created, "");
    }

    public async Task<LoginResult> LoginAsync(string userName, string password, string ip)
    {
        var user = await FindByNameAsync(userName).ConfigureAwait(false);
        var now = DateTime.UtcNow;

        if (user is null)
        {
            // 账号不存在也要付出同样的 PBKDF2 代价，避免通过响应时间枚举用户名
            Crypto.VerifyPassword(password, "", Convert.ToBase64String(new byte[16]), Crypto.Pbkdf2Iterations, dummy: true);
            return new LoginResult { Success = false, Error = "用户名或口令错误", AccountNotFound = true };
        }

        if (user.Disabled)
            return new LoginResult { Success = false, Error = "该账号已被禁用，请联系管理员" };

        if (user.IsLocked)
            return new LoginResult { Success = false, Locked = true, LockedUntil = user.LockedUntil, Error = "账号因多次登录失败被临时锁定" };

        var ok = Crypto.VerifyPassword(password, user.PasswordHash, user.PasswordSalt, user.PasswordIterations);
        if (!ok)
        {
            var updated = await _store.MutateAsync<UserRecord, (int Fails, DateTime? Locked)>(StoreNames.Users, list =>
            {
                var target = list.FirstOrDefault(u => u.Id == user.Id);
                if (target is null) return (0, null);

                target.FailedLoginCount++;
                if (target.FailedLoginCount >= _cfg.Limits.LoginPerFifteenMinutesPerAccount)
                {
                    target.LockedUntil = DateTime.UtcNow.AddMinutes(_cfg.Limits.AccountLockMinutes);
                    target.FailedLoginCount = 0;
                    return (0, target.LockedUntil);
                }
                return (target.FailedLoginCount, null);
            }).ConfigureAwait(false);

            await _audit.WriteAsync(user.Id, user.UserName, user.Role, ip, "auth.login", user.UserName,
                "FAIL", $"口令错误，累计失败 {updated.Fails} 次{(updated.Locked is null ? "" : "，账号已锁定")}").ConfigureAwait(false);

            return new LoginResult
            {
                Success = false,
                Locked = updated.Locked is not null,
                LockedUntil = updated.Locked,
                Error = updated.Locked is not null
                    ? $"多次登录失败，账号已锁定至 {updated.Locked:HH:mm:ss}"
                    : "用户名或口令错误",
            };
        }

        await _store.MutateAsync<UserRecord, bool>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == user.Id);
            if (target is null) return false;
            target.FailedLoginCount = 0;
            target.LockedUntil = null;
            target.LastLoginAt = DateTime.UtcNow;
            target.LastLoginIp = ip;
            return true;
        }).ConfigureAwait(false);

        return new LoginResult { Success = true, User = user };
    }

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

        var (hash, salt, iterations) = Crypto.HashPassword(newPassword);
        await _store.MutateAsync<UserRecord, bool>(StoreNames.Users, list =>
        {
            var target = list.FirstOrDefault(u => u.Id == userId);
            if (target is null) return false;
            target.PasswordHash = hash;
            target.PasswordSalt = salt;
            target.PasswordIterations = iterations;
            target.TokenEpoch++;          // 立刻失效所有旧令牌
            target.FailedLoginCount = 0;
            target.LockedUntil = null;
            return true;
        }).ConfigureAwait(false);

        await _audit.WriteAsync(actor, actor, "", ip, "user.password", user.UserName, "SUCCESS", "已重置口令并使旧令牌失效").ConfigureAwait(false);
        return true;
    }

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
            await _audit.WriteAsync(actor, actor, Roles.Admin, ip, "user.role", name, "SUCCESS", $"新角色={Roles.Label(role)}").ConfigureAwait(false);
        return found;
    }

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
            await _audit.WriteAsync(actor, actor, Roles.Admin, ip, disabled ? "user.disable" : "user.enable",
                name, "SUCCESS", "").ConfigureAwait(false);
        return found;
    }

    /// <summary>AI 每日配额（防"经济型 DoS"：攻击者刷接口烧掉真金白银的 API 额度）。</summary>
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

    public string DecryptEmail(UserRecord user)
    {
        if (string.IsNullOrEmpty(user.EmailCipher)) return "";
        return Crypto.AesGcmDecrypt(user.EmailCipher, _emailKey, Aad(user.Id)) ?? "";
    }

    private static string Aad(string userId) => "aiapproval:user-email:" + userId;

    private static string MaskEmail(string email)
    {
        var at = email.IndexOf('@');
        if (at <= 0) return Crypto.Mask(email);
        var head = email[..at];
        var domain = email[(at + 1)..];
        return (head.Length <= 2 ? head[..1] + "*" : head[..2] + new string('*', Math.Min(4, head.Length - 2))) + "@" + domain;
    }

    /// <summary>
    /// 首次启动播种三个演示账号（用户 / 员工 / 管理员）。
    /// 这是刻意为之的演示入口：口令会打印在控制台，README 明确要求部署后立即修改。
    /// </summary>
    public async Task<List<string>> EnsureSeedAsync()
    {
        var notes = new List<string>();
        var all = await _store.ReadListAsync<UserRecord>(StoreNames.Users).ConfigureAwait(false);
        if (all.Count > 0) return notes;

        var seeds = new (string Name, string Password, string Role, string Display)[]
        {
            ("zhangsan", "User#2026abc", Roles.User, "张三（业务专员）"),
            ("lisi", "Staff#2026abc", Roles.Staff, "李四（运营工程师）"),
            // 注意：口令策略拒绝"包含用户名"的口令，因此管理员初始口令不能含 admin 字样
            ("admin", "SysRoot#2026ak", Roles.Admin, "系统管理员"),
        };

        foreach (var seed in seeds)
        {
            var (user, error) = await CreateAsync(seed.Name, seed.Password, seed.Display, seed.Role, "", "seed").ConfigureAwait(false);
            if (user is not null)
                notes.Add($"演示账号 {seed.Name} / {seed.Password}（{Roles.Label(seed.Role)}）");
            else
                notes.Add($"演示账号 {seed.Name} 创建失败：{error}");
        }

        // 每个用户一个工作区目录，员工另有共享区
        foreach (var user in await _store.ReadListAsync<UserRecord>(StoreNames.Users).ConfigureAwait(false))
            Directory.CreateDirectory(Path.Combine(_cfg.WorkspacesDir, "u_" + user.Id));

        if (notes.Count > 0)
            await _audit.WriteAsync("system", "system", "system", "", "system.seed", "users.json", "SUCCESS",
                $"已播种 {notes.Count} 个演示账号").ConfigureAwait(false);

        return notes;
    }
}
