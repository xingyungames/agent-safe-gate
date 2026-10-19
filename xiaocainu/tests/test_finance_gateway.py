#财务审核网关层单测（离线、零网络、零真实凭证）
#覆盖：/finance/* 写操作经主服务器写入口（backend_client.*_as）——角色墙、字段白名单、
#中文→snake_case 投影剔除内部键、错误映射（4xx 原样 / 5xx 502 / 会话缺失 503）、
#只读端点（list/detail/attachment）继续直连飞书。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from models.retriever import Retriever, Chunk
from models.llm import LLMAdapter, MockProvider
from models.agent import XiaoCaiAgent
from models.feishu import FeishuError, FeishuBitableClient
from models import users
from api.main import dispatch, _finance_row
from api.session import SessionStore
from api.auth_service import AuthService
from security.auth import issue_token
from security.guard import RateLimiter
import api.backend_client as bc_mod
from api.backend_client import BackendError, BackendRejected

CORPUS = [
    Chunk(text="差旅费报销需提供出差审批单、交通票据、住宿发票。单人住宿标准不超过300元/晚。",
          source="报销制度.md"),
    Chunk(text="招待费报销需提供发票、消费清单。单次超过2000元须部门总监审批。",
          source="报销制度.md"),
]

AUTH_SECRET = "unit-test-secret-finance"


class MockFeishu:
    """离线替代飞书客户端：内存表 + 操作留痕（只读端点断言）。"""

    def __init__(self, rows=None, schema=None):
        self.rows = rows or []          # [{record_id, fields:{中文字段}}]
        self.schema = schema or ["单号", "申请人", "工号", "部门", "报销类型", "金额", "事由",
                                 "票据状态", "城市", "状态", "审核人", "审批意见",
                                 "记录提交时间", "合规结果", "风险摘要", "发票日期",
                                 "发票信息", "发票附件"]
        self.updated = []               # [(record_id, {字段:值})]
        self.deleted = []               # [record_id]
        self.fail_on = ""               # 非空则对应方法抛 FeishuError

    def _maybe_fail(self, op):
        if self.fail_on == op:
            raise FeishuError(f"模拟飞书{op}失败")

    def list_records(self, table_id, *, page_size=20, filter_expr="", app_token=""):
        self._maybe_fail("查询")
        return [dict(r) for r in self.rows]

    def get_record(self, table_id, record_id, app_token=""):
        self._maybe_fail("单条查询")
        for r in self.rows:
            if r["record_id"] == record_id:
                return dict(r)
        return {}

    def update_record(self, table_id, record_id, fields, app_token=""):
        self._maybe_fail("更新")
        self.updated.append((record_id, dict(fields)))
        for r in self.rows:
            if r["record_id"] == record_id:
                r["fields"].update(fields)
                return record_id
        return record_id

    def delete_record(self, table_id, record_id, app_token=""):
        self._maybe_fail("删除")
        self.deleted.append(record_id)
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["record_id"] != record_id]
        if len(self.rows) == before:
            raise FeishuError("记录不存在")
        return True

    def list_fields(self, table_id, *, app_token=""):
        self._maybe_fail("字段查询")
        return [{"field_name": n, "type": 1, "property": {}} for n in self.schema]

    def field_names(self, table_id, *, app_token="", use_cache=True):
        return set(self.schema)

    def create_records(self, table_id, records, app_token=""):
        self._maybe_fail("写入")
        return ["recNew1"]


class FakeUserClient:
    """内存版飞书用户表客户端（与 test_api.py 同款模式）。"""

    def __init__(self, seed_fields=None):
        self._next_id = 1
        self.records = []
        for f in seed_fields or []:
            self.records.append({"record_id": f"rec{self._next_id}", "fields": dict(f)})
            self._next_id += 1

    def list_records(self, table_id, *, page_size=20, filter_expr="", app_token=""):
        return [{"record_id": r["record_id"], "fields": dict(r["fields"])}
                for r in self.records]

    def update_record(self, table_id, record_id, fields, app_token=""):
        for r in self.records:
            if r.get("record_id") == record_id:
                r["fields"].update(fields)
                return record_id
        raise Exception("record not found")


