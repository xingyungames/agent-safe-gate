"""
M8 · 认证运行时（装配层）
========================
把 security/auth.py（纯函数）+ models/users.py（飞书用户表）+ security/guard.py（限流）
装配成网关可用的 AuthService：login / verify / logout / 会话-用户绑定。

设计要点
---------
- 登录限流**只计失败次数**（would_allow 预检 + 失败才 hit），正常登录不消耗配额；
- 账号权威在主服务器 AiApprovalServer：登录先请主服务器判定口令，角色直接取主服务器
  返回的（employee/finance/admin/superadmin），二次映射为界面三标签（员工/财务/管理员）；
- token 校验用 AUTH_SECRET HMAC 验签，logout 走内存 jti 黑名单；
- sid 绑定：服务端不再盲信 body 里的 session_id——某 sid 首次出现即绑定到当前用户，
  后续同 sid 换用户 → 403（防会话串用/越权读取他人草稿与上下文）。
"""
from __future__ import annotations

import os
import secrets

from security.auth import TokenRevoker, issue_token, verify_token
from security.guard import RateLimiter
from models import users
from models.feishu import FeishuBitableClient
from . import backend_client

# 登录响应/前端展示用的用户字段（绝不含 密码哈希/盐）
PUBLIC_USER_KEYS = ("工号", "姓名", "用户名", "角色")

# 主服务器四角色 → 本网关三标签（superadmin 在界面上等同管理员）
ROLE_LABELS = {
    "employee": "员工",
    "finance": "财务",
    "admin": "管理员",
    "superadmin": "管理员",
}


