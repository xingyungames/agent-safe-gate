using System.Collections.Concurrent;

namespace AiApproval.Services;

public sealed class ChatMessage
{
    public string Role { get; set; } = "user";
    public string Content { get; set; } = "";
}

public sealed class ChatSession
{
    public string Id { get; set; } = "";
    public string UserId { get; set; } = "";
    public string Role { get; set; } = "";
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public DateTime LastUsed { get; set; } = DateTime.UtcNow;
    public List<ChatMessage> Messages { get; set; } = new();
    public string LastPlanJson { get; set; } = "";
}

/// <summary>
/// 会话记忆（进程内）。
///
/// 为什么要按用户隔离：如果把所有用户的对话塞进同一个上下文，
/// 别人的输入就会变成你的"提示词注入载体"（跨用户污染）。
/// 因此这里以 userId 为键，并且每个会话只有 20 条上限，超限丢弃最早的对话。
/// </summary>
public sealed class ChatSessionStore
{
    private const int MaxMessagesPerSession = 20;
    private const int MaxSessions = 500;

    private readonly ConcurrentDictionary<string, ChatSession> _sessions = new(StringComparer.Ordinal);

    public ChatSession GetOrCreate(string userId, string role, string? sessionId)
    {
        Evict();

        var key = string.IsNullOrWhiteSpace(sessionId) ? userId + "|default" : userId + "|" + Sanitize(sessionId);
        var session = _sessions.GetOrAdd(key, _ => new ChatSession
        {
            Id = string.IsNullOrWhiteSpace(sessionId) ? "s_" + Security.Crypto.RandomHex(6) : Sanitize(sessionId),
            UserId = userId,
            Role = role,
        });

        // 用户被改了角色 → 上下文也必须换一套（否则旧上下文里可能残留更高权限的工具说明）
        if (!string.Equals(session.Role, role, StringComparison.Ordinal))
        {
            session.Role = role;
            session.Messages.Clear();
        }

        session.LastUsed = DateTime.UtcNow;
        return session;
    }

    public void Append(ChatSession session, string role, string content)
    {
        lock (session)
        {
            session.Messages.Add(new ChatMessage { Role = role, Content = content });
            while (session.Messages.Count > MaxMessagesPerSession)
                session.Messages.RemoveAt(0);
        }
    }

    public List<ChatMessage> Snapshot(ChatSession session)
    {
        lock (session) return session.Messages.ToList();
    }

    public void Reset(string userId, string? sessionId)
    {
        var key = string.IsNullOrWhiteSpace(sessionId) ? userId + "|default" : userId + "|" + Sanitize(sessionId);
        _sessions.TryRemove(key, out _);
    }

    private void Evict()
    {
        if (_sessions.Count <= MaxSessions) return;
        foreach (var key in _sessions.OrderBy(kv => kv.Value.LastUsed).Take(_sessions.Count - MaxSessions).Select(kv => kv.Key).ToList())
            _sessions.TryRemove(key, out _);
    }

    private static string Sanitize(string value)
    {
        var cleaned = new string(value.Where(c => char.IsLetterOrDigit(c) || c is '_' or '-').ToArray());
        return cleaned.Length > 40 ? cleaned[..40] : cleaned;
    }
}
