"""
M5 · API 网关单测（离线、零网络、零真实凭证）
=========================================
覆盖：/health、/chat(多轮 history + B 跨轮草稿合并)、/reimburse、/progress、404，
M8 认证墙（401/403/sid 绑定/登出撤销/登录限流），
以及 FastAPI TestClient 冒烟测试（验证完整 HTTP 层 + 规范 JSON 返回）。

运行：
    python tests/test_api.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.retriever import Retriever, Chunk
from models.llm import LLMAdapter, MockProvider
from models.agent import XiaoCaiAgent
from models import users
from api.main import dispatch, app
import api.main as apimod
from api.session import SessionStore
from api.auth_service import AuthService
from security.guard import RateLimiter


CORPUS = [
    Chunk(text="差旅费报销需提供出差审批单、交通票据、住宿发票。单人住宿标准不超过300元/晚。",
          source="报销制度.md"),
    Chunk(text="招待费报销需提供发票、消费清单。单次超过2000元须部门总监审批。",
          source="报销制度.md"),
]


class MockFeishu:
    """离线替代飞书客户端：记录调用，按设定返回。"""
    def __init__(self):
        self.created = []

    def create_records(self, table_id, records):
        ids = [f"rec_{i}_{len(self.created)}" for i in range(len(records))]
        self.created.extend(records)
        return ids

    def list_records(self, table_id, **kw):
        return [{
            "record_id": "r1",
            "fields": {"单号": "BX-000013", "申请人": "小新新", "部门": "研发部",
                       "报销类型": "差旅费", "金额": 1800, "事由": "北京出差",
                       "状态": "审核中", "合规结果": "通过", "票据状态": "有票"},
        }]

    def get_record(self, table_id, record_id, **kw):
        return {"record_id": record_id, "fields": {"单号": "BX-DEMO-001"}}


class FakeUserClient:
    """内存版飞书用户表客户端（与 test_users.py 同款模式）。"""

    def __init__(self, seed_fields=None):
        self._next_id = 1
        self.records = []
        for f in seed_fields or []:
            self.records.append({"record_id": f"rec{self._next_id}", "fields": dict(f)})
            self._next_id += 1
        self.list_calls = 0
        self.updates = []
        self.created = []

    def list_records(self, table_id, *, page_size=20, filter_expr="", app_token=""):
        self.list_calls += 1
        return [{"record_id": r["record_id"], "fields": dict(r["fields"])}
                for r in self.records]

    def create_records(self, table_id, records, app_token=""):
        ids = []
        for f in records:
            rid = f"rec{self._next_id}"
            self._next_id += 1
            self.records.append({"record_id": rid, "fields": dict(f)})
            ids.append(rid)
        self.created.extend(records)
        return ids

    def update_record(self, table_id, record_id, fields, app_token=""):
        self.updates.append((record_id, dict(fields)))
        for r in self.records:
            if r["record_id"] == record_id:
                r["fields"].update(fields)
                return record_id
        raise Exception("record not found")


def _seed_users():
    """预置演示账号：员工×2 / 财务 / 管理员各一 + 一个停用账号。
    第二个员工（xiaoxin）供「同 sid 换用户」用例使用——聊天填单仅限员工角色，
    所以换的那个用户也必须是启用的员工账号。"""
    return [
        users.make_user_record(username="zhangsan", name="张三",
                               role="员工", password="pw123", employee_id="E0001"),
        users.make_user_record(username="xiaoxin", name="小新",
                               role="员工", password="pw123", employee_id="E0004"),
        users.make_user_record(username="caiwu", name="钱会计",
                               role="财务", password="pw456", employee_id="E0002"),
        users.make_user_record(username="admin", name="管理员",
                               role="管理员", password="pw789", employee_id="E0003"),
        users.make_user_record(username="lisi", name="李四",
                               role="员工", password="pw000", status="停用"),
    ]


AUTH_SECRET = "unit-test-secret-m8"


def _auth(limit: int = 5) -> AuthService:
    """离线 AuthService：Fake 用户表 + 固定测试密钥。"""
    store = users.UserStore(FakeUserClient(_seed_users()), "tblUsers")
    return AuthService(store, AUTH_SECRET, ttl=600,
                       login_limiter=RateLimiter(limit=limit, window=300))


def _tok(auth: AuthService, username: str, password: str) -> str:
    """直调 AuthService.login 拿 token（不发 HTTP、不碰真实飞书）。"""
    code, p = auth.login(username, password, client_ip="127.0.0.1")
    assert code == 200, f"登录失败 {username}: {p}"
    return f"Bearer {p['token']}"


def _agent(feishu=None, table_id=None):
    llm = LLMAdapter(MockProvider(canned="[MOCK] 依据报销制度解答。"))
    ret = Retriever(CORPUS)
    return XiaoCaiAgent(llm, ret, feishu=feishu, table_id=table_id)


# ---------------------------------------------------------- 端点
def test_health():
    code, p = dispatch("GET", "/health", {}, _agent(), SessionStore())
    assert code == 200 and p["status"] == "ok"
    print("[PASS] test_health")


def test_chat_consult_offline():
    a = _agent()
    s = SessionStore()
    auth = _auth()
    tok = _tok(auth, "zhangsan", "pw123")
    code, p = dispatch("POST", "/chat", {"session_id": "c1", "text": "差旅费住宿标准是多少"},
                       a, s, auth=auth, token=tok)
    assert code == 200 and p["intent"] == "A"
    # 多轮：第二轮应把历史带入（store 内已有 2 条消息）
    assert len(s.get_messages("c1")) == 2
    # 第三轮仍正常
    code2, p2 = dispatch("POST", "/chat", {"session_id": "c1", "text": "那招待费呢"},
                         a, s, auth=auth, token=tok)
    assert code2 == 200 and p2["intent"] == "A"
    assert len(s.get_messages("c1")) == 4
    print("[PASS] test_chat_consult_offline")


def test_chat_fill_merges_draft():
    """B 分支跨轮补全：第一轮说姓名+部门，第二轮补全其余字段 → 进入确认态，第三轮确认提交。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    s = SessionStore()
    auth = _auth()
    tok = _tok(auth, "zhangsan", "pw123")
    r1 = dispatch("POST", "/chat", {"session_id": "f1", "text": "我要填报销单 张三 研发部"},
                  a, s, auth=auth, token=tok)
    assert r1[0] == 200 and r1[1]["intent"] == "B"
    assert r1[1]["record_id"] is None, "第一轮缺项不应写表"
    r2 = dispatch("POST", "/chat", {"session_id": "f1", "text": "招待费 800 有发票"},
                  a, s, auth=auth, token=tok)
    assert r2[0] == 200 and r2[1]["intent"] == "B"
    assert r2[1]["record_id"] is None, "合并草稿后字段齐全应进入确认态（不直接写表）"
    assert "确认提交" in r2[1]["answer"], "应提示用户确认"
    # 用户确认 → 真正写表
    r3 = dispatch("POST", "/chat", {"session_id": "f1", "text": "确认提交"},
                  a, s, auth=auth, token=tok)
    assert r3[0] == 200 and r3[1]["intent"] == "B"
    assert r3[1]["record_id"], "确认后字段齐全应已写表"
    assert "✅" in r3[1]["answer"]
    print("[PASS] test_chat_fill_merges_draft")


