using System.Collections.Concurrent;
using AiApproval.Core;

namespace AiApproval.Security;

public sealed class BanRecord
{
    public string Ip { get; set; } = "";
    public DateTime BannedAt { get; set; }
    public DateTime BannedUntil { get; set; }
    public int Strikes { get; set; }
    public string Reason { get; set; } = "";
    public int HoneypotHits { get; set; }
    public int WafHits { get; set; }
    public int LoginFails { get; set; }
    public List<string> LastEvents { get; set; } = new();
}

public sealed class IpState
{
    public int Strikes;
    public int HoneypotHits;
    public int WafHits;
    public int LoginFails;
    public DateTime LastSeen;
    public DateTime? BannedUntil;
    public string LastReason = "";
    public readonly List<string> Events = new();
}

/// <summary>
/// 运行时防线状态机（内存实现，进程重启即清零；账号锁定状态则持久化在 users.json）：
///   * 固定窗口限流（登录、AI 对话、读接口、后台写操作各自独立桶）；
///   * 攻击计分（strike）与自动封禁：WAF 命中累计 8 次、蜜罐探测 3 次即封禁 60 分钟；
///   * Anti-Replay 随机数一次性校验（时间窗内 nonce 不可复用）。
/// 内存实现的好处：不会因为"日志写满了 disk"而失效；坏处：多实例部署时要换成 Redis（已在 README 说明）。
/// </summary>
public sealed class SecurityGuard
{
    private sealed class Window
    {
        public long StartTicks;
        public int Count;
    }

    private readonly object _sync = new();
    private readonly ConcurrentDictionary<string, Window> _windows = new(StringComparer.Ordinal);
    private readonly Dictionary<string, IpState> _ips = new(StringComparer.Ordinal);
    private readonly Dictionary<string, DateTime> _nonces = new(StringComparer.Ordinal);
    private readonly LimitOptions _limits;
    private readonly int _nonceWindowSeconds;

    public long WafBlocks { get; private set; }
    public long HoneypotHits { get; private set; }
    public long LoginFailures { get; private set; }
    public long RateLimited { get; private set; }
    public long ReplayRejected { get; private set; }
    public long SignatureRejected { get; private set; }
    public long BannedRequests { get; private set; }

    /// <summary>记录一次令牌/签名校验失败（计数器对外只读，避免外部随意改写安全指标）。</summary>
    public void NoteSignatureRejected() => SignatureRejected++;

    /// <summary>后台写操作的每分钟上限（读自 <c>Limits:AdminWritePerMinutePerAdmin</c>）。</summary>
    public int AdminWritePerMinute => _limits.AdminWritePerMinutePerAdmin;

    /// <summary>业务对接台写操作的每 5 分钟上限（读自 <c>Limits:BizActionPerFiveMinutes*</c>）。</summary>
    public int BizActionPerFiveMinutesPerUser => _limits.BizActionPerFiveMinutesPerUser;
    public int BizActionPerFiveMinutesPerIp => _limits.BizActionPerFiveMinutesPerIp;

    public SecurityGuard(LimitOptions limits, int nonceWindowSeconds)
    {
        _limits = limits;
        _nonceWindowSeconds = nonceWindowSeconds;
    }

    // ---------------------------------------------------------------- 限流

    /// <summary>固定窗口计数。key 由调用方组合（例如 "login:ip:1.2.3.4"、"chat:user:u1"）。</summary>
    public bool TryConsume(string key, int limit, TimeSpan window)
    {
        var now = DateTime.UtcNow.Ticks;
        var w = _windows.GetOrAdd(key, _ => new Window { StartTicks = now, Count = 0 });

        lock (_sync)
        {
            if (now - w.StartTicks > window.Ticks)
            {
                w.StartTicks = now;
                w.Count = 0;
            }

            if (w.Count >= limit)
            {
                RateLimited++;
                return false;
            }

            w.Count++;
            return true;
        }
    }

    public int Remaining(string key, int limit, TimeSpan window)
    {
        if (!_windows.TryGetValue(key, out var w)) return limit;
        lock (_sync)
        {
            if (DateTime.UtcNow.Ticks - w.StartTicks > window.Ticks) return limit;
            return Math.Max(0, limit - w.Count);
        }
    }

    // ---------------------------------------------------------------- 攻击计分 / 封禁

    public bool IsBanned(string ip, out DateTime until, out string reason)
    {
        until = default;
        reason = "";
        lock (_sync)
        {
            if (!_ips.TryGetValue(ip, out var state)) return false;
            if (state.BannedUntil is { } t && t > DateTime.UtcNow)
            {
                until = t;
                reason = state.LastReason;
                BannedRequests++;
                return true;
            }
            return false;
        }
    }

