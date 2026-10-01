using AiApproval.Core;
using AiApproval.Security;

namespace AiApproval.Api;

/// <summary>API 统一响应与请求辅助。所有响应都走同一套序列化约定，避免字段风格漂移。</summary>
public static class Api
{
    public static IResult Ok(object? data = null)
        => Results.Json(new { ok = true, data }, AppJson.Wire);

    public static IResult Error(int status, string code, string message)
        => Results.Json(new { ok = false, code, message }, AppJson.Wire, statusCode: status);

    public static RequestIdentity? Identity(HttpContext context)
        => context.Items[SecurityMiddleware.IdentityKey] as RequestIdentity;

    public static string Ip(HttpContext context) => SecurityGuard.ClientIp(context);

    /// <summary>从中间件已缓冲的请求体里反序列化（避免重复读流）。</summary>
    public static T? ReadBody<T>(HttpContext context) where T : class
    {
        if (context.Items[SecurityMiddleware.RawBodyKey] is byte[] bytes && bytes.Length > 0)
            return AppJson.Deserialize<T>(System.Text.Encoding.UTF8.GetString(bytes));

        return null;
    }

    /// <summary>统一的"读参数"防御：限制长度、去掉控制字符。</summary>
    public static string Clean(string? value, int maxLength)
    {
        if (string.IsNullOrEmpty(value)) return "";
        var trimmed = value.Trim();
        var cleaned = new string(trimmed.Where(c => !char.IsControl(c) || c == '\n' || c == '\t').ToArray());
        return cleaned.Length <= maxLength ? cleaned : cleaned[..maxLength];
    }
}
