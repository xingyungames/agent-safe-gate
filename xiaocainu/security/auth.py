"""
M8 · 认证模块（纯 stdlib，零第三方依赖）
========================================
安全设计（答辩要点）：
- 密码：PBKDF2-HMAC-SHA256，每用户独立随机 16 字节盐、100k 迭代 → 防彩虹表/拖库；
  校验用 hmac.compare_digest 恒时比较 → 防时序侧信道。
- Token：`base64url(payload).b64url(HMAC-SHA256)`，payload 含 exp（≤2h）与 jti（唯一 ID）；
  服务端以 AUTH_SECRET 验签 → 防伪造；登出用内存 jti 黑名单（TokenRevoker）补齐。
- 限流：登录失败限流复用 security/guard.py 的 RateLimiter（5 次/300 秒，按 用户名|IP）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

PBKDF2_ITERATIONS = 100_000
DEFAULT_TTL = 7200  # token 有效期（秒），≤2h


# ---------------------------------------------------------- 密码哈希
def hash_password(password: str, salt=None, iterations: int = PBKDF2_ITERATIONS):
    """生成 (盐hex, 哈希hex)。salt 缺省随机 16 字节；传入 hex 字符串则复用（重置密码保盐可用）。"""
    if salt is None:
        salt = secrets.token_bytes(16)
    elif isinstance(salt, str):
        salt = bytes.fromhex(salt)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str,
                    iterations: int = PBKDF2_ITERATIONS) -> bool:
    """恒时比较校验密码；任何格式异常一律 False（不抛异常，防探测）。"""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    if not salt or not expected:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------- HMAC 签名 token
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(claims: dict, secret: str, ttl: int = DEFAULT_TTL) -> str:
    """签发 token：payload(含 iat/exp/jti) b64url + HMAC-SHA256 签名。"""
    now = int(time.time())
    payload = dict(claims)
    payload.setdefault("iat", now)
    payload.setdefault("exp", now + int(ttl))
    payload.setdefault("jti", secrets.token_hex(8))
    body = _b64url(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str, secret: str, now=None, revoked=None):
    """验签 + 过期 + 撤销检查。合法返回 claims dict；任何异常返回 None（不抛）。

    revoked 可传 set[str] 或 TokenRevoker 实例。
    """
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected_sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                            hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        payload = json.loads(_b64url_decode(body))
        if not isinstance(payload, dict):
            return None
    except Exception:
        return None
    current = int(time.time()) if now is None else int(now)
    exp = payload.get("exp")
    if not isinstance(exp, int) or current >= exp:
        return None
    jti = payload.get("jti", "")
    if revoked is not None and jti:
        if hasattr(revoked, "is_revoked"):
            if revoked.is_revoked(jti):
                return None
        elif jti in revoked:
            return None
    return payload


class TokenRevoker:
    """内存 jti 黑名单（logout 用）。进程重启即清空——token TTL≤2h 兜底，
    答辩话术：「重启即全员重新登录，符合最小会话原则」。"""

    def __init__(self) -> None:
        self._revoked: set = set()

    def revoke(self, jti: str) -> None:
        if jti:
            self._revoked.add(jti)

    def is_revoked(self, jti: str) -> bool:
        return jti in self._revoked
