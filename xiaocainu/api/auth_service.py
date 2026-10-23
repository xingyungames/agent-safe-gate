#认证运行时装配层 账号与权限唯一来源是主服务器 C#
from __future__ import annotations

import hashlib
import os
import secrets
import time

from models.actor import Actor, build_actor
from security.guard import RateLimiter
from . import backend_client

#身份缓存上限 秒 与 C# 令牌有效期对齐
_ACTOR_TTL_MAX = 7200
#失效令牌负缓存 秒 避免拿废令牌反复打主服务器累积攻击分
_NEGATIVE_TTL = 30


#令牌摘要 用作缓存键 不把裸令牌留在内存键里
def _digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


#从令牌载荷取过期时间
def _exp_of(token: str, default_ttl: int = _ACTOR_TTL_MAX) -> int:
    try:
        exp = int(backend_client.payload_of(token).get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    return exp if exp > 0 else int(time.time()) + default_ttl


class AuthService:
    #认证服务 不再签发本地令牌 只校验主服务器令牌

    def __init__(self, ttl: int = _ACTOR_TTL_MAX, login_limiter: RateLimiter | None = None) -> None:
        self.ttl = int(ttl or _ACTOR_TTL_MAX)
        self.login_limiter = login_limiter or RateLimiter(limit=5, window=300)
        self._actors: dict = {}   #令牌摘要 -> (Actor, 过期时刻)
        self._dead: dict = {}     #令牌摘要 -> 负缓存过期时刻
        self.sid_bindings: dict = {}  #sid -> 用户标识 内存级 重启即清

    #登录：口令判定完全交给主服务器
    def login(self, username: str, password: str, client_ip: str = ""):
        username = (username or "").strip()
        key = f"{username}|{client_ip}"
        if not self.login_limiter.would_allow(key):
            return 429, {"error": "登录尝试过于频繁，请几分钟后再试"}

        try:
            data = backend_client.login(username, password or "", client_ip=client_ip)
        except (backend_client.BackendRejected, backend_client.BackendUnauthorized) as e:
            #口令错/账号不可用/限流都属于业务结论 原样回给用户
            self.login_limiter.allow(key)  #仅失败计数
            return (e.status or 401), {"error": str(e) or "用户名或口令错误"}
        except backend_client.BackendError as e:
            return 503, {"error": f"主服务器暂时不可用，无法登录：{e}"}

        token = str(data.get("token") or "")
        if not token:
            return 503, {"error": "主服务器登录响应缺少令牌"}
        actor = build_actor(data.get("user") or {},
                            token=token,
                            sign_key=str(data.get("signKey") or ""),
                            expires_at=_exp_of(token, self.ttl))
        if not actor.username:
            return 503, {"error": "主服务器登录响应缺少账号信息"}

        self._remember(actor)
        return 200, {
            "token": token,
            "session_id": secrets.token_hex(8),
            "user": actor.public(),
            "perms": sorted(actor.permissions),
            "expires_at": actor.expires_at,
        }

    #校验浏览器带来的主服务器令牌 返回 Actor 或 None
    def authenticate(self, token: str) -> Actor | None:
        token = (token or "").strip()
        if not token:
            return None

        key = _digest(token)
        now = time.time()

        hit = self._actors.get(key)
        if hit:
            if hit[1] > now:
                return hit[0]
            self._actors.pop(key, None)

        dead_until = self._dead.get(key)
        if dead_until and dead_until > now:
            return None

        #本地先判格式与过期 不拿废令牌去打主服务器
        payload = backend_client.payload_of(token)
        if not payload.get("exp"):
            #不是主服务器签发的令牌格式 直接判无效
            self._dead[key] = now + _NEGATIVE_TTL
            return None
        if backend_client.token_expired(token):
            self._dead[key] = now + _NEGATIVE_TTL
            return None

        try:
            data = backend_client.me(token)
        except backend_client.BackendUnauthorized:
            self._dead[key] = now + _NEGATIVE_TTL
            return None
        except backend_client.BackendError:
            raise  #主服务器故障交给上层变 503 不伪装成未登录

        actor = build_actor(data, permissions=data.get("permissions"), token=token,
                            sign_key=backend_client.sign_key_of(token),
                            expires_at=_exp_of(token, self.ttl))
        if not actor.username:
            self._dead[key] = now + _NEGATIVE_TTL
            return None

        self._remember(actor)
        return actor

    #缓存身份 到期时间取令牌 exp 与上限的较小值
    def _remember(self, actor: Actor) -> None:
        now = time.time()
        ttl = min(actor.expires_at - now, self.ttl)
        if ttl <= 0:
            return
        self._actors[_digest(actor.token)] = (actor, now + ttl)
        self._dead.pop(_digest(actor.token), None)

    #登出：主服务器不做吊销 这里只清本网关缓存
    def logout(self, token: str) -> bool:
        key = _digest(token)
        hit = self._actors.pop(key, None)
        self._dead.pop(key, None)
        if hit:
            self.sid_bindings = {s: u for s, u in self.sid_bindings.items()
                                 if u != hit[0].user_id}
        return hit is not None

    #sid 首次出现即绑定用户 换用户即拒绝
    def bind_sid(self, sid: str, user_key: str):
        sid = (sid or "default")[:64]
        bound = self.sid_bindings.get(sid)
        if bound is None:
            self.sid_bindings[sid] = user_key
            return True, ""
        if bound != user_key:
            return False, "该会话已绑定其他用户，请刷新页面获取新会话"
        return True, ""


#按 .env 装配认证服务
def build_auth():
    if not backend_client.enabled():
        print("[M8][安全] 未配置 BACKEND_BASE_URL，认证墙处于关闭状态——"
              "所有受保护端点与登录接口将返回 503。请在 .env 填写主服务器地址。")
        return None
    ttl = int(os.getenv("AUTH_TOKEN_TTL", str(_ACTOR_TTL_MAX)))
    limit = int(os.getenv("AUTH_LOGIN_LIMIT", "5"))
    window = int(os.getenv("AUTH_LOGIN_WINDOW", "300"))
    print(f"[M8] 认证已启用：账号权威={backend_client.base_url()} "
          f"身份缓存上限={ttl}s 登录限流={limit}次/{window}s（仅失败计数）")
    return AuthService(ttl=ttl, login_limiter=RateLimiter(limit=limit, window=window))
