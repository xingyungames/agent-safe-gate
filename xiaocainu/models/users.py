"""
M8 · 用户表（飞书 Bitable 存储账号）
====================================
账号三要素存飞书「用户表」（表结构见 docs/TABLE_SETUP.md）：
- 密码只存 PBKDF2 哈希+盐（security/auth.py 生成），表里永远没有明文；
- 哈希只能由 tools/user_admin.py 脚本写入，严禁手填；
- UserStore 读表带 60s 内存缓存（表小、防刷飞书限流）；刚停用的账号最多 60s 缓冲期内
  仍可能登录成功——演示可接受，文档已注明。
"""
from __future__ import annotations

import time

from .feishu import FeishuBitableClient, FeishuError
from security.auth import hash_password, verify_password

# 用户表字段（与 docs/TABLE_SETUP.md 逐字一致）
# 2026-10-11 修订：去掉「部门」——报销部门以用户对话口述为准，账号资料不再存部门
USER_FIELDS = ["工号", "姓名", "用户名", "角色", "密码哈希", "盐",
               "账号状态", "创建时间", "最后登录时间"]

# 三角色
ROLE_EMPLOYEE = "员工"
ROLE_FINANCE = "财务"
ROLE_ADMIN = "管理员"
ALLOWED_ROLES = {ROLE_EMPLOYEE, ROLE_FINANCE, ROLE_ADMIN}

# 对外可见字段（list_users 时屏蔽密码哈希/盐——凭据不出用户表域）
PUBLIC_USER_FIELDS = ["工号", "姓名", "用户名", "角色", "账号状态",
                      "创建时间", "最后登录时间"]

_STATUS_OK = "启用"
_STATUS_DISABLED = "停用"


def _ms_now() -> int:
    return int(time.time() * 1000)


class UserStore:
    """飞书用户表读取层：get_by_username / list_users / touch_last_login。"""

    def __init__(self, client: FeishuBitableClient, table_id: str,
                 cache_ttl: float = 60.0) -> None:
        self.client = client
        self.table_id = table_id
        self.cache_ttl = cache_ttl
        self._cache: list = []       # [{record_id, fields(归一化)}]
        self._cached_at: float = 0.0

    # -------------------------------------------------- 内部
    def _load(self, force: bool = False) -> list:
        now = time.time()
        if force or not self._cache or (now - self._cached_at) > self.cache_ttl:
            recs = self.client.list_records(self.table_id, page_size=200)
            self._cache = [
                {"record_id": r.get("record_id"), "fields": self._normalize(r.get("fields", {}))}
                for r in recs
            ]
            self._cached_at = now
        return self._cache

    @staticmethod
    def _normalize(fields: dict) -> dict:
        user = {k: fields.get(k) for k in USER_FIELDS}
        for k in ("工号", "姓名", "用户名", "角色", "账号状态"):
            v = user.get(k)
            user[k] = v.strip() if isinstance(v, str) else ""
        return user

    # -------------------------------------------------- 查询
    def get_by_username(self, username: str):
        """按用户名精确查找。命中返回 {record_id, fields...}；未命中返回 None。"""
        name = (username or "").strip()
        if not name:
            return None
        for item in self._load():
            if item["fields"].get("用户名") == name:
                return item
        return None

    def list_users(self) -> list:
        """全量用户（屏蔽密码哈希/盐）。"""
        out = []
        for item in self._load():
            u = {k: item["fields"].get(k, "") for k in PUBLIC_USER_FIELDS}
            u["record_id"] = item["record_id"]
            out.append(u)
        return out

    def invalidate(self) -> None:
        """清缓存（管理脚本改表后调用，保证下次读到最新）。"""
        self._cache = []
        self._cached_at = 0.0

    # -------------------------------------------------- 登录回写（可选展示项）
    def touch_last_login(self, username: str) -> None:
        """登录成功回写「最后登录时间」。失败静默（非关键路径，不影响登录结果）。"""
        try:
            item = self.get_by_username(username)
            if item and item.get("record_id"):
                self.client.update_record(self.table_id, item["record_id"],
                                          {"最后登录时间": _ms_now()})
        except FeishuError:
            pass


# ---------------------------------------------------------- 账号管理核心（CLI 与测试共用）
def make_user_record(*, username: str, name: str, role: str,
                     password: str, employee_id: str = "", status: str = _STATUS_OK) -> dict:
    """生成用户表记录（密码在此刻哈希，明文不落任何地方）。角色非法直接抛 ValueError。

    2026-10-11 修订：不再写「部门」——报销部门以用户对话口述为准。
    """
    if role not in ALLOWED_ROLES:
        raise ValueError(f"角色必须是 {'/'.join(sorted(ALLOWED_ROLES))}，收到：{role!r}")
    if not (username or "").strip():
        raise ValueError("用户名不能为空")
    if not password:
        raise ValueError("密码不能为空")
    salt_hex, hash_hex = hash_password(password)
    return {
        "工号": employee_id or "",
        "姓名": name or username,
        "用户名": username.strip(),
        "角色": role,
        "密码哈希": hash_hex,
        "盐": salt_hex,
        "账号状态": status,
        "创建时间": _ms_now(),
    }


def authenticate(store: UserStore, username: str, password: str):
    """登录校验。返回 (user_item, error)；成功 error 为空。

    安全细节：用户不存在与密码错误返回同一句提示（防用户名枚举）；
    停用账号单独提示（合法用户需要知道找管理员）。
    """
    item = store.get_by_username(username)
    if not item:
        return None, "用户名或密码错误"
    f = item["fields"]
    if f.get("账号状态") == _STATUS_DISABLED:
        return None, "账号已停用，请联系管理员"
    if not verify_password(password or "", f.get("盐", ""), f.get("密码哈希", "")):
        return None, "用户名或密码错误"
    return item, ""


def reset_password(store: UserStore, username: str, password: str) -> str:
    """重置密码（保留原盐以减少无谓随机性？不——每次重置换新盐更安全）。返回用户名。"""
    item = store.get_by_username(username)
    if not item:
        raise ValueError(f"用户不存在：{username}")
    if not password:
        raise ValueError("密码不能为空")
    salt_hex, hash_hex = hash_password(password)
    store.client.update_record(store.table_id, item["record_id"],
                               {"密码哈希": hash_hex, "盐": salt_hex})
    store.invalidate()
    return username


def set_user_status(store: UserStore, username: str, enabled: bool) -> str:
    """启用/停用账号。"""
    item = store.get_by_username(username)
    if not item:
        raise ValueError(f"用户不存在：{username}")
    store.client.update_record(store.table_id, item["record_id"],
                               {"账号状态": _STATUS_OK if enabled else _STATUS_DISABLED})
    store.invalidate()
    return username
