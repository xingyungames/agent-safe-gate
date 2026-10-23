#主服务器适配器 无状态：令牌由浏览器持有 这里只负责转发与签名
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple


#主服务器错误基类 带状态码与业务码 供网关精确映射
class BackendError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = int(status or 0)
        self.code = code or ""


#连不上或主服务器自身故障
class BackendUnavailable(BackendError):
    pass


#主服务器明确拒绝本次请求（业务结论，不许绕过）
class BackendRejected(BackendError):
    pass


#令牌无效或已过期 需要重新登录
class BackendUnauthorized(BackendError):
    pass


#主服务器地址 为空即未启用
def base_url() -> str:
    return (os.getenv("BACKEND_BASE_URL", "") or "").strip().rstrip("/")


#是否已配置主服务器
def enabled() -> bool:
    return bool(base_url())


def _timeout() -> int:
    try:
        return max(1, int(os.getenv("BACKEND_TIMEOUT", "20")))
    except ValueError:
        return 20


#按约定生成 HMAC 签名头 每次调用都换新的时间戳与随机数
def _sign(method: str, path_with_query: str, body_text: str, sign_key: str) -> dict:
    ts = str(int(time.time() * 1000))
    nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:36]
    body_hash = hashlib.sha256((body_text or "").encode("utf-8")).hexdigest()
    canonical = "\n".join([method.upper(), path_with_query, ts, nonce, body_hash])
    sig = hmac.new(sign_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    return {
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": base64.b64encode(sig).decode("ascii"),
    }


#发一次请求 返回状态与 JSON
def _request(method: str, path: str, *, body: Optional[dict] = None,
             token: str = "", sign_key: str = "",
             extra_headers: Optional[dict] = None) -> Tuple[int, dict]:
    if not enabled():
        raise BackendUnavailable("未配置 BACKEND_BASE_URL（主服务器地址）")
    url = base_url() + path
    body_text = json.dumps(body, ensure_ascii=False) if body is not None else None
    headers = {"Accept": "application/json"}
    if body_text is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = "Bearer " + token
    #调用方自定义头（如 X-Client-Ip）
    if extra_headers:
        for k, v in extra_headers.items():
            if v:
                headers[k] = v
    #只有非 GET 且拿到签名密钥时才签名
    if sign_key and method.upper() != "GET":
        headers.update(_sign(method, path, body_text or "", sign_key))

    data = body_text.encode("utf-8") if body_text is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8")
        except Exception:
            pass
        status = e.code
    except Exception as e:  #URLError 超时 连接被拒
        raise BackendUnavailable(f"无法连接主服务器 {base_url()}：{type(e).__name__}: {e}") from e

    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        raise BackendUnavailable(f"主服务器返回非 JSON（HTTP {status}）") from None
    return status, payload


#从响应里取错误说明
def _error_message(status: int, payload: dict) -> str:
    msg = payload.get("message") or payload.get("error") or ""
    code = payload.get("code") or ""
    if msg:
        return f"{msg}（HTTP {status}{'，' + str(code) if code else ''}）"
    return f"主服务器返回 HTTP {status}"


#按状态码与业务码分类抛出
def _raise_for(status: int, payload: dict):
    code = str(payload.get("code") or "")
    message = _error_message(status, payload)
    if status == 401:
        raise BackendUnauthorized(message, status=status, code=code)
    #来源 IP 被安全策略封禁属于基础设施故障 不是用户操作错误
    if status == 403 and code == "IP_BANNED":
        raise BackendUnavailable("主服务器安全策略拦截了本机来源地址：" + message,
                                 status=status, code=code)
    if 400 <= status < 500:
        raise BackendRejected(message, status=status, code=code)
    raise BackendUnavailable(message, status=status, code=code)


#200 且 ok 才返回 data 其余一律抛
def _unwrap(status: int, payload: dict) -> dict:
    if status == 200 and payload.get("ok"):
        return payload.get("data") or {}
    _raise_for(status, payload)
    return {}


#解出 JWT 载荷 只用于取 sk 与 exp 不做验签
def payload_of(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body.encode()).decode("utf-8"))
    except Exception:
        return {}


#从令牌载荷取请求签名密钥
def sign_key_of(token: str) -> str:
    return str(payload_of(token).get("sk") or "")


#令牌是否已在本地可判定为过期
def token_expired(token: str, skew: int = 5) -> bool:
    exp = payload_of(token).get("exp")
    try:
        return int(exp) <= int(time.time()) + skew
    except (TypeError, ValueError):
        return False


#登录换令牌 返回主服务器原始 data
def login(username: str, password: str, client_ip: str = "") -> dict:
    #带上真实客户端 IP 供主服务器按 (账号,IP) 做失败计数 避免被拿来锁别人账号
    ip = _clean_ip(client_ip)
    status, payload = _request(
        "POST", "/api/auth/login",
        body={"userName": username, "password": password},
        extra_headers={"X-Client-Ip": ip} if ip else None,
    )
    return _unwrap(status, payload)


#只放行单个合法 IP 版本 防止逗号串或超长内容被带出去
def _clean_ip(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return ""


#查当前令牌对应身份
def me(token: str) -> dict:
    status, payload = _request("GET", "/api/auth/me", token=token)
    return _unwrap(status, payload)


#日期归一成 YYYY-MM-DD
def _normalize_date(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    for sep in ("/", "-", ".", "年"):
        if sep in s:
            parts = [p for p in s.replace("年", " ").replace("月", " ").replace("日", " ")
                     .replace(sep, " ").split() if p]
            if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
                y, m, d = parts[0], parts[1], parts[2]
                if len(y) == 4:
                    return f"{y}-{int(m):02d}-{int(d):02d}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return ""


#提交报销单 身份由主服务器按令牌盖章
def submit_with_token(token: str, sign_key: str, *, department: str, rtype: str, amount,
                      reason: str = "", has_invoice: str = "", city: str = "",
                      date_str: str = "", invoice_info: str = "") -> dict:
    body = {
        "department": department,
        "rtype": rtype,
        "amount": float(amount or 0),
        "reason": reason or "",
        "hasInvoice": has_invoice or "",
        "city": city or "",
        "invoiceDate": _normalize_date(date_str),
        "invoiceInfo": invoice_info or "",
    }
    status, payload = _request("POST", "/api/invoice/submit", body=body,
                               token=token, sign_key=sign_key)
    data = _unwrap(status, payload)
    return {
        "record_id": data.get("recordId") or "",
        "bill_no": data.get("billNo") or "",
        "message": data.get("message") or "已提交",
    }


#查报销单列表 范围由主服务器按令牌角色裁剪
def list_invoices(token: str, sign_key: str = "", limit: int = 500) -> dict:
    status, payload = _request("GET", f"/api/invoice/list?limit={int(limit)}",
                               token=token, sign_key=sign_key)
    return _unwrap(status, payload)


#审核报销单 仅财务及以上
def review_with_token(token: str, sign_key: str, *, record_id: str, status_text: str,
                      opinion: str) -> dict:
    status, payload = _request("POST", "/api/invoice/review", body={
        "recordId": record_id, "status": status_text, "opinion": opinion,
    }, token=token, sign_key=sign_key)
    return _unwrap(status, payload)


#取枚举字典与判定口径
def meta_with_token(token: str) -> dict:
    status, payload = _request("GET", "/api/invoice/meta", token=token)
    return _unwrap(status, payload)


#健康检查 无需登录
def health() -> dict:
    status, payload = _request("GET", "/api/public/health")
    return _unwrap(status, payload)
