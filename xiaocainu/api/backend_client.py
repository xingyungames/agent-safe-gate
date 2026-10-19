# 主服务器适配器 报销单写入口
# 纯标准库（urllib），不引入任何第三方依赖。
# 职责：把网关的报销单写操作转发到主服务器 AiApprovalServer（账号权威、唯一写入口、
# 服务端盖章、快照回退、审计哈希链都在主服务器侧完成）。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

# 令牌提前 60 秒重换，避免临界过期
_TOKEN_MARGIN = 60


# ---------------------------------------------------------- 错误类型
class BackendError(Exception):
    """主服务器调用失败（基类）。"""


class BackendUnavailable(BackendError):
    """连不上主服务器（基础设施故障：未启动 / 超时 / 连接被拒）。"""


class BackendRejected(BackendError):
    """主服务器明确拒绝本次写入（业务规则：口令错 / 枚举非法 / 金额越界 / 权限不足）。

    status 携带主服务器返回的 HTTP 状态码（4xx），便于网关按原样映射
    （400 业务参数错 / 403 无权限）回前端。
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class BackendSessionMissing(BackendUnavailable):
    """该账号从未建立过主服务器会话（没有缓存令牌），重登也无效。"""


# ---------------------------------------------------------- 配置
def mode() -> str:
    """后端模式：off / preferred / required。"""
    if not base_url():
        return "off"
    raw = (os.getenv("BACKEND_MODE", "preferred") or "preferred").strip().lower()
    return raw if raw in ("off", "preferred", "required") else "preferred"


def base_url() -> str:
    """主服务器地址（不带末尾斜杠）。"""
    return (os.getenv("BACKEND_BASE_URL", "") or "").strip().rstrip("/")


def enabled() -> bool:
    """是否启用主服务器（写入口是否收口到主服务器）。"""
    return mode() != "off"


def _timeout() -> int:
    try:
        return max(1, int(os.getenv("BACKEND_TIMEOUT", "15")))
    except ValueError:
        return 15


# ---------------------------------------------------------- 签名
def _sign(method: str, path_with_query: str, body_text: str, sign_key: str) -> Dict[str, str]:
    """按约定生成 HMAC 签名头（与 AiApprovalServer 校验算法一致）。"""
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


# 禁用系统代理：本机开代理时，127.0.0.1 的请求会被代理接管，
# 把"连接被拒"变成代理的 502 页面，甚至把带令牌的请求交给第三方代理。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(method: str, path: str, *, body: Optional[dict] = None,
             token: str = "", sign_key: str = "") -> Tuple[int, dict]:
    """发一次请求，返回 (HTTP 状态, JSON payload)。"""
    url = base_url() + path
    body_text = json.dumps(body, ensure_ascii=False) if body is not None else None
    headers = {"Accept": "application/json"}
    if body_text is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = "Bearer " + token
    if sign_key and method.upper() != "GET":
        headers.update(_sign(method, path, body_text or "", sign_key))

    data = body_text.encode("utf-8") if body_text is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with _opener.open(req, timeout=_timeout()) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8")
        except Exception:
            pass
        status = e.code
    except Exception as e:  # URLError / 超时 / 连接被拒
        raise BackendUnavailable(
            f"无法连接主服务器 {base_url()}：{type(e).__name__}: {e}") from e

    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        raise BackendError(f"主服务器返回非 JSON（HTTP {status}）") from None
    return status, payload


def _error_message(status: int, payload: dict) -> str:
    """从响应里取人类可读的错误说明。"""
    msg = payload.get("message") or payload.get("error") or ""
    code = payload.get("code") or ""
    if msg:
        return f"{msg}（HTTP {status}{'，' + str(code) if code else ''}）"
    return f"主服务器返回 HTTP {status}"


# ---------------------------------------------------------- 令牌缓存
_lock = threading.Lock()
_tokens: Dict[str, Tuple[str, str, float]] = {}   # 账号 -> (令牌, 签名密钥, 过期时间)


def _cache_token(username: str, token: str, sign_key: str, ttl: int = 7200) -> None:
    with _lock:
        _tokens[username] = (token, sign_key, time.time() + max(60, ttl) - _TOKEN_MARGIN)


def _cached(username: str) -> Optional[Tuple[str, str]]:
    with _lock:
        item = _tokens.get(username)
    if not item:
        return None
    token, sign_key, exp = item
    if time.time() >= exp:
        with _lock:
            _tokens.pop(username, None)
        return None
    return token, sign_key


def login(username: str, password: str) -> dict:
    """登录换令牌并缓存；返回主服务器下发的 user 资料。"""
    status, payload = _request("POST", "/api/auth/login",
                               body={"userName": username, "password": password})
    if status != 200 or not payload.get("ok"):
        message = _error_message(status, payload)
        # 4xx 是主服务器明确拒绝：业务结论，调用方不许回落到本地表
        if 400 <= status < 500:
            raise BackendRejected(message)
        raise BackendError(message)
    data = payload.get("data") or {}
    token = data.get("token") or ""
    sign_key = data.get("signKey") or ""
    if not token or not sign_key:
        raise BackendError("主服务器登录响应缺少 token/signKey")
    _cache_token(username, token, sign_key, int(data.get("expiresIn") or 7200))
    return data.get("user") or {}


def ensure_login(username: str, password: str = "") -> Tuple[str, str]:
    """取可用令牌；没有就重登。没有口令且未建立会话时，抛 BackendSessionMissing。"""
    cached = _cached(username)
    if cached:
        return cached
    if not password:
        raise BackendSessionMissing("主服务器会话未建立，请先启动主服务器并重新登录")
    login(username, password)
    again = _cached(username)
    if not again:
        raise BackendSessionMissing("主服务器会话未建立，请先启动主服务器并重新登录")
    return again


def _authed_call(method: str, path: str, body: Optional[dict], username: str, password: str) -> dict:
    """带令牌调用；过期重登一次；业务拒绝/基础设施故障按口径上抛。"""
    token, sign_key = ensure_login(username, password)

    def once(tok: str, key: str) -> Tuple[int, dict]:
        return _request(method, path, body=body, token=tok, sign_key=key)

    status, payload = once(token, sign_key)
    if status == 401:                      # 令牌过期：清缓存重登一次
        with _lock:
            _tokens.pop(username, None)
        token, sign_key = ensure_login(username, password)
        status, payload = once(token, sign_key)

    if status != 200 or not payload.get("ok"):
        if 400 <= status < 500:
            raise BackendRejected(_error_message(status, payload), status)
        raise BackendError(_error_message(status, payload))
    return payload.get("data") or {}


# ---------------------------------------------------------- 日期归一
def _normalize_date(value: str) -> str:
    """日期归一成 YYYY-MM-DD（主服务器自己转毫秒写飞书）。"""
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


# ---------------------------------------------------------- 提交（员工填单）
def submit_as(username: str, *, department: str, rtype: str, amount,
              reason: str = "", has_invoice: str = "", city: str = "",
              date_str: str = "", invoice_info: str = "") -> dict:
    """用已登录会话提交（只持缓存令牌，不持口令）。"""
    if not username:
        raise BackendError("缺少登录用户名，无法向主服务器提交")
    if not _cached(username):
        raise BackendSessionMissing("主服务器会话已过期，请重新登录后再提交")
    return _submit_authed(username, department=department, rtype=rtype, amount=amount,
                          reason=reason, has_invoice=has_invoice, city=city,
                          date_str=date_str, invoice_info=invoice_info)


def _submit_authed(username: str, *, department: str, rtype: str, amount,
                   reason: str = "", has_invoice: str = "", city: str = "",
                   date_str: str = "", invoice_info: str = "") -> dict:
    """组装字段并提交报销单。"""
    token, sign_key = _cached(username) or ("", "")
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
    status, payload = _request("POST", "/api/invoice/submit", body=body, token=token, sign_key=sign_key)
    if status == 401:
        with _lock:
            _tokens.pop(username, None)
        raise BackendSessionMissing("主服务器会话已过期，请重新登录后再提交")
    if status != 200 or not payload.get("ok"):
        if 400 <= status < 500:
            # 业务拒绝：绝不绕过写入口
            raise BackendRejected(_error_message(status, payload), status)
        raise BackendError(_error_message(status, payload))
    data = payload.get("data") or {}
    return {
        "record_id": data.get("recordId") or "",
        "bill_no": data.get("billNo") or "",
        "message": data.get("message") or "已提交",
    }


def submit(username: str, password: str, *, department: str, rtype: str, amount,
           reason: str = "", has_invoice: str = "", city: str = "",
           date_str: str = "", invoice_info: str = "") -> dict:
    """提交一张报销单（带口令版本，登录后转发）。"""
    ensure_login(username, password)
    return _submit_authed(username, department=department, rtype=rtype, amount=amount,
                          reason=reason, has_invoice=has_invoice, city=city,
                          date_str=date_str, invoice_info=invoice_info)


def my_invoices(username: str, password: str, limit: int = 100) -> list:
    """查我的报销单。"""
    data = _authed_call("GET", f"/api/invoice/list?limit={int(limit)}", None, username, password)
    return data.get("rows") or []


# ---------------------------------------------------------- 财务审核页 _as 系列
# 用缓存令牌调用，不需要口令（财务端点被调用时网关手里只有会话 token）。
def meta_as(username: str) -> dict:
    """取契约元数据（枚举字典、能力位、规则）。"""
    return _authed_call("GET", "/api/invoice/meta", None, username, "")


def list_as(username: str, limit: int = 500) -> dict:
    """取单据列表（全部）。"""
    return _authed_call("GET", f"/api/invoice/list?limit={int(limit)}", None, username, "")


def detail_as(username: str, record_id: str) -> dict:
    """取单据详情。"""
    return _authed_call("GET", f"/api/invoice/detail?recordId={urllib.parse.quote(record_id)}",
                        None, username, "")


def review_as(username: str, *, record_id: str, status: str, opinion: str) -> dict:
    """审核并写回主服务器（盖章/状态机/快照/审计都在主服务器侧）。"""
    return _authed_call("POST", "/api/invoice/review",
                        {"recordId": record_id, "status": status, "opinion": opinion}, username, "")


def update_as(username: str, *, record_id: str, fields: dict) -> dict:
    """修改字段并写回主服务器（fields 为中文列名->字符串的字典）。"""
    return _authed_call("POST", "/api/invoice/update",
                        {"recordId": record_id, "fields": fields}, username, "")


def delete_as(username: str, *, record_id: str, reason: str) -> dict:
    """删除单据并写回主服务器。"""
    return _authed_call("POST", "/api/invoice/delete",
                        {"recordId": record_id, "reason": reason}, username, "")


# ---------------------------------------------------------- 兼容旧调用（带口令）
def review(username: str, password: str, *, record_id: str, status: str, opinion: str) -> dict:
    return _authed_call("POST", "/api/invoice/review",
                        {"recordId": record_id, "status": status, "opinion": opinion}, username, password)


def meta(username: str, password: str) -> dict:
    return _authed_call("GET", "/api/invoice/meta", None, username, password)


def health() -> dict:
    """主服务器健康检查（无需登录）。"""
    _, payload = _request("GET", "/api/public/health")
    return payload.get("data") or {}
