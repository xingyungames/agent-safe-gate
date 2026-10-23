using System.Collections.Concurrent;
using System.Net;
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

//运行时防线状态机（内存实现）
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

    //IP 白名单：只豁免限流与自动封禁，不豁免认证/签名/权限/规则引擎
    private readonly HashSet<string> _allowlist;

    public long WafBlocks { get; private set; }
    public long HoneypotHits { get; private set; }
    public long LoginFailures { get; private set; }
    public long RateLimited { get; private set; }
    public long ReplayRejected { get; private set; }
    public long SignatureRejected { get; private set; }
    public long BannedRequests { get; private set; }

    //白名单放行次数（用于核对安全测试打了多少请求）
    public long AllowlistedHits { get; private set; }
    public IReadOnlyCollection<string> Allowlist => _allowlist;

    //记录一次签名校验失败
    public void NoteSignatureRejected() => SignatureRejected++;

    //后台写操作每分钟上限
    public int AdminWritePerMinute => _limits.AdminWritePerMinutePerAdmin;

    //是否白名单 IP（只影响限流与封禁，不影响任何鉴权判断）
    public bool IsAllowlisted(string? ip)
        => !string.IsNullOrEmpty(ip) && _allowlist.Count > 0 && _allowlist.Contains(ip);



    public SecurityGuard(LimitOptions limits, int nonceWindowSeconds, IEnumerable<string>? allowlistIps = null)
    {
        _limits = limits;
        _nonceWindowSeconds = nonceWindowSeconds;
        _allowlist = new HashSet<string>(
            (allowlistIps ?? Array.Empty<string>()).Where(x => !string.IsNullOrWhiteSpace(x)).Select(x => x.Trim()),
            StringComparer.Ordinal);
    }

    //限流

    //固定窗口计数；allowlistIp 命中白名单时直接放行（仍计数，便于观察压力）
    public bool TryConsume(string key, int limit, TimeSpan window, string? allowlistIp = null)
    {
        var now = DateTime.UtcNow.Ticks;
        var w = _windows.GetOrAdd(key, _ => new Window { StartTicks = now, Count = 0 });

        lock (_sync)
        {
            var exempt = IsAllowlisted(allowlistIp);
            if (now - w.StartTicks > window.Ticks)
            {
                w.StartTicks = now;
                w.Count = 0;
            }

            if (exempt)
            {
                w.Count++;
                AllowlistedHits++;
                return true;
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

    //剩余配额
    public int Remaining(string key, int limit, TimeSpan window)
    {
        if (!_windows.TryGetValue(key, out var w)) return limit;
        lock (_sync)
        {
            if (DateTime.UtcNow.Ticks - w.StartTicks > window.Ticks) return limit;
            return Math.Max(0, limit - w.Count);
        }
    }

    //攻击计分与封禁

    //是否已被封禁
    public bool IsBanned(string ip, out DateTime until, out string reason)
    {
        until = default;
        reason = "";

        //白名单永不被封
        if (IsAllowlisted(ip)) return false;

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

    //记一次攻击特征，weight 累加
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

            //白名单：记分照记，但永不触发自动封禁
            if (IsAllowlisted(ip)) return false;

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

    //手动封禁
    public void Ban(string ip, string reason, int minutes)
    {
        //白名单永不被封（蜜罐命中也只记分）
        if (IsAllowlisted(ip)) return;

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

    //解封
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

    //登录失败锁定：按 (来源IP + 账号) 组合计数
    //攻击者只能锁死"自己这条来源 IP 对某个账号"的通道，锁不掉别人的正常登录
    private sealed class LoginPair
    {
        public int Fails;
        public DateTime WindowStart = DateTime.UtcNow;
        public DateTime? LockedUntil;
    }

    private readonly Dictionary<string, LoginPair> _loginPairs = new(StringComparer.Ordinal);

    private static string PairKey(string? ip, string? userName)
        => (ip ?? "").Trim() + "|" + (userName ?? "").Trim().ToLowerInvariant();

    //该 (来源IP, 账号) 组合是否处于锁定期
    public bool IsLoginLocked(string ip, string userName, out DateTime lockedUntil)
    {
        lockedUntil = default;
        var key = PairKey(ip, userName);
        lock (_sync)
        {
            if (!_loginPairs.TryGetValue(key, out var st) || st.LockedUntil is null) return false;
            if (st.LockedUntil.Value <= DateTime.UtcNow) { st.LockedUntil = null; return false; }
            lockedUntil = st.LockedUntil.Value;
            return true;
        }
    }

    //记一次登录失败；达到阈值就锁该组合
    public bool RecordLoginFailure(string ip, string userName, int threshold, TimeSpan window, TimeSpan lockFor,
        out DateTime lockedUntil)
    {
        lockedUntil = default;
        var key = PairKey(ip, userName);
        var now = DateTime.UtcNow;
        var need = Math.Max(1, threshold);

        lock (_sync)
        {
            if (_loginPairs.Count > 20000) CleanupLoginPairs(now);

            if (!_loginPairs.TryGetValue(key, out var st))
            {
                st = new LoginPair { WindowStart = now };
                _loginPairs[key] = st;
            }

            if (now - st.WindowStart > window)
            {
                st.Fails = 0;
                st.WindowStart = now;
            }

            st.Fails++;
            if (st.Fails < need) return false;

            st.Fails = 0;
            st.LockedUntil = now.Add(lockFor);
            lockedUntil = st.LockedUntil.Value;
            return true;
        }
    }

    //登录成功后清掉该组合的失败计数
    public void ClearLoginFailure(string ip, string userName)
    {
        var key = PairKey(ip, userName);
        lock (_sync) { _loginPairs.Remove(key); }
    }

    //清理过期的组合状态
    private void CleanupLoginPairs(DateTime now)
    {
        foreach (var key in _loginPairs
                     .Where(kv => kv.Value.Fails == 0 &&
                                  (kv.Value.LockedUntil is null || kv.Value.LockedUntil < now))
                     .Select(kv => kv.Key)
                     .ToList())
        {
            _loginPairs.Remove(key);
        }
    }

    //安全态势快照
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
                allowlistCount = _allowlist.Count,
                allowlistedHits = AllowlistedHits,
                allowlist = _allowlist.ToArray(),
                activeBans = list.Count(x => x.banned),
                trackedIps = _ips.Count,
                offsets = list,
            };
        }
    }

    //Anti-Replay

    //一次性随机数校验，防重放
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

    //清理过期状态
    private void Cleanup(DateTime now)
    {
        foreach (var key in _nonces.Where(kv => kv.Value < now).Select(kv => kv.Key).ToList())
            _nonces.Remove(key);

        foreach (var key in _ips.Where(kv => kv.Value.LastSeen < now.AddHours(-6)
                                            && (kv.Value.BannedUntil is null || kv.Value.BannedUntil < now))
                     .Select(kv => kv.Key).ToList())
            _ips.Remove(key);
    }

    //请求级解析后的真实来源 IP（中间件在最前面写入）
    public const string ClientIpKey = "ai_client_ip";

    //取来源 IP：优先用中间件解析好的结果（可信代理场景下是真实用户 IP），否则取 TCP 对端
    public static string ClientIp(HttpContext context)
    {
        if (context.Items[ClientIpKey] is string resolved && resolved.Length > 0) return resolved;
        return PeerIp(context);
    }

    //TCP 对端地址（不可伪造）
    public static string PeerIp(HttpContext context)
        => context.Connection.RemoteIpAddress is { } addr ? addr.MapToIPv4().ToString() : "unknown";

    //解析来源 IP：只有「TCP 对端就是可信代理」时才采信 X-Client-Ip，否则一律用对端地址
    public static string ResolveClientIp(HttpContext context, IReadOnlyCollection<string> trustedProxies)
    {
        var peer = PeerIp(context);
        if (trustedProxies.Count == 0 || !trustedProxies.Contains(peer)) return peer;

        var raw = context.Request.Headers["X-Client-Ip"].ToString().Trim();
        if (raw.Length == 0) return peer;

        //严格校验：只接受单个字面 IP，逗号串 / CIDR / 任何垃圾一律丢弃
        if (raw.Contains(',') || raw.Contains('/') || !IPAddress.TryParse(raw, out var reported)) return peer;

        //拒绝回环地址：网关与浏览器跑在同一台机器时，它报上来的 client_ip 会是 127.0.0.1，
        //若采信会把所有用户压成同一个地址（比直接用对端 IP 更糟），因此这种情况回退到对端
        if (IPAddress.IsLoopback(reported)) return peer;

        return reported.MapToIPv4().ToString();
    }

}