    /// <summary>记录一次攻击特征。weight 越大越快触发封禁（蜜罐探测权重最高）。</summary>
    public bool Strike(string ip, string reason, int weight = 1, bool honeypot = false, bool waf = false, bool loginFail = false)
    {
        if (weight <= 0) return false;

        lock (_sync)
        {
            if (!_ips.TryGetValue(ip, out var state))
            {
                state = new IpState { LastSeen = DateTime.UtcNow };
                _ips[ip] = state;
            }

            state.Strikes += weight;
            state.LastSeen = DateTime.UtcNow;
            state.LastReason = reason;
            if (honeypot) { state.HoneypotHits++; HoneypotHits++; }
            if (waf) { state.WafHits++; WafBlocks++; }
            if (loginFail) { state.LoginFails++; LoginFailures++; }

            state.Events.Add($"{DateTime.UtcNow:HH:mm:ss} {reason} (+{weight})");
            if (state.Events.Count > 12) state.Events.RemoveAt(0);

            var threshold = honeypot && state.HoneypotHits >= _limits.HoneypotStrikesBeforeBan
                ? _limits.HoneypotStrikesBeforeBan
                : _limits.WafStrikesBeforeBan;

            var triggered = (honeypot && state.HoneypotHits >= _limits.HoneypotStrikesBeforeBan)
                            || state.Strikes >= _limits.WafStrikesBeforeBan;

            if (triggered && (state.BannedUntil is null || state.BannedUntil < DateTime.UtcNow))
            {
                state.BannedUntil = DateTime.UtcNow.AddMinutes(_limits.BanMinutes);
                state.LastReason = $"自动封禁：{reason}（阈值 {threshold}）";
                return true;
            }

            return false;
        }
    }

    public void Ban(string ip, string reason, int minutes)
    {
        lock (_sync)
        {
            if (!_ips.TryGetValue(ip, out var state))
            {
                state = new IpState();
                _ips[ip] = state;
            }
            state.BannedUntil = DateTime.UtcNow.AddMinutes(minutes);
            state.LastReason = reason;
            state.Events.Add($"{DateTime.UtcNow:HH:mm:ss} 封禁：{reason}（{minutes} 分钟）");
        }
    }

    public bool Unban(string ip)
    {
        lock (_sync)
        {
            if (!_ips.TryGetValue(ip, out var state)) return false;
            state.BannedUntil = null;
            state.Strikes = 0;
            state.HoneypotHits = 0;
            state.WafHits = 0;
            state.LastReason = "已由管理员解封";
            state.Events.Add($"{DateTime.UtcNow:HH:mm:ss} 管理员解封");
            return true;
        }
    }

    public object Snapshot()
    {
        lock (_sync)
        {
            var now = DateTime.UtcNow;
            var list = _ips.OrderByDescending(kv => kv.Value.Strikes).Take(50).Select(kv => new
            {
                ip = kv.Key,
                strikes = kv.Value.Strikes,
                honeypotHits = kv.Value.HoneypotHits,
                wafHits = kv.Value.WafHits,
                loginFails = kv.Value.LoginFails,
                banned = kv.Value.BannedUntil is { } t && t > now,
                bannedUntil = kv.Value.BannedUntil,
                lastReason = kv.Value.LastReason,
                lastSeen = kv.Value.LastSeen,
                events = kv.Value.Events,
            }).ToList();

            return new
            {
                wafBlocks = WafBlocks,
                honeypotHits = HoneypotHits,
                loginFailures = LoginFailures,
                rateLimited = RateLimited,
                replayRejected = ReplayRejected,
                signatureRejected = SignatureRejected,
                bannedRequests = BannedRequests,
                activeBans = list.Count(x => x.banned),
                trackedIps = _ips.Count,
                offsets = list,
            };
        }
    }

    // ---------------------------------------------------------------- Anti-Replay

    /// <summary>
    /// 一次性随机数校验。同一 (主体 + nonce) 在时间窗内只能出现一次，
    /// 保证"抓包重放"在窗口内也必然失败（就算时间戳仍有效）。
    /// </summary>
    public bool TryUseNonce(string subject, string nonce, out string reason)
    {
        reason = "";
        if (string.IsNullOrWhiteSpace(nonce) || nonce.Length < 16 || nonce.Length > 128)
        {
            reason = "随机数长度非法";
            ReplayRejected++;
            return false;
        }

        var key = subject + "|" + nonce;
        var now = DateTime.UtcNow;

        lock (_sync)
        {
            if (_nonces.Count > 20000) Cleanup(now);
            if (_nonces.ContainsKey(key))
            {
                reason = "随机数已被使用（重放）";
                ReplayRejected++;
                return false;
            }
            _nonces[key] = now.AddSeconds(_nonceWindowSeconds);
            return true;
        }
    }

    private void Cleanup(DateTime now)
    {
        foreach (var key in _nonces.Where(kv => kv.Value < now).Select(kv => kv.Key).ToList())
            _nonces.Remove(key);

        foreach (var key in _ips.Where(kv => kv.Value.LastSeen < now.AddHours(-6)
                                            && (kv.Value.BannedUntil is null || kv.Value.BannedUntil < now))
                     .Select(kv => kv.Key).ToList())
            _ips.Remove(key);
    }

    public static string ClientIp(HttpContext context)
    {
        // 只信任连接对端地址：反向代理场景请在代理处覆盖，不要盲目信任 X-Forwarded-For（可伪造）
        return context.Connection.RemoteIpAddress?.ToString() ?? "unknown";
    }
}
