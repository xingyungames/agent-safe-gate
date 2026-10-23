#登录身份 账号与权限的唯一来源是主服务器 C# 的登录与 /me 响应
from __future__ import annotations

from dataclasses import dataclass

#权限键 与 C# Core/AccountPolicy.cs 的 Perm 常量逐字一致
PERM_INVOICE_SUBMIT = "invoice.submit"
PERM_INVOICE_READ_SELF = "invoice.read.self"
PERM_INVOICE_READ_ALL = "invoice.read.all"
PERM_INVOICE_REVIEW = "invoice.review"

#C# 四个角色代码 不做中文折叠
ROLE_EMPLOYEE = "employee"
ROLE_FINANCE = "finance"
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"

ALLOWED_ROLES = (ROLE_EMPLOYEE, ROLE_FINANCE, ROLE_ADMIN, ROLE_SUPERADMIN)


#登录主体 所有权限判断都走 can()
@dataclass(frozen=True)
class Actor:
    user_id: str
    username: str
    name: str
    employee_id: str
    role: str
    role_label: str
    permissions: frozenset
    token: str = ""
    sign_key: str = ""
    expires_at: int = 0

    #是否拥有某个权限键
    def can(self, perm: str) -> bool:
        return perm in self.permissions

    #展示用姓名 空则回退用户名
    @property
    def display_name(self) -> str:
        return self.name or self.username

    #给前端的展示字段 沿用原有中文键
    def public(self) -> dict:
        return {
            "工号": self.employee_id,
            "姓名": self.display_name,
            "用户名": self.username,
            "角色": self.role_label,
            "角色代码": self.role,
        }


#C# 的 permissions 是 [{key,label}] 这里压平为 key 集合
def flatten_permissions(raw) -> frozenset:
    keys = set()
    for item in raw or []:
        if isinstance(item, str):
            keys.add(item)
        elif isinstance(item, dict):
            key = item.get("key")
            if key:
                keys.add(str(key))
    return frozenset(keys)


#由 C# 响应的 user 段构造 Actor
def build_actor(user: dict, *, permissions=None, token: str = "", sign_key: str = "",
                expires_at: int = 0) -> Actor:
    user = user or {}
    username = str(user.get("userName") or "").strip()
    name = str(user.get("displayName") or "").strip() or username
    if permissions is None:
        permissions = user.get("permissions")
    return Actor(
        user_id=str(user.get("id") or ""),
        username=username,
        name=name,
        employee_id=str(user.get("employeeId") or "").strip(),
        role=str(user.get("role") or "").strip(),
        role_label=str(user.get("roleLabel") or "").strip(),
        permissions=flatten_permissions(permissions),
        token=token,
        sign_key=sign_key,
        expires_at=int(expires_at or 0),
    )
