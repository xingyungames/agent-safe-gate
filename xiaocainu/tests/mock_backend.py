#主服务器离线替身 供测试替换 backend_client 的公开函数
from __future__ import annotations

import base64
import json
import time

from api.backend_client import (
    BackendRejected,
    BackendUnauthorized,
    BackendUnavailable,
)

#各角色权限键 与 C# Core/AccountPolicy.cs 的 Matrix 对齐
_ROLE_PERMS = {
    "employee": [
        "auth.login", "invoice.read.self", "invoice.submit",
    ],
    "finance": [
        "auth.login", "invoice.read.self", "invoice.read.all", "invoice.submit",
        "invoice.review", "invoice.update", "log.read",
    ],
    "admin": [
        "auth.login", "invoice.read.self", "invoice.read.all", "invoice.submit",
        "invoice.review", "invoice.update", "invoice.delete", "log.read",
        "log.rollback", "admin.read", "account.create", "account.list", "account.manage",
    ],
    "superadmin": [
        "auth.login", "invoice.read.self", "invoice.read.all", "invoice.submit",
        "invoice.review", "invoice.update", "invoice.delete", "log.read",
        "log.rollback", "admin.read", "account.create", "account.list", "account.manage",
        "account.grant_admin", "account.delete",
    ],
}

_ROLE_LABELS = {
    "employee": "员工",
    "finance": "财务部",
    "admin": "管理员",
    "superadmin": "超级管理员",
}


#base64url 无填充编码
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


#伪造一个与 C# 同构的 JWT 载荷 含 sk 与 exp
def make_token(username: str, role: str = "employee", sk: str = "sk-test", ttl: int = 7200) -> str:
    now = int(time.time())
    payload = {
        "sub": "u_" + username,
        "name": username,
        "role": role,
        "sk": sk,
        "jti": "jti-" + username,
        "epo": 0,
        "iat": now,
        "exp": now + int(ttl),
        "iss": "aiapproval",
        "aud": "aiapproval-web",
    }
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload, ensure_ascii=False).encode())
    return f"{header}.{body}.sig"


#一个账号的完整资料
def make_user(username: str, role: str = "employee", name: str = "", employee_id: str = "") -> dict:
    return {
        "id": "u_" + username,
        "userName": username,
        "displayName": name or username,
        "employeeId": employee_id,
        "role": role,
        "roleLabel": _ROLE_LABELS.get(role, ""),
        "emailMasked": "",
        "permissions": [{"key": k, "label": k} for k in _ROLE_PERMS.get(role, [])],
    }


#一行报销单 C# 的形状
def make_row(record_id: str = "rec1", applicant: str = "张三", employee_id: str = "E0001",
             bill_no: str = "BX-000001", **over) -> dict:
    row = {
        "recordId": record_id,
        "billNo": bill_no,
        "applicant": applicant,
        "employeeId": employee_id,
        "department": "研发部",
        "rtype": "差旅费",
        "amount": 500,
        "reason": "出差往返",
        "hasInvoice": "有票",
        "city": "上海",
        "invoiceDate": "1790000000000",
        "submittedAt": "1790000000000",
        "compliance": "通过",
        "riskSummary": "无",
        "status": "待审核",
        "reviewer": "",
        "reviewOpinion": "",
        "reviewedAt": "",
        "invoiceInfo": "",
        "raw": {},
    }
    row.update(over)
    return row