class AuthService:
    def __init__(self, user_store: users.UserStore, secret: str, ttl: int = 7200,
                 login_limiter: RateLimiter | None = None) -> None:
        self.users = user_store
        self.secret = secret
        self.ttl = ttl
        self.login_limiter = login_limiter or RateLimiter(limit=5, window=300)
        self.revoker = TokenRevoker()
        self.sid_bindings: dict = {}  # sid -> username（内存级，重启即清）

    # -------------------------------------------------- 登录
    def login(self, username: str, password: str, client_ip: str = ""):
        """登录。返回 (http_status, payload)。失败只计失败次数的限流配额。

        账号权威在主服务器 AiApprovalServer：登录先请主服务器判定口令，成功后再用
        主服务器返回的账号资料（角色/姓名/工号）签发本网关会话；令牌缓存供后续
        写请求做 HMAC 签名复用。主服务器 4xx 直接失败、不回落本地表；连不上时按
        BACKEND_MODE 决定兜底（preferred 回本地表 / required 直接拒绝）。
        """
        username = (username or "").strip()
        key = f"{username}|{client_ip}"
        if not self.login_limiter.would_allow(key):
            return 429, {"error": "登录尝试过于频繁，请几分钟后再试"}

        # ① 主服务器优先（账号权威）
        if backend_client.enabled():
            try:
                user = backend_client.login(username, password)
            except backend_client.BackendRejected as e:
                # 口令错/账号停用/锁定/限流：业务结论，绝不回落本地表
                return 401, {"error": str(e)}
            except (backend_client.BackendUnavailable,
                    backend_client.BackendSessionMissing) as e:
                # 连不上/会话缺失：看模式
                if backend_client.mode() == "required":
                    return 503, {"error": f"主服务器不可达，已按 required 模式拒绝登录：{e}"}
                return self._login_local(username, password, key)
            # 成功：用主服务器下发的资料建 claims（角色以主服务器为准）
            role = ROLE_LABELS.get((user.get("role") or "employee"), "员工")
            claims = {
                "username": user.get("userName") or username,
                "name": user.get("displayName") or user.get("userName") or username,
                "role": role,
                "employee_id": user.get("employeeId") or "",
            }
            public = {
                "工号": claims["employee_id"],
                "姓名": claims["name"],
                "用户名": claims["username"],
                "角色": claims["role"],
            }
            return self._issue(username, claims, public,
                               f"主服务器已校验（角色={role}，工号={claims['employee_id']}）")

        # 未启用主服务器：纯本地用户表
        return self._login_local(username, password, key)

    def _login_local(self, username: str, password: str, key: str):
        """本地飞书用户表兜底登录（仅 preferred 模式且主服务器不可达时）。"""
        item, err = users.authenticate(self.users, username, password or "")
        if err:
            self.login_limiter.allow(key)  # 仅失败计数
            return 401, {"error": err}
        f = item["fields"]
        claims = {
            "username": f.get("用户名", ""),
            "name": f.get("姓名") or f.get("用户名", ""),
            "role": f.get("角色", ""),
            "employee_id": f.get("工号", ""),
        }
        public = {k: f.get(k, "") for k in PUBLIC_USER_KEYS}
        return self._issue(username, claims, public, "本地用户表校验（主服务器未启用或不可达）")

    def _issue(self, username: str, claims: dict, public: dict, note: str):
        """签发本地会话 token（登录成功统一出口；note 注明本次账号权威来源）。"""
        token = issue_token(claims, self.secret, self.ttl)
        self.users.touch_last_login(claims.get("username") or username)  # 失败静默，不影响登录
        payload = {
            "token": token,
            "session_id": secrets.token_hex(8),  # 服务端生成的新 sid，前端切换使用
            "user": public,
        }
        if note:
            payload["backend"] = note
        return 200, payload

    # -------------------------------------------------- 校验 / 登出
    def verify(self, token: str):
        """校验 token。合法返回 claims；非法/过期/已登出返回 None。"""
        return verify_token(token or "", self.secret, revoked=self.revoker)

    def logout(self, token: str) -> bool:
        """登出：把 jti 拉黑。返回是否成功校验过该 token。"""
        claims = verify_token(token or "", self.secret, revoked=self.revoker)
        if not claims:
            return False
        self.revoker.revoke(claims.get("jti", ""))
        # 顺带清掉该用户的 sid 绑定，防残留
        self.sid_bindings = {s: u for s, u in self.sid_bindings.items()
                             if u != claims.get("username")}
        return True

    # -------------------------------------------------- sid ↔ 用户绑定
    def bind_sid(self, sid: str, username: str):
        """首次使用某 sid 绑定到用户；同 sid 换用户拒绝。返回 (ok, error)。"""
        sid = (sid or "default")[:64]
        bound = self.sid_bindings.get(sid)
        if bound is None:
            self.sid_bindings[sid] = username
            return True, ""
        if bound != username:
            return False, f"该会话已绑定用户 {bound}，请刷新页面获取新会话"
        return True, ""


def build_auth():
    """按 .env 装配 AuthService。未配置 FEISHU_USER_TABLE_ID → None（网关 fail-closed 401）。"""
    table_id = os.getenv("FEISHU_USER_TABLE_ID", "")
    if not (os.getenv("FEISHU_APP_ID") and table_id):
        print("[M8][安全] 未配置 FEISHU_USER_TABLE_ID，认证墙处于关闭状态——"
              "所有受保护端点将返回 401。请按 docs/TABLE_SETUP.md 建用户表并填写配置。")
        return None
    secret = os.getenv("AUTH_SECRET", "")
    if not secret:
        secret = secrets.token_hex(32)
        print("[M8][警告] AUTH_SECRET 未设置，本次启动使用临时随机密钥（重启后全体登录态失效）。"
              "请尽快在 .env 固化 AUTH_SECRET。")
    ttl = int(os.getenv("AUTH_TOKEN_TTL", "7200"))
    limit = int(os.getenv("AUTH_LOGIN_LIMIT", "5"))
    window = int(os.getenv("AUTH_LOGIN_WINDOW", "300"))
    client = FeishuBitableClient.from_env()
    store = users.UserStore(client, table_id)
    print(f"[M8] 认证已启用：用户表={table_id} tokenTTL={ttl}s 登录限流={limit}次/{window}s（仅失败计数）")
    return AuthService(store, secret, ttl=ttl,
                       login_limiter=RateLimiter(limit=limit, window=window))