def test_reimburse_endpoint():
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    auth = _auth()
    tok = _tok(auth, "zhangsan", "pw123")
    code, p = dispatch("POST", "/reimburse",
                       {"applicant": "张三", "rtype": "差旅费", "amount": 800, "has_invoice": "有票"},
                       a, SessionStore(), auth=auth, token=tok)
    assert code == 200 and p["record_id"]
    assert p["status"] == "待审核"
    print("[PASS] test_reimburse_endpoint")


def test_reimburse_no_feishu_503():
    a = _agent()  # 无飞书
    auth = _auth()
    tok = _tok(auth, "zhangsan", "pw123")
    code, p = dispatch("POST", "/reimburse", {"applicant": "张三"}, a, SessionStore(),
                       auth=auth, token=tok)
    assert code == 503
    print("[PASS] test_reimburse_no_feishu_503")


def test_progress_endpoint():
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    auth = _auth()
    tok = _tok(auth, "caiwu", "pw456")  # 财务：P4 起可查任意人
    code, p = dispatch("POST", "/progress", {"applicant": "小新新"}, a, SessionStore(),
                       auth=auth, token=tok)
    assert code == 200 and p["intent"] == "D"
    assert "小新新" in p["answer"]
    print("[PASS] test_progress_endpoint")


def test_progress_requires_param():
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    auth = _auth()
    tok = _tok(auth, "caiwu", "pw456")
    code, p = dispatch("POST", "/progress", {}, a, SessionStore(), auth=auth, token=tok)
    assert code == 400
    print("[PASS] test_progress_requires_param")