def _seed_users():
    return [
        users.make_user_record(username="zhangsan", name="张三",
                               role="员工", password="pw123", employee_id="E0001"),
        users.make_user_record(username="caiwu", name="钱会计",
                               role="财务", password="pw456", employee_id="E0002"),
        users.make_user_record(username="admin", name="管理员",
                               role="管理员", password="pw789", employee_id="E0003"),
    ]


def _auth() -> AuthService:
    store = users.UserStore(FakeUserClient(_seed_users()), "tblUsers")
    return AuthService(store, AUTH_SECRET, ttl=600,
                       login_limiter=RateLimiter(limit=50, window=300))


def _role_tok(username: str, role: str) -> str:
    """直接签发指定角色 token（免登录流程，专测角色门）。"""
    claims = {"username": username, "name": username, "role": role, "employee_id": "E0009"}
    return "Bearer " + issue_token(claims, AUTH_SECRET, 600)


def _fin_tok() -> str:
    """财务角色 token，带真实姓名（审核要由主服务器按令牌盖章）。"""
    claims = {"username": "caiwu", "name": "钱会计", "role": "财务", "employee_id": "E0002"}
    return "Bearer " + issue_token(claims, AUTH_SECRET, 600)


def _agent(feishu=None, table_id="tblX"):
    llm = LLMAdapter(MockProvider(canned="[MOCK] 依据报销制度解答。"))
    ret = Retriever(CORPUS)
    return XiaoCaiAgent(llm, ret, feishu=feishu, table_id=table_id or "")


def _seed_rows():
    """两张单：recOld 待审核（旧时间）、recNew 审核中（新时间）——验证倒序。"""
    return [
        {"record_id": "recOld", "fields": {
            "单号": "BX-000001", "申请人": "张三", "部门": "研发部", "报销类型": "差旅费",
            "金额": 800, "事由": "北京出差", "票据状态": "有票", "城市": "北京",
            "记录提交时间": 1759000000000, "状态": "待审核"}},
        {"record_id": "recNew", "fields": {
            "单号": "BX-000002", "申请人": "钱会计", "部门": "财务部", "报销类型": "招待费",
            "金额": 2000, "事由": "客户接待", "票据状态": "有票", "城市": "上海",
            "记录提交时间": 1759171200000, "状态": "审核中"}},
    ]


class _Captured:
    """捕获主服务器适配层调用（验证网关转发了哪些参数）。"""
    def __init__(self):
        self.review = []
        self.update = []
        self.delete = []


@pytest.fixture
def fake_backend(monkeypatch):
    """用捕获型假适配器替换 backend_client.*_as，验证网关转发而非直连飞书。"""
    cap = _Captured()

    def review_as(username, *, record_id, status, opinion):
        cap.review.append({"username": username, "record_id": record_id,
                           "status": status, "opinion": opinion})
        return {"message": f"已审核：{status}"}

    def update_as(username, *, record_id, fields):
        cap.update.append({"username": username, "record_id": record_id,
                           "fields": dict(fields)})
        return {"message": f"已修改 {len(fields)} 个字段"}

    def delete_as(username, *, record_id, reason):
        cap.delete.append({"username": username, "record_id": record_id, "reason": reason})
        return {"message": f"已删除单据 {record_id}（理由：{reason}）"}

    monkeypatch.setattr(bc_mod, "review_as", review_as)
    monkeypatch.setattr(bc_mod, "update_as", update_as)
    monkeypatch.setattr(bc_mod, "delete_as", delete_as)
    return cap