#主服务器离线替身
class FakeBackend:
    #装配默认四个演示账号
    def __init__(self, users: dict | None = None, rows: list | None = None):
        self.users = users if users is not None else {
            "employee": ("Staff#2026abc", make_user("employee", "employee", "张三", "E0001")),
            "finance": ("Money#2026abc", make_user("finance", "finance", "李四", "E0002")),
            "admin": ("SysRoot#2026ak", make_user("admin", "admin", "王五", "E0003")),
            "root": ("Overlord#2026ak", make_user("superadmin", "superadmin", "赵六", "E0004")),
        }
        self.rows = rows if rows is not None else [make_row()]
        self.submitted = []
        self.calls = []
        self.fail = ""          # "" / "unavailable" / "rejected" / "unauthorized"
        self.login_count = 0
        self.login_ips = []

    #按账号名取用户资料
    def _user(self, username: str) -> dict | None:
        item = self.users.get(username)
        return item[1] if item else None

    #模拟登录
    def login(self, username: str, password: str, client_ip: str = "") -> dict:
        self.calls.append(("login", username))
        self.login_ips.append(client_ip)
        self.login_count += 1
        if self.fail == "unavailable":
            raise BackendUnavailable("模拟主服务器不可用", status=503)
        if self.fail == "rejected":
            raise BackendUnauthorized("用户名或口令错误", status=401, code="AUTH_FAILED")
        item = self.users.get((username or "").strip())
        if not item or item[0] != password:
            #与真实 backend_client._raise_for 一致：401 抛 BackendUnauthorized
            raise BackendUnauthorized("用户名或口令错误", status=401, code="AUTH_FAILED")
        user = item[1]
        return {
            "token": make_token(user["userName"], user["role"]),
            "signKey": "sk-test",
            "expiresAt": "",
            "user": user,
            "signing": {},
        }

    #模拟查身份
    def me(self, token: str) -> dict:
        self.calls.append(("me", token))
        if self.fail == "unavailable":
            raise BackendUnavailable("模拟主服务器不可用", status=503)
        if self.fail == "unauthorized":
            raise BackendUnauthorized("登录状态无效", status=401)
        payload = _payload_of(token)
        if not payload:
            raise BackendUnauthorized("登录状态无效", status=401)
        user = self._user(str(payload.get("name") or ""))
        if not user:
            raise BackendUnauthorized("登录状态无效", status=401)
        return {"permissions": user["permissions"], **user}

    #从令牌载荷取签名密钥
    def sign_key_of(self, token: str) -> str:
        return str((_payload_of(token) or {}).get("sk") or "")

    #模拟提交报销单
    def submit_with_token(self, token: str, sign_key: str, **fields) -> dict:
        self.calls.append(("submit", token))
        if self.fail == "unavailable":
            raise BackendUnavailable("模拟主服务器不可用", status=503)
        if self.fail == "rejected":
            raise BackendRejected("事由需为 2 到 200 个字符，请写清真实业务用途", status=400)
        payload = _payload_of(token) or {}
        user = self._user(str(payload.get("name") or "")) or {}
        self.submitted.append({**fields, "applicant": user.get("displayName", ""),
                               "employeeId": user.get("employeeId", "")})
        return {"record_id": "rec_new", "bill_no": "BX-000123", "message": "报销单已提交"}

    #模拟查报销单列表 按角色裁范围
    def list_invoices(self, token: str, sign_key: str, limit: int = 100) -> dict:
        self.calls.append(("list", token))
        if self.fail == "unavailable":
            raise BackendUnavailable("模拟主服务器不可用", status=503)
        payload = _payload_of(token) or {}
        user = self._user(str(payload.get("name") or "")) or {}
        perms = {p["key"] for p in user.get("permissions", [])}
        rows = list(self.rows)
        if "invoice.read.all" not in perms:
            rows = [r for r in rows if r.get("employeeId") == user.get("employeeId")]
        return {"scope": "all" if "invoice.read.all" in perms else "mine",
                "applicant": user.get("displayName", ""), "count": len(rows), "rows": rows[:limit]}


#解出令牌载荷 失败返回 None
def _payload_of(token: str) -> dict | None:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return None
    body = parts[1]
    body += "=" * (-len(body) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body.encode()).decode("utf-8"))
    except Exception:
        return None


#把替身挂到 backend_client 模块上
_PATCH_NAMES = ("login", "me", "sign_key_of", "submit_with_token", "list_invoices")
#记录被替换前的真实实现 便于个别用例临时拿回真函数
_ORIGINALS = {}


def install(fake: "FakeBackend", module=None) -> "FakeBackend":
    if module is None:
        from api import backend_client as module
    if module not in _ORIGINALS:
        _ORIGINALS[module] = {n: getattr(module, n) for n in _PATCH_NAMES}
    module.login = fake.login
    module.me = fake.me
    module.sign_key_of = fake.sign_key_of
    module.submit_with_token = fake.submit_with_token
    module.list_invoices = fake.list_invoices
    return fake


#把模块函数还原成真实实现（测试内部验证真实链路时用）
def restore(module=None) -> None:
    if module is None:
        from api import backend_client as module
    saved = _ORIGINALS.get(module)
    if not saved:
        return
    for name, fn in saved.items():
        setattr(module, name, fn)