def test_not_found():
    auth = _auth()
    tok = _tok(auth, "zhangsan", "pw123")
    code, p = dispatch("GET", "/nope", {}, _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 404
    print("[PASS] test_not_found")


# ---------------------------------------------------------- M8 认证墙
def test_auth_wall_401_fail_closed():
    """无 token / 坏 token / auth 未配置 → 一律 401（fail-closed，无匿名旁路）。"""
    a = _agent()
    s = SessionStore()
    auth = _auth()
    # 无 token
    code, p = dispatch("POST", "/chat", {"session_id": "w1", "text": "你好"}, a, s, auth=auth, token="")
    assert code == 401, "缺 token 应 401"
    # 伪造 token
    code, p = dispatch("POST", "/chat", {"session_id": "w1", "text": "你好"},
                       a, s, auth=auth, token="Bearer fake.token.here")
    assert code == 401, "伪造 token 应 401"
    # auth 未配置 → 也是 401（不是匿名放行）
    code, p = dispatch("POST", "/chat", {"session_id": "w1", "text": "你好"}, a, s, auth=None)
    assert code == 401 and "auth required" in p["error"], "auth 未配置应 fail-closed 401"
    # /health 免认证不受影响
    code, p = dispatch("GET", "/health", {}, a, s, auth=None)
    assert code == 200
    print("[PASS] test_auth_wall_401_fail_closed")


def test_login_flow():
    """登录成功返回 token/新 sid/用户信息；GET /me 还原身份。"""
    a = _agent()
    auth = _auth()
    code, p = dispatch("POST", "/login", {"username": "zhangsan", "password": "pw123"},
                       a, SessionStore(), auth=auth, client_ip="1.2.3.4")
    assert code == 200 and p["token"] and p["session_id"]
    assert p["user"]["角色"] == "员工" and p["user"]["姓名"] == "张三"
    assert "密码哈希" not in str(p) and "盐" not in str(p), "登录响应不得泄露凭据字段"
    code, p = dispatch("GET", "/me", {}, a, SessionStore(), auth=auth,
                       token=f"Bearer {p['token']}")
    assert code == 200
    assert p["user"]["username"] == "zhangsan" and p["user"]["role"] == "员工"
    assert "department" not in p["user"], "用户表已删部门列，claims 不应再有 department"
    assert p["user"]["employee_id"] == "E0001"
    print("[PASS] test_login_flow")


def test_login_rejections():
    """错密码/不存在用户（同提示防枚举）/停用账号/缺参数/auth 未配置。"""
    a = _agent()
    auth = _auth()
    code, p = dispatch("POST", "/login", {"username": "zhangsan", "password": "bad"},
                       a, SessionStore(), auth=auth)
    assert code == 401 and p["error"] == "用户名或密码错误"
    code, p = dispatch("POST", "/login", {"username": "nobody", "password": "bad"},
                       a, SessionStore(), auth=auth)
    assert code == 401 and p["error"] == "用户名或密码错误", "不存在与错密码提示必须一致（防枚举）"
    code, p = dispatch("POST", "/login", {"username": "lisi", "password": "pw000"},
                       a, SessionStore(), auth=auth)
    assert code == 401 and "停用" in p["error"]
    code, p = dispatch("POST", "/login", {"username": "", "password": "x"},
                       a, SessionStore(), auth=auth)
    assert code == 400
    code, p = dispatch("POST", "/login", {"username": "zhangsan", "password": "pw123"},
                       a, SessionStore(), auth=None)
    assert code == 503, "auth 未配置时 /login 应 503"
    print("[PASS] test_login_rejections")


def test_login_rate_limit_only_failures():
    """登录限流只计失败：正常登录不消耗配额；失败达上限后该 用户名|IP 全部拒绝（含再输对的），窗口过后自动解锁。"""
    a = _agent()
    auth = _auth(limit=2)
    code, p = dispatch("POST", "/login", {"username": "zhangsan", "password": "pw123"},
                       a, SessionStore(), auth=auth)
    assert code == 200, "正常登录不消耗限流配额"
    for _ in range(2):
        dispatch("POST", "/login", {"username": "zhangsan", "password": "bad"},
                 a, SessionStore(), auth=auth)
    code, p = dispatch("POST", "/login", {"username": "zhangsan", "password": "pw123"},
                       a, SessionStore(), auth=auth)
    assert code == 429, "失败达上限后即使密码正确也应拒绝（防爆破）"
    code, p = dispatch("POST", "/login", {"username": "caiwu", "password": "pw456"},
                       a, SessionStore(), auth=auth)
    assert code == 200, "其他用户不受影响"
    print("[PASS] test_login_rate_limit_only_failures")


def test_sid_binding_403():
    """同 sid 换用户 → 403（防会话串用/越权读他人草稿）；换新 sid 正常。
    两个账号都用**员工**角色——聊天填单仅限员工，财务/管理员会先被角色门 403 挡下。"""
    a = _agent()
    s = SessionStore()
    auth = _auth()
    tok_a = _tok(auth, "zhangsan", "pw123")
    tok_b = _tok(auth, "xiaoxin", "pw123")   # 另一个启用的员工账号
    code, p = dispatch("POST", "/chat", {"session_id": "shared", "text": "你好"},
                       a, s, auth=auth, token=tok_a)
    assert code == 200
    code, p = dispatch("POST", "/chat", {"session_id": "shared", "text": "你好"},
                       a, s, auth=auth, token=tok_b)
    assert code == 403, "同 sid 换用户应 403"
    code, p = dispatch("POST", "/chat", {"session_id": "other", "text": "你好"},
                       a, s, auth=auth, token=tok_b)
    assert code == 200, "新 sid 正常"
    print("[PASS] test_sid_binding_403")


def test_logout_revokes_token():
    """登出后原 token 立即失效；重新登录拿新 token 可用。"""
    a = _agent()
    s = SessionStore()
    auth = _auth()
    tok = _tok(auth, "zhangsan", "pw123")
    code, p = dispatch("POST", "/chat", {"session_id": "lo1", "text": "你好"},
                       a, s, auth=auth, token=tok)
    assert code == 200
    code, p = dispatch("POST", "/logout", {}, a, s, auth=auth, token=tok)
    assert code == 200
    code, p = dispatch("POST", "/chat", {"session_id": "lo1", "text": "你好"},
                       a, s, auth=auth, token=tok)
    assert code == 401, "登出后原 token 应立即失效"
    tok2 = _tok(auth, "zhangsan", "pw123")
    code, p = dispatch("POST", "/chat", {"session_id": "lo2", "text": "你好"},
                       a, s, auth=auth, token=tok2)
    assert code == 200, "重新登录后应可用"
    print("[PASS] test_logout_revokes_token")


def test_role_overrides_gateway():
    """/reimburse 申请人以登录身份覆写；/progress 员工只能查自己、财务查任意人。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    auth = _auth()
    tok_emp = _tok(auth, "zhangsan", "pw123")
    # 员工直调 /reimburse 冒充别人 → 申请人工号被覆写为登录身份；
    # 部门以提交者口述为准（body 市场部落库，不再被账号资料覆写）
    code, p = dispatch("POST", "/reimburse",
                       {"applicant": "李四", "department": "市场部", "rtype": "差旅费",
                        "amount": 500, "has_invoice": "有票"},
                       a, SessionStore(), auth=auth, token=tok_emp)
    assert code == 200 and p["record_id"]
    rec = a.feishu.created[-1]
    assert rec["申请人"] == "张三", "直调 API 冒充申请人应被身份覆写"
    assert rec["部门"] == "市场部", "口述部门应优先于账号资料"
    assert rec.get("工号") == "E0001", "登录身份工号应写入"
    # 员工 /progress 指定查别人 → 忽略，强制查自己（MockFeishu 只有 小新新 的记录）
    code, p = dispatch("POST", "/progress", {"applicant": "小新新"}, a, SessionStore(),
                       auth=auth, token=tok_emp)
    assert code == 200 and p["intent"] == "D"
    assert "没有找到" in p["answer"], "员工查他人应被强制改为查自己"
    # 财务可查任意人
    tok_fin = _tok(auth, "caiwu", "pw456")
    code, p = dispatch("POST", "/progress", {"applicant": "小新新"}, a, SessionStore(),
                       auth=auth, token=tok_fin)
    assert code == 200 and "小新新" in p["answer"]
    print("[PASS] test_role_overrides_gateway")


# ---------------------------------------------------------- FastAPI 冒烟（TestClient 验证完整 HTTP 层 + 规范 JSON）
def test_http_server_smoke():
    from fastapi.testclient import TestClient
    # 用离线 Fake auth 替换真实单例（真实 AUTH 依赖用户表配置，离线环境下为 None）
    fake_auth = _auth()
    apimod.AUTH = fake_auth
    tok = _tok(fake_auth, "zhangsan", "pw123")
    client = TestClient(app)
    # GET /health
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # OPTIONS 预检（CORS）：带 preflight 头才会被中间件处理并返回 200
    r = client.options(
        "/chat",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers
    # POST /chat 未带 token → 401（认证墙在 HTTP 层生效）
    r = client.post("/chat", json={"session_id": "h1", "text": "差旅费住宿标准是多少"})
    assert r.status_code == 401
    # 带 token → 200 规范 JSON
    r = client.post("/chat", json={"session_id": "h1", "text": "差旅费住宿标准是多少"},
                    headers={"Authorization": tok})
    assert r.status_code == 200
    assert r.json()["intent"] == "A"
    # 入口防注入：超长 text 应被 pydantic 拒（422）
    long_text = "报" * 3000
    r = client.post("/chat", json={"text": long_text}, headers={"Authorization": tok})
    assert r.status_code == 422
    print("[PASS] test_http_server_smoke")


# ---------------------------------------------------------- M9 发票 OCR 端点
import base64 as _b64
from models.ocr import OCRResult
from api.main import _decode_image, _strip_data_url  # noqa: E402

_JPEG_B64 = _b64.b64encode(b"\xff\xd8\xff\xe0FAKEJPGDATA").decode()
_JPEG_BYTES = b"\xff\xd8\xff\xe0FAKEJPGDATA"


class FakeOcrEngine:
    """离线假 OCR 引擎：返回固定 OCRResult 或抛指定异常。"""
    name = "fake-ocr"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def extract(self, *, image_b64, content_type="image/jpeg", filename=""):
        self.calls.append({"len": len(image_b64), "mime": content_type, "filename": filename})
        if self.error is not None:
            raise self.error
        return self.result


def _ocr_result():
    return OCRResult(
        fields={"amount": 800.0, "invoice_date": "2026/10/1", "invoice_no": "INV1",
                "seller": "某酒店", "rtype": "差旅费", "city": "北京"},
        warnings=[], usage={"total_tokens": 384}, model="deepseek-flash",
        provider="deepseek-ocr", latency_ms=120,
    )


def test_ocr_requires_auth():
    code, p = dispatch("POST", "/ocr", {"image_b64": _JPEG_B64}, _agent(), SessionStore())
    assert code == 401, "OCR 端点必须走认证墙"
    print("[PASS] test_ocr_requires_auth")


def test_ocr_rejects_non_image():
    auth = _auth(); tok = _tok(auth, "zhangsan", "pw123")
    # "hello" 的 base64：能解码但无图片魔数 → 400
    code, p = dispatch("POST", "/ocr", {"session_id": "ocr1", "image_b64": "aGVsbG8="},
                       _agent(), SessionStore(), auth=auth, token=tok,
                       ocr_engine=FakeOcrEngine(_ocr_result()))
    assert code == 400 and "invalid image" in p["error"]
    print("[PASS] test_ocr_rejects_non_image")


def test_ocr_success_with_mock_engine():
    auth = _auth(); tok = _tok(auth, "zhangsan", "pw123")
    eng = FakeOcrEngine(_ocr_result())
    code, p = dispatch("POST", "/ocr", {"session_id": "ocr2", "image_b64": _JPEG_B64,
                                        "filename": "fapiao.jpg"},
                       _agent(), SessionStore(), auth=auth, token=tok, ocr_engine=eng)
    assert code == 200 and p["ok"] and p["fields"]["amount"] == 800.0
    assert p["mime"] == "image/jpeg" and "有发票" in p["preview"]
    assert p["invoice_info"] == "INV1；某酒店"
    assert eng.calls[0]["mime"] == "image/jpeg"
    print("[PASS] test_ocr_success_with_mock_engine")


def test_ocr_engine_error_502():
    from models.ocr import OCRError
    auth = _auth(); tok = _tok(auth, "zhangsan", "pw123")
    eng = FakeOcrEngine(error=OCRError("图片不是发票/收据/消费凭证"))
    code, p = dispatch("POST", "/ocr", {"session_id": "ocr3", "image_b64": _JPEG_B64},
                       _agent(), SessionStore(), auth=auth, token=tok, ocr_engine=eng)
    assert code == 502 and "OCR failed" in p["error"]
    print("[PASS] test_ocr_engine_error_502")


def test_ocr_not_configured_503():
    """模块级 OCR_ENGINE=None（等价未配 key）→ /ocr 诚实 503，不静默 mock。"""
    auth = _auth(); tok = _tok(auth, "zhangsan", "pw123")
    orig = apimod.OCR_ENGINE
    apimod.OCR_ENGINE = None
    try:
        code, p = dispatch("POST", "/ocr", {"session_id": "ocr5", "image_b64": _JPEG_B64},
                           _agent(), SessionStore(), auth=auth, token=tok, ocr_engine=None)
    finally:
        apimod.OCR_ENGINE = orig
    assert code == 503 and "OCR not configured" in p["error"]
    print("[PASS] test_ocr_not_configured_503")


def test_decode_image_helpers():
    # data URL 前缀剥离
    assert _strip_data_url("data:image/png;base64,AAAA") == "AAAA"
    assert _strip_data_url("AAAA") == "AAAA"
    # 魔数判定
    raw, mime = _decode_image(_JPEG_B64, 1024 * 1024)
    assert mime == "image/jpeg" and raw == _JPEG_BYTES
    webp = _b64.b64encode(b"RIFF0000WEBPVP8 ").decode()
    _, mime = _decode_image(webp, 1024 * 1024)
    assert mime == "image/webp"
    fake_webp = _b64.b64encode(b"RIFF0000XXXX").decode()  # RIFF 但非 WEBP
    assert _decode_image(fake_webp, 1024 * 1024) == (None, None)
    # 超限
    big = _b64.b64encode(b"\xff\xd8\xff" + b"\x00" * 300).decode()
    assert _decode_image(big, 100) == (None, None)
    print("[PASS] test_decode_image_helpers")


class MockFeishuM9(MockFeishu):
    """M9 扩展：field_names/upload_attachment/update_record 全离线。"""

    def __init__(self):
        super().__init__()
        self.uploaded = []
        self.updated = []

    def field_names(self, table_id, **kw):
        return {"发票信息", "发票附件", "事由", "金额"}

    def upload_attachment(self, filename, file_bytes, content_type="application/octet-stream",
                          app_token=""):
        self.uploaded.append((filename, bytes(file_bytes), content_type))
        return "boxcnenTokenApi"

    def update_record(self, table_id, record_id, fields, app_token=""):
        self.updated.append((table_id, record_id, dict(fields)))
        return record_id


def test_chat_confirm_with_image_attaches_invoice():
    """端到端：带 OCR 字段建草稿 → 确认提交捎图 → 发票信息随单写 + 附件回写关联。"""
    mf = MockFeishuM9()
    auth = _auth(); tok = _tok(auth, "zhangsan", "pw123")
    s = SessionStore()
    # 第一轮：OCR 字段随首句并入草稿
    code, p = dispatch("POST", "/chat",
                       {"session_id": "m9e2e", "text": "我是张三，研发部，差旅费 800元，有发票",
                        "invoice_no": "INV1", "invoice_seller": "某酒店"},
                       _agent(feishu=mf, table_id="tblX"), s, auth=auth, token=tok)
    assert code == 200 and p["intent"] == "B"
    assert p["draft"].get("invoice_no") == "INV1"
    # 第二轮：确认提交 + 发票图
    code2, p2 = dispatch("POST", "/chat",
                         {"session_id": "m9e2e", "text": "确认提交",
                          "image_b64": _JPEG_B64, "image_mime": "image/jpeg"},
                         _agent(feishu=mf, table_id="tblX"), s, auth=auth, token=tok)
    assert code2 == 200 and p2["record_id"]
    assert mf.created[0].get("发票信息") == "INV1；某酒店"
    assert mf.uploaded and mf.uploaded[0] == ("发票.jpg", _JPEG_BYTES, "image/jpeg")
    assert mf.updated and mf.updated[0][1] == p2["record_id"]
    assert "已存档" in p2["answer"]
    print("[PASS] test_chat_confirm_with_image_attaches_invoice")


def test_chat_invalid_image_400():
    auth = _auth(); tok = _tok(auth, "zhangsan", "pw123")
    code, p = dispatch("POST", "/chat",
                       {"session_id": "m9bad", "text": "我要报销",
                        "image_b64": "aGVsbG8="},
                       _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 400 and "invalid image" in p["error"]
    print("[PASS] test_chat_invalid_image_400")


if __name__ == "__main__":
    test_health()
    test_chat_consult_offline()
    test_chat_fill_merges_draft()
    test_reimburse_endpoint()
    test_reimburse_no_feishu_503()
    test_progress_endpoint()
    test_progress_requires_param()
    test_not_found()
    test_auth_wall_401_fail_closed()
    test_login_flow()
    test_login_rejections()
    test_login_rate_limit_only_failures()
    test_sid_binding_403()
    test_logout_revokes_token()
    test_role_overrides_gateway()
    test_ocr_requires_auth()
    test_ocr_rejects_non_image()
    test_ocr_success_with_mock_engine()
    test_ocr_engine_error_502()
    test_ocr_not_configured_503()
    test_decode_image_helpers()
    test_chat_confirm_with_image_attaches_invoice()
    test_chat_invalid_image_400()
    test_http_server_smoke()
    print("\n全部 M5 API 网关单测通过")