# ---------------------------------------------------------- 角色墙
def test_finance_role_gate_only_finance_and_admin():
    auth = _auth()
    emp = _role_tok("zhangsan", "员工")
    fin = _fin_tok()
    adm = _role_tok("admin", "管理员")
    a = _agent(feishu=MockFeishu(_seed_rows()))

    code, p = dispatch("POST", "/finance/list", {}, a, SessionStore())
    assert code == 401, (code, p)
    code, p = dispatch("POST", "/finance/list", {}, a, SessionStore(), auth=auth, token=emp)
    assert code == 403 and "财务" in p.get("error", ""), (code, p)
    code, p = dispatch("POST", "/finance/list", {}, a, SessionStore(), auth=auth, token=fin)
    assert code == 200, (code, p)
    code, p = dispatch("POST", "/finance/list", {}, a, SessionStore(), auth=auth, token=adm)
    assert code == 200, (code, p)
    print("[PASS] test_finance_role_gate_only_finance_and_admin")


# ---------------------------------------------------------- meta：能力位 + 表内真实枚举
def test_finance_meta_capabilities_and_enums():
    auth = _auth()
    fin = _fin_tok()
    fs = MockFeishu(_seed_rows())
    fs.list_fields = lambda table_id, *, app_token="": [
        {"field_name": "部门", "type": 3,
         "property": {"options": [{"name": "研发部"}, {"name": "财务部"}]}},
        {"field_name": "报销类型", "type": 3,
         "property": {"options": [{"name": "差旅费"}, {"name": "招待费"}]}},
        {"field_name": "票据状态", "type": 3,
         "property": {"options": [{"name": "有票"}, {"name": "无票"}]}},
        {"field_name": "城市", "type": 3, "property": {"options": [{"name": "北京"}]}},
        {"field_name": "合规结果", "type": 3,
         "property": {"options": [{"name": "通过"}, {"name": "风险"}]}},
        {"field_name": "状态", "type": 3,
         "property": {"options": [{"name": "待审核"}, {"name": "已通过"}]}},
    ]
    a = _agent(feishu=fs)

    code, p = dispatch("POST", "/finance/meta", {}, a, SessionStore(), auth=auth, token=fin)
    assert code == 200, (code, p)
    assert p["can_review"] and p["can_update"] and p["can_delete"], p
    assert p["can_rollback"] is False, "本页不做回退（归审计侧）"
    assert p["departments"] == ["研发部", "财务部"], p
    assert p["types"] == ["差旅费", "招待费"], p
    assert p["invoice_states"] == ["有票", "无票"], p
    # 审核状态放宽为主服务器允许的集合（待审核/审核中 可改为 已通过/已驳回）
    assert p["review_statuses"] == ["审核中", "已通过", "已驳回"], p
    assert "单号" not in p["finance_editable_fields"], "AutoNumber 不可改"
    assert "状态" not in p["finance_editable_fields"], "状态只能经审核动作改"
    assert "申请人" not in p["finance_editable_fields"], "申请人属身份盖章字段"
    print("[PASS] test_finance_meta_capabilities_and_enums")


# ---------------------------------------------------------- list：投影 + 倒序 + 内部键剔除
def test_finance_list_projection_and_order():
    auth = _auth()
    fin = _fin_tok()
    fs = MockFeishu(_seed_rows())
    fs.rows[0]["fields"]["raw"] = {"内部": "绝不外泄"}
    fs.rows[0]["fields"]["神秘键"] = "不该出现"
    a = _agent(feishu=fs)

    code, p = dispatch("POST", "/finance/list", {}, a, SessionStore(), auth=auth, token=fin)
    assert code == 200, (code, p)
    assert p["count"] == 2 and p["scope"] == "all", p
    assert p["rows"][0]["record_id"] == "recNew", "应按记录提交时间倒序"
    r = p["rows"][1]
    assert r["bill_no"] == "BX-000001" and r["applicant"] == "张三", r
    assert r["rtype"] == "差旅费" and r["has_invoice"] == "有票", r
    assert r["submitted_at"] == 1759000000000, r
    assert "raw" not in r and "神秘键" not in r, "内部键必须被投影剔除"
    assert set(r.keys()) >= {"record_id", "bill_no", "applicant", "status"}
    print("[PASS] test_finance_list_projection_and_order")


