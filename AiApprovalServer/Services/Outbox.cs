using System.Net;
using System.Net.Mail;
using AiApproval.Core;
using AiApproval.Security;

namespace AiApproval.Services;

/// <summary>
/// 出站通知箱。
///
/// 本环境无法依赖真实邮件服务，所以采取"双通道"设计：
///   * 永远先落库（outbox.json）→ 后台可查看、可补发、可撤回；
///   * 若配置了 SMTP（Mail.Enabled=true）才真正发信，失败也不影响主流程（只标记 FAILED）。
///
/// 这也让"补偿事务"有落点：通知发出去之后可以撤回（RECALLED），而不是只写一句"已发送"。
/// </summary>
public sealed class Outbox
{
    private readonly JsonStore _store;
    private readonly AppConfig _cfg;
    private readonly AuditLog _audit;
    private readonly ILogger<Outbox> _logger;

    public Outbox(JsonStore store, AppConfig cfg, AuditLog audit, ILogger<Outbox> logger)
    {
        _store = store;
        _cfg = cfg;
        _audit = audit;
        _logger = logger;
    }

    public async Task<OutboxMessage> EnqueueAsync(string to, string subject, string body, string taskId,
        string ownerUserId, string ownerUserName, string approvalLink)
    {
        var message = new OutboxMessage
        {
            Id = "n_" + Crypto.RandomHex(6),
            CreatedAt = DateTime.UtcNow,
            To = to,
            Subject = subject,
            Body = body,
            TaskId = taskId,
            OwnerUserId = ownerUserId,
            OwnerUserName = ownerUserName,
            ApprovalLink = approvalLink,
            Status = "QUEUED",
        };

        await _store.MutateAsync<OutboxMessage, bool>(StoreNames.Outbox, list =>
        {
            list.Add(message);
            return true;
        }).ConfigureAwait(false);

        if (_cfg.Mail.Enabled && !string.IsNullOrWhiteSpace(_cfg.Mail.Host) && !string.IsNullOrWhiteSpace(to))
        {
            try
            {
                using var client = new SmtpClient(_cfg.Mail.Host, _cfg.Mail.Port)
                {
                    EnableSsl = _cfg.Mail.UseSsl,
                    DeliveryMethod = SmtpDeliveryMethod.Network,
                };

                if (!string.IsNullOrWhiteSpace(_cfg.Mail.Sender) && !string.IsNullOrWhiteSpace(_cfg.Mail.Password))
                    client.Credentials = new NetworkCredential(_cfg.Mail.Sender, _cfg.Mail.Password);

                using var mail = new MailMessage(_cfg.Mail.Sender, to, subject, body) { IsBodyHtml = false };
                await client.SendMailAsync(mail).ConfigureAwait(false);

                await MarkAsync(message.Id, "SENT", "").ConfigureAwait(false);
                await _audit.WriteAsync("system", "system", "system", "", "notify.sent", message.Id, "SUCCESS",
                    $"收件人 {Crypto.Mask(to)}").ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                await MarkAsync(message.Id, "FAILED", ex.Message).ConfigureAwait(false);
                _logger.LogWarning("邮件发送失败（已保留在出站箱）：{Message}", ex.Message);
            }
        }
        else
        {
            await _audit.WriteAsync("system", "system", "system", "", "notify.queued", message.Id, "QUEUED",
                $"收件人 {Crypto.Mask(to)}（SMTP 未启用，已写入出站箱）").ConfigureAwait(false);
        }

        return message;
    }

    public async Task<List<OutboxMessage>> ListAsync(string? ownerUserId = null, int limit = 100)
    {
        var all = await _store.ReadListAsync<OutboxMessage>(StoreNames.Outbox).ConfigureAwait(false);
        IEnumerable<OutboxMessage> query = all.OrderByDescending(m => m.CreatedAt);
        if (!string.IsNullOrWhiteSpace(ownerUserId))
            query = query.Where(m => m.OwnerUserId == ownerUserId);
        return query.Take(Math.Clamp(limit, 1, 300)).ToList();
    }

    public async Task<OutboxMessage?> GetAsync(string id)
    {
        var all = await _store.ReadListAsync<OutboxMessage>(StoreNames.Outbox).ConfigureAwait(false);
        return all.FirstOrDefault(m => m.Id == id);
    }

    public async Task<bool> MarkAsync(string id, string status, string error)
        => await _store.MutateAsync<OutboxMessage, bool>(StoreNames.Outbox, list =>
        {
            var target = list.FirstOrDefault(m => m.Id == id);
            if (target is null) return false;
            target.Status = status;
            target.Error = error.Length > 400 ? error[..400] : error;
            if (status == "SENT") target.SentAt = DateTime.UtcNow;
            return true;
        }).ConfigureAwait(false);

    /// <summary>补偿事务：撤回已发出的通知（反操作）。</summary>
    public async Task<bool> RecallAsync(string id, string actor)
    {
        var ok = await _store.MutateAsync<OutboxMessage, bool>(StoreNames.Outbox, list =>
        {
            var target = list.FirstOrDefault(m => m.Id == id);
            if (target is null || target.Status == "RECALLED") return false;
            target.Status = "RECALLED";
            target.CompensatedAt = DateTime.UtcNow;
            return true;
        }).ConfigureAwait(false);

        if (ok)
            await _audit.WriteAsync(actor, actor, Roles.Admin, "", "notify.recall", id, "SUCCESS",
                "执行补偿事务：撤回通知").ConfigureAwait(false);

        return ok;
    }
}