# ---------------------------------------------------------- detail
def test_finance_detail_projection():
    auth = _auth()
    fin = _fin_tok()
    fs = MockFeishu(_seed_rows())
    a = _agent(feishu=fs)

    code, p = dispatch("POST", "/finance/detail", {"record_id": "recOld"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 200, (code, p)
    assert p["row"]["bill_no"] == "BX-000001" and p["row"]["record_id"] == "recOld", p
    code, p = dispatch("POST", "/finance/detail", {"record_id": "recNope"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 404, (code, p)
    print("[PASS] test_finance_detail_projection")


# ---------------------------------------------------------- review：转发主服务器（盖章/快照/审计在服务端）
def test_finance_review_forwards_to_backend(fake_backend):
    auth = _auth()
    fin = _fin_tok()
    a = _agent()

    code, p = dispatch("POST", "/finance/review",
                       {"record_id": "recOld", "status": "已通过", "opinion": "同意报销"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 200 and "已通过" in p.get("message", ""), (code, p)
    assert fake_backend.review, fake_backend.review
    c = fake_backend.review[-1]
    assert c["record_id"] == "recOld" and c["status"] == "已通过" and c["opinion"] == "同意报销", c
    assert c["username"] == "caiwu", "审核人由主服务器按令牌盖章，网关只传登录用户名"


def test_finance_review_does_not_stamp_reviewer_or_time(fake_backend):
    """网关只转发 record_id/status/opinion，绝不传审核人/审批时间（服务端盖章）。"""
    auth = _auth()
    fin = _fin_tok()
    a = _agent()
    dispatch("POST", "/finance/review",
             {"record_id": "recOld", "status": "已通过", "opinion": "同意报销"},
             a, SessionStore(), auth=auth, token=fin)
    c = fake_backend.review[-1]
    assert set(c.keys()) == {"username", "record_id", "status", "opinion"}, c


def test_finance_review_rejects_bad_input(fake_backend):
    auth = _auth()
    fin = _fin_tok()
    a = _agent()

    code, p = dispatch("POST", "/finance/review",
                       {"record_id": "recOld", "status": "待审核", "opinion": "随便改"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 400 and "已通过" in p.get("error", ""), (code, p)
    code, p = dispatch("POST", "/finance/review",
                       {"record_id": "recOld", "status": "已通过", "opinion": "好"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 400 and "2 个字" in p.get("error", ""), (code, p)
    # 终态由主服务器状态机裁定，网关不再本地拦截；被网关拒绝的请求不得转发主服务器
    assert fake_backend.review == [], "被网关拒绝的审核不得转发主服务器"


def test_finance_review_backend_rejection_mapped(fake_backend, monkeypatch):
    """主服务器业务拒绝（如终态不可再审）原样映射为 4xx。"""
    def review_as(username, *, record_id, status, opinion):
        raise BackendRejected("该单据已是终态，不可再审", 400)
    monkeypatch.setattr(bc_mod, "review_as", review_as)

    auth = _auth(); fin = _fin_tok(); a = _agent()
    code, p = dispatch("POST", "/finance/review",
                       {"record_id": "recOld", "status": "已通过", "opinion": "同意报销"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 400 and "终态" in p.get("error", ""), (code, p)


def test_finance_review_backend_unavailable_502(fake_backend, monkeypatch):
    """主服务器 5xx/超时 → 502，提示主服务器不可用。"""
    def review_as(username, *, record_id, status, opinion):
        raise BackendError("主服务器连接被拒绝")
    monkeypatch.setattr(bc_mod, "review_as", review_as)

    auth = _auth(); fin = _fin_tok(); a = _agent()
    code, p = dispatch("POST", "/finance/review",
                       {"record_id": "recOld", "status": "已通过", "opinion": "同意报销"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 502 and "主服务器" in p.get("error", ""), (code, p)


# ---------------------------------------------------------- update：白名单 + 枚举 + 字段口径
def test_finance_update_whitelist_and_enum(fake_backend):
    auth = _auth()
    fin = _fin_tok()
    a = _agent()

    code, p = dispatch("POST", "/finance/update",
                       {"record_id": "recOld", "fields": {"金额": 999, "事由": "改备注"}},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 200, (code, p)
    c = fake_backend.update[-1]
    assert c["fields"]["金额"] == "999" and c["fields"]["事由"] == "改备注", c["fields"]
    code, p = dispatch("POST", "/finance/update",
                       {"record_id": "recOld", "fields": {"申请人": "李四"}},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 400 and "不允许修改" in p.get("error", ""), (code, p)
    code, p = dispatch("POST", "/finance/update",
                       {"record_id": "recOld", "fields": {"报销类型": "通讯补贴"}},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 400, (code, p)
    print("[PASS] test_finance_update_whitelist_and_enum")


def test_finance_update_date_sent_as_yyyymmdd(fake_backend):
    """网关把界面日期归一成 YYYY-MM-DD 字符串发给主服务器（主服务器自己转毫秒写飞书）。"""
    auth = _auth()
    fin = _fin_tok()
    a = _agent()

    code, p = dispatch("POST", "/finance/update",
                       {"record_id": "recOld", "fields": {"发票日期": "2026-10-19"}},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 200, (code, p)
    c = fake_backend.update[-1]
    assert c["fields"]["发票日期"] == "2026-10-19", \
        f"日期必须发 YYYY-MM-DD 字符串，实际 {c['fields'].get('发票日期')!r}"


def test_finance_update_rejects_bad_date_and_amount(fake_backend):
    """日期无法解析 / 金额非数字 → 400 且不转发主服务器。"""
    auth = _auth()
    fin = _fin_tok()
    a = _agent()

    code, p = dispatch("POST", "/finance/update",
                       {"record_id": "recOld", "fields": {"发票日期": "不是日期"}},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 400 and "发票日期" in p.get("error", ""), (code, p)
    code, p = dispatch("POST", "/finance/update",
                       {"record_id": "recOld", "fields": {"金额": "abc"}},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 400 and "金额" in p.get("error", ""), (code, p)
    assert fake_backend.update == [], "被拒的修改不得转发主服务器"


# ---------------------------------------------------------- delete：转发主服务器（权限矩阵在服务端）
def test_finance_delete_forwards_to_backend(fake_backend):
    """财务可触达删除端点；最终删除权由主服务器矩阵裁决（invoice.delete 仅管理员）。
    网关负责转发 + 错误映射，不藏按钮也不自行放行。"""
    auth = _auth()
    fin = _fin_tok()
    a = _agent()

    code, p = dispatch("POST", "/finance/delete",
                       {"record_id": "recOld", "reason": "重复提交"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 200 and "已删除" in p.get("message", ""), (code, p)
    c = fake_backend.delete[-1]
    assert c["record_id"] == "recOld" and c["reason"] == "重复提交", c
    assert c["username"] == "caiwu", "删除由主服务器按令牌裁决，网关只传登录用户名"
    # 理由太短 → 网关提前拦，不转发
    code, p = dispatch("POST", "/finance/delete",
                       {"record_id": "recNew", "reason": "x"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 400 and "删除理由" in p.get("error", ""), (code, p)
    print("[PASS] test_finance_delete_forwards_to_backend")


def test_finance_delete_finance_gets_403_from_backend(monkeypatch):
    """主服务器矩阵：invoice.delete 仅管理员；财务点删除收主服务器 403，文案原样回显。"""
    def delete_as(username, *, record_id, reason):
        raise BackendRejected("只有管理员可以删除报销单", 403)
    monkeypatch.setattr(bc_mod, "delete_as", delete_as)

    auth = _auth(); fin = _fin_tok(); a = _agent()
    code, p = dispatch("POST", "/finance/delete",
                       {"record_id": "recOld", "reason": "重复提交"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 403 and "只有管理员" in p.get("error", ""), (code, p)


def test_finance_delete_missing_row_maps_backend_404(monkeypatch):
    """删除不存在的单据：网关转发主服务器，主服务器返回 404 → 网关原样 404。"""
    def delete_as(username, *, record_id, reason):
        raise BackendRejected("记录不存在", 404)
    monkeypatch.setattr(bc_mod, "delete_as", delete_as)

    auth = _auth(); fin = _fin_tok(); a = _agent()
    code, p = dispatch("POST", "/finance/delete",
                       {"record_id": "recNope", "reason": "删不存在的"},
                       a, SessionStore(), auth=auth, token=fin)
    assert code == 404, (code, p)


# ---------------------------------------------------------- 未接入飞书时（只读端点）fail-closed
def test_finance_without_feishu_503():
    auth = _auth()
    fin = _fin_tok()
    a = _agent(feishu=None, table_id="")

    code, p = dispatch("POST", "/finance/list", {}, a, SessionStore(), auth=auth, token=fin)
    assert code == 503 and "飞书" in p.get("error", ""), (code, p)
    print("[PASS] test_finance_without_feishu_503")


# ---------------------------------------------------------- 投影函数直测
def test_finance_row_projection_drops_unknown_keys():
    row = {"record_id": "recX", "单号": "BX-1", "金额": 100, "神秘键": "不该出现",
           "raw": {"a": 1}}
    out = _finance_row(row)
    assert out["bill_no"] == "BX-1" and out["amount"] == 100 and out["record_id"] == "recX", out
    assert "神秘键" not in out and "raw" not in out, out
    assert out["reviewer"] == "" and out["invoice_info"] == "", out
    print("[PASS] test_finance_row_projection_drops_unknown_keys")


def test_finance_projection_includes_invoice_files():
    """发票附件过网关：下发 file_token/name/size/type/url；
    url 是飞书签发的临时票据（含 extra 鉴权），供服务端中转下载用。
    tmp_url 必须剔除——它是用户身份链接，应用身份打不开（99991661）。"""
    row = {"record_id": "recA", "单号": "BX-1", "发票信息": "INV-1", "发票附件": [
        {"file_token": "tk1", "name": "发票.png", "size": 226539, "type": "image/png",
         "url": "https://open.feishu.cn/open-apis/drive/v1/medias/tk1/download?extra=%7B%22bitablePerm%22%3A%7B%7D%7D",
         "tmp_url": "https://open.feishu.cn/open-apis/drive/v1/medias/batch_get_tmp_download_url?file_tokens=tk1"}]}
    out = _finance_row(row)
    assert isinstance(out["invoice_files"], list) and len(out["invoice_files"]) == 1, out
    f = out["invoice_files"][0]
    assert f.get("name") == "发票.png" and f.get("file_token") == "tk1", f
    assert f.get("size") == 226539 and f.get("type") == "image/png", f
    assert "extra=" in f.get("url", ""), "需下发含 extra 鉴权票据的 url 供服务端中转"
    assert "tmp_url" not in f, "tmp_url 是用户身份临时链接，不得下发"
    print("[PASS] test_finance_projection_includes_invoice_files")


def test_finance_download_media_rejects_bad_token():
    """附件下载的 file_token 必须先校验：含 / 或超长直接拒（防路径穿越与滥用）。"""
    c = FeishuBitableClient(app_id="x", app_secret="y")
    for bad in ("../../secret", "abc/def", "", "t" * 200):
        try:
            c.download_media(bad)
            raise AssertionError(f"非法 file_token 应被拒：{bad[:20]!r}")
        except FeishuError as e:
            assert "非法" in str(e), e
    print("[PASS] test_finance_download_media_rejects_bad_token")


def test_finance_download_media_rejects_non_official_url():
    """只允许飞书官方域名的下载地址——防 SSRF（网关内网/元数据地址探测）。"""
    c = FeishuBitableClient(app_id="x", app_secret="y")
    for bad in ("https://evil.example.com/x",
                "http://open.feishu.cn/明文HTTP",
                "https://open.feishu.cn.evil.com/x",
                "file:///etc/passwd"):
        try:
            c.download_media("tok", url=bad)
            raise AssertionError(f"非官方域名应被拒：{bad}")
        except FeishuError as e:
            assert "官方域名" in str(e) or "非法" in str(e), e
    print("[PASS] test_finance_download_media_rejects_non_official_url")


def test_finance_attachment_route_not_public():
    """附件端点必须在认证墙内（不能出现在公开白名单）。"""
    from api.main import _PUBLIC_ENDPOINTS
    assert ("GET", "/finance/attachment") not in _PUBLIC_ENDPOINTS, \
        "附件下载绝不能免认证"
    print("[PASS] test_finance_attachment_route_not_public")


# ---------------------------------------------------------- 聊天填单角色门（防自审自批）
def test_finance_role_cannot_use_chat_or_submit():
    """财务/管理员不得经 /chat、/ocr、/reimburse 给自己填单——否则可自审自批。
    员工必须仍可正常填单（不得误伤）。"""
    auth = _auth()
    fin = _fin_tok()                       # 财务
    adm = _role_tok("admin", "管理员")     # 管理员
    emp = _role_tok("zhangsan", "员工")    # 员工
    a = _agent(feishu=MockFeishu(_seed_rows()))
    store = SessionStore()

    for tok, who in ((fin, "财务"), (adm, "管理员")):
        code, p = dispatch("POST", "/chat", {"session_id": "s1", "text": "我要填报销单"},
                           a, SessionStore(), auth=auth, token=tok)
        assert code == 403, f"{who} 用 /chat 应被拒：{(code, p)}"
        assert "财务审核台" in p.get("error", ""), p
        code, p = dispatch("POST", "/reimburse",
                           {"applicant": "我", "rtype": "差旅费", "amount": 100},
                           a, store, auth=auth, token=tok)
        assert code == 403, f"{who} 用 /reimburse 应被拒：{(code, p)}"

    # 员工不受影响（仍能聊与填单）
    code, p = dispatch("POST", "/chat", {"session_id": "e1", "text": "报销制度怎么规定的？"},
                       a, SessionStore(), auth=auth, token=emp)
    assert code == 200, f"员工 /chat 不应被拦：{(code, p)}"
    print("[PASS] test_finance_role_cannot_use_chat_or_submit")


def test_progress_query_allowed_for_all_roles():
    """进度查询是只读且财务自身也要用（查自己历史单），不应被角色门拦。"""
    auth = _auth()
    a = _agent(feishu=MockFeishu(_seed_rows()))
    for tok in (_fin_tok(), _role_tok("admin", "管理员"), _role_tok("zhangsan", "员工")):
        code, p = dispatch("POST", "/progress", {"applicant": "张三"},
                           a, SessionStore(), auth=auth, token=tok)
        assert code == 200, f"/progress 应放行所有角色：{(code, p)}"
    print("[PASS] test_progress_query_allowed_for_all_roles")


if __name__ == "__main__":
    test_finance_role_gate_only_finance_and_admin()
    test_finance_meta_capabilities_and_enums()
    test_finance_list_projection_and_order()
    test_finance_detail_projection()
    test_finance_review_forwards_to_backend()
    test_finance_review_does_not_stamp_reviewer_or_time()
    test_finance_review_rejects_bad_input()
    test_finance_review_backend_rejection_mapped()
    test_finance_review_backend_unavailable_502()
    test_finance_update_whitelist_and_enum()
    test_finance_update_date_sent_as_yyyymmdd()
    test_finance_update_rejects_bad_date_and_amount()
    test_finance_delete_forwards_to_backend()
    test_finance_delete_finance_gets_403_from_backend()
    test_finance_delete_missing_row_maps_backend_404()
    test_finance_without_feishu_503()
    test_finance_row_projection_drops_unknown_keys()
    test_finance_projection_includes_invoice_files()
    test_finance_download_media_rejects_bad_token()
    test_finance_download_media_rejects_non_official_url()
    test_finance_attachment_route_not_public()
    test_finance_role_cannot_use_chat_or_submit()
    test_progress_query_allowed_for_all_roles()
    print("\n财务审核网关层单测全部通过")
