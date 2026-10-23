#API 网关离线单测 零网络
#账号与权限来自主服务器（离线替身），单据读写全部经主服务器
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_backend import FakeBackend, install, make_row, make_token, make_user
from models.retriever import Retriever, Chunk
from models.llm import LLMAdapter, MockProvider
from models.agent import XiaoCaiAgent
from api import backend_client
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

EMP = ("employee", "Staff#2026abc")   #张三 E0001 员工
FIN = ("finance", "Money#2026abc")    #李四 E0002 财务部


class MockFeishu:
    #离线假飞书客户端 只承担发票附件列
    def __init__(self):
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


#装上离线主服务器
def _backend(rows=None):
    fake = FakeBackend(rows=rows)
    install(fake, backend_client)
    return fake


#装配认证服务与离线主服务器
def _setup(rows=None, limit: int = 5):
    fake = _backend(rows)
    auth = AuthService(login_limiter=RateLimiter(limit=limit, window=300))
    return auth, fake


def _tok(auth: AuthService, username: str, password: str) -> str:
    #直接登录取令牌
    code, p = auth.login(username, password, client_ip="127.0.0.1")
    assert code == 200, f"登录失败 {username}: {p}"
    return f"Bearer {p['token']}"


def _agent(feishu=None, table_id=None):
    #构造离线智能体
    llm = LLMAdapter(MockProvider(canned="[MOCK] 依据报销制度解答。"))
    ret = Retriever(CORPUS)
    return XiaoCaiAgent(llm, ret, feishu=feishu, table_id=table_id)


def test_health():
    #健康检查返回 ok
    code, p = dispatch("GET", "/health", {}, _agent(), SessionStore())
    assert code == 200 and p["status"] == "ok"
    print("[PASS] test_health")


def test_chat_consult_offline():
    #离线咨询走 A 分支
    a = _agent()
    s = SessionStore()
    auth, _ = _setup()
    tok = _tok(auth, *EMP)
    code, p = dispatch("POST", "/chat", {"session_id": "c1", "text": "差旅费住宿标准是多少"},
                       a, s, auth=auth, token=tok)
    assert code == 200 and p["intent"] == "A"
    print("[PASS] test_chat_consult_offline")


def test_chat_fill_merges_draft():
    #两轮补全字段并保持 B 分支
    a = _agent()
    s = SessionStore()
    auth, _ = _setup()
    tok = _tok(auth, *EMP)
    code, p = dispatch("POST", "/chat", {"session_id": "f1", "text": "我要报销 张三 研发部 差旅费"},
                       a, s, auth=auth, token=tok)
    assert code == 200 and p["intent"] == "B"
    assert p["draft"] and p["draft"].get("department") == "研发部"
    code, p = dispatch("POST", "/chat", {"session_id": "f1", "text": "800 有发票 上海 事由是出差谈客户"},
                       a, s, auth=auth, token=tok)
    assert p["intent"] == "B"
    assert p["draft"]["amount"] == 800 and p["draft"]["city"] == "上海"
    assert p["draft"].get("await_confirm"), "字段齐了应进入确认态"
    print("[PASS] test_chat_fill_merges_draft")


def test_reimburse_endpoint():
    #报销接口经主服务器写入 身份由服务端盖章
    auth, fake = _setup()
    tok = _tok(auth, *EMP)
    code, p = dispatch("POST", "/reimburse",
                       {"department": "研发部", "rtype": "差旅费", "amount": 800,
                        "reason": "北京出差", "has_invoice": "有票", "city": "上海"},
                       _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 200 and p["record_id"], p
    assert p["status"] == "待审核" and p["via"] == "main-server"
    rec = fake.submitted[-1]
    assert rec["applicant"] == "张三" and rec["employeeId"] == "E0001", rec
    assert rec["city"] == "上海" and rec["department"] == "研发部", rec
    print("[PASS] test_reimburse_endpoint")


def test_reimburse_ignores_spoofed_applicant():
    #接口不再接收申请人 想替人报也没入口
    auth, fake = _setup()
    tok = _tok(auth, *EMP)
    code, p = dispatch("POST", "/reimburse",
                       {"applicant": "李四", "department": "研发部", "rtype": "差旅费",
                        "amount": 800, "has_invoice": "有票", "city": "上海"},
                       _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 200 and p["record_id"], p
    assert fake.submitted[-1]["applicant"] == "张三", fake.submitted[-1]
    print("[PASS] test_reimburse_ignores_spoofed_applicant")


def test_reimburse_backend_down_503():
    #主服务器不可用时 503 且不写飞书
    auth, fake = _setup()
    tok = _tok(auth, *EMP)
    fake.fail = "unavailable"
    code, p = dispatch("POST", "/reimburse",
                       {"department": "研发部", "rtype": "差旅费", "amount": 800,
                        "has_invoice": "有票", "city": "上海"},
                       _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 503 and "主服务器" in p["error"], p
    assert not fake.submitted
    print("[PASS] test_reimburse_backend_down_503")


def test_reimburse_rejected_400():
    #业务拒绝回 400 并透出服务端原因
    auth, fake = _setup()
    tok = _tok(auth, *EMP)
    fake.fail = "rejected"
    code, p = dispatch("POST", "/reimburse",
                       {"department": "研发部", "rtype": "差旅费", "amount": 800},
                       _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 400 and "事由" in p["error"], p
    print("[PASS] test_reimburse_rejected_400")


def test_progress_endpoint():
    #财务可查任意人进度
    rows = [make_row("r1", "小新新", "E0009", "BX-000013", status="审核中")]
    auth, _ = _setup(rows=rows)
    tok = _tok(auth, *FIN)
    code, p = dispatch("POST", "/progress", {"applicant": "小新新"}, _agent(), SessionStore(),
                       auth=auth, token=tok)
    assert code == 200 and p["intent"] == "D"
    #明细在 records 里 答案只给摘要
    assert p["records"] and p["records"][0]["申请人"] == "小新新", p["records"]
    assert "查到 1 条" in p["answer"], p["answer"]
    print("[PASS] test_progress_endpoint")


def test_progress_requires_param():
    #缺参数返回 400
    auth, _ = _setup()
    tok = _tok(auth, *FIN)
    code, p = dispatch("POST", "/progress", {}, _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 400
    print("[PASS] test_progress_requires_param")


def test_progress_employee_forced_self():
    #员工查他人只能看到自己的单
    rows = [make_row("r1", "张三", "E0001", "BX-000013"),
            make_row("r2", "李四", "E0002", "BX-000014")]
    auth, _ = _setup(rows=rows)
    tok = _tok(auth, *EMP)
    code, p = dispatch("POST", "/progress", {"applicant": "李四"}, _agent(), SessionStore(),
                       auth=auth, token=tok)
    assert code == 200 and p["intent"] == "D"
    nos = [r["单号"] for r in p["records"]]
    assert nos == ["BX-000013"], nos
    assert "BX-000014" not in nos, "员工不应看到他人单据"
    print("[PASS] test_progress_employee_forced_self")


def test_not_found():
    #未知路径返回 404
    auth, _ = _setup()
    tok = _tok(auth, *EMP)
    code, p = dispatch("GET", "/nope", {}, _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 404
    print("[PASS] test_not_found")


def test_auth_wall_fail_closed():
    #缺令牌 401；认证未配置 503
    a = _agent()
    s = SessionStore()
    auth, _ = _setup()
    code, p = dispatch("POST", "/chat", {"session_id": "w1", "text": "你好"}, a, s, auth=auth, token="")
    assert code == 401, "缺 token 应 401"
    code, p = dispatch("POST", "/chat", {"session_id": "w1", "text": "你好"},
                       a, s, auth=auth, token="Bearer fake.token.here")
    assert code == 401, "伪造 token 应 401"
    #认证未配置是配置错误 用 503 区分于「该重新登录」
    code, p = dispatch("POST", "/chat", {"session_id": "w1", "text": "你好"}, a, s, auth=None)
    assert code == 503 and "BACKEND_BASE_URL" in p["error"], p
    #健康检查免认证
    code, p = dispatch("GET", "/health", {}, a, s, auth=None)
    assert code == 200
    print("[PASS] test_auth_wall_fail_closed")


def test_login_flow():
    #登录发主服务器令牌 查身份还原
    a = _agent()
    auth, _ = _setup()
    code, p = dispatch("POST", "/login", {"username": EMP[0], "password": EMP[1]},
                       a, SessionStore(), auth=auth, client_ip="1.2.3.4")
    assert code == 200 and p["token"] and p["session_id"]
    assert p["user"]["角色"] == "员工" and p["user"]["姓名"] == "张三"
    assert p["user"]["工号"] == "E0001"
    assert p["perms"] and "invoice.submit" in p["perms"], p["perms"]
    assert "signKey" not in p, "签名密钥不得下发浏览器"
    tok = f"Bearer {p['token']}"
    code, p = dispatch("GET", "/me", {}, a, SessionStore(), auth=auth, token=tok)
    assert code == 200
    assert p["user"]["用户名"] == "employee" and p["user"]["角色"] == "员工"
    assert "department" not in p["user"], "主服务器账号没有部门字段"
    print("[PASS] test_login_flow")


def test_login_rejections():
    #登录各类失败分支
    a = _agent()
    auth, _ = _setup()
    code, p = dispatch("POST", "/login", {"username": EMP[0], "password": "bad"},
                       a, SessionStore(), auth=auth)
    assert code == 401 and "口令" in p["error"], p
    code, p = dispatch("POST", "/login", {"username": "nobody", "password": "bad"},
                       a, SessionStore(), auth=auth)
    assert code == 401 and "口令" in p["error"], "不存在与错密码提示必须一致（防枚举）"
    code, p = dispatch("POST", "/login", {"username": "", "password": "x"},
                       a, SessionStore(), auth=auth)
    assert code == 400
    code, p = dispatch("POST", "/login", {"username": EMP[0], "password": EMP[1]},
                       a, SessionStore(), auth=None)
    assert code == 503, "auth 未配置时 /login 应 503"
    print("[PASS] test_login_rejections")


def test_login_backend_down_503():
    #主服务器不可用是 503 不是口令错误
    a = _agent()
    auth, fake = _setup()
    fake.fail = "unavailable"
    code, p = dispatch("POST", "/login", {"username": EMP[0], "password": EMP[1]},
                       a, SessionStore(), auth=auth)
    assert code == 503 and "主服务器" in p["error"], p
    print("[PASS] test_login_backend_down_503")


def test_login_rate_limit_only_failures():
    #登录限流只计失败次数
    a = _agent()
    auth, _ = _setup(limit=2)
    code, p = dispatch("POST", "/login", {"username": EMP[0], "password": EMP[1]},
                       a, SessionStore(), auth=auth)
    assert code == 200, "正常登录不消耗限流配额"
    for _ in range(2):
        dispatch("POST", "/login", {"username": EMP[0], "password": "bad"},
                 a, SessionStore(), auth=auth)
    code, p = dispatch("POST", "/login", {"username": EMP[0], "password": EMP[1]},
                       a, SessionStore(), auth=auth)
    assert code == 429, "失败达上限后即使口令正确也应拒绝（防爆破）"
    code, p = dispatch("POST", "/login", {"username": FIN[0], "password": FIN[1]},
                       a, SessionStore(), auth=auth)
    assert code == 200, "其他用户不受影响"
    print("[PASS] test_login_rate_limit_only_failures")


def test_sid_binding_403():
    #同会话换用户返回 403
    a = _agent()
    s = SessionStore()
    auth, _ = _setup()
    tok_a = _tok(auth, *EMP)
    tok_b = _tok(auth, *FIN)
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


def test_logout_clears_gateway_cache():
    #登出只清网关缓存；主服务器不做吊销 令牌被拒后必须重新登录
    a = _agent()
    s = SessionStore()
    auth, fake = _setup()
    tok = _tok(auth, *EMP)
    code, p = dispatch("POST", "/chat", {"session_id": "lo1", "text": "你好"},
                       a, s, auth=auth, token=tok)
    assert code == 200
    code, p = dispatch("POST", "/logout", {}, a, s, auth=auth, token=tok)
    assert code == 200
    #清缓存后会重新向主服务器校验：主服务器判失效则 401
    fake.fail = "unauthorized"
    code, p = dispatch("POST", "/chat", {"session_id": "lo1", "text": "你好"},
                       a, s, auth=auth, token=tok)
    assert code == 401, "清缓存且主服务器判失效后应 401"
    print("[PASS] test_logout_clears_gateway_cache")


def test_chat_fill_type_word_not_mistaken_for_name():
    #回归：说「我要报销交通费」不该被当成替人报账
    auth, fake = _setup()
    tok = _tok(auth, *EMP)
    code, p = dispatch("POST", "/chat", {"session_id": "tw1", "text": "我要报销交通费"},
                       _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 200 and p["intent"] == "B", p
    assert "只能由本人提交" not in p["answer"], p["answer"]
    assert p["draft"] and p["draft"]["applicant"] == "张三", p.get("draft")
    assert not fake.submitted, "此时还在追问 不该提交"
    print("[PASS] test_chat_fill_type_word_not_mistaken_for_name")


def test_chat_rejects_reporting_for_others():    #对话里替人报账被直接拦截
    auth, fake = _setup()
    tok = _tok(auth, *EMP)
    code, p = dispatch("POST", "/chat",
                       {"session_id": "imp1", "text": "我要报销 李四 研发部 招待费 800 有发票"},
                       _agent(), SessionStore(), auth=auth, token=tok)
    assert code == 200 and "只能由本人提交" in p["answer"], p
    assert not fake.submitted, "不得提交"
    print("[PASS] test_chat_rejects_reporting_for_others")


def test_http_server_smoke():
    #TestClient 冒烟测 HTTP
    from fastapi.testclient import TestClient
    auth, _ = _setup()
    apimod.AUTH = auth
    apimod.AGENT = _agent()   #用离线 agent 避免依赖本机模型服务
    tok = _tok(auth, *EMP)
    client = TestClient(app)
    #健康检查
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    #跨域预检返回 200
    r = client.options(
        "/chat",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers
    #未带令牌返回 401
    r = client.post("/chat", json={"session_id": "h1", "text": "差旅费住宿标准是多少"})
    assert r.status_code == 401
    #带令牌返回规范 JSON
    r = client.post("/chat", json={"session_id": "h1", "text": "差旅费住宿标准是多少"},
                    headers={"Authorization": tok})
    assert r.status_code == 200
    assert r.json()["intent"] == "A"
    #超长文本被拒 422
    long_text = "报" * 3000
    r = client.post("/chat", json={"text": long_text}, headers={"Authorization": tok})
    assert r.status_code == 422
    print("[PASS] test_http_server_smoke")


import base64 as _b64
from models.ocr import OCRResult
from api.main import _decode_image, _strip_data_url  # noqa: E402

_JPEG_B64 = _b64.b64encode(b"\xff\xd8\xff\xe0FAKEJPGDATA").decode()
_JPEG_BYTES = b"\xff\xd8\xff\xe0FAKEJPGDATA"


class FakeOcrEngine:
    #离线假 OCR 引擎
    name = "fake-ocr"

    def __init__(self, result=None, error=None):
        #保存返回值与待抛异常
        self.result = result
        self.error = error
        self.calls = []

    def extract(self, *, image_b64, content_type="image/jpeg", filename=""):
        #记录入参后返回结果
        self.calls.append({"len": len(image_b64), "mime": content_type, "filename": filename})
        if self.error is not None:
            raise self.error
        return self.result


def _ocr_result():
    #构造固定识别结果
    return OCRResult(
        fields={"amount": 800.0, "invoice_date": "2026/10/1", "invoice_no": "INV1",
                "seller": "某酒店", "rtype": "差旅费", "city": "北京"},
        warnings=[], usage={"total_tokens": 384}, model="deepseek-flash",
        provider="deepseek-ocr", latency_ms=120,
    )


def test_ocr_requires_auth():
    #OCR 接口必须带令牌
    code, p = dispatch("POST", "/ocr", {"image_b64": _JPEG_B64}, _agent(), SessionStore())
    assert code == 503, "认证未配置时应 fail-closed"
    auth, _ = _setup()
    code, p = dispatch("POST", "/ocr", {"image_b64": _JPEG_B64}, _agent(), SessionStore(), auth=auth)
    assert code == 401, "缺令牌应 401"
    print("[PASS] test_ocr_requires_auth")


def test_ocr_rejects_non_image():
    #非图片返回 400
    auth, _ = _setup()
    tok = _tok(auth, *EMP)
    #能解码但无图片魔数
    code, p = dispatch("POST", "/ocr", {"session_id": "ocr1", "image_b64": "aGVsbG8="},
                       _agent(), SessionStore(), auth=auth, token=tok,
                       ocr_engine=FakeOcrEngine(_ocr_result()))
    assert code == 400 and "invalid image" in p["error"]
    print("[PASS] test_ocr_rejects_non_image")


def test_ocr_success_with_mock_engine():
    #假引擎识别成功返回字段
    auth, _ = _setup()
    tok = _tok(auth, *EMP)
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
    #引擎抛错时返回 502
    from models.ocr import OCRError
    auth, _ = _setup()
    tok = _tok(auth, *EMP)
    eng = FakeOcrEngine(error=OCRError("没识别出报销要素，请上传相关发票，或更清晰的支付凭证截图"))
    code, p = dispatch("POST", "/ocr", {"session_id": "ocr3", "image_b64": _JPEG_B64},
                       _agent(), SessionStore(), auth=auth, token=tok, ocr_engine=eng)
    assert code == 502 and "识别失败" in p["error"], p
    assert "没识别出报销要素" in p["error"], p
    print("[PASS] test_ocr_engine_error_502")


def test_ocr_not_configured_503():
    #未配 OCR 时返回 503
    auth, _ = _setup()
    tok = _tok(auth, *EMP)
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
    #图片解码与魔数判定
    assert _strip_data_url("data:image/png;base64,AAAA") == "AAAA"
    assert _strip_data_url("AAAA") == "AAAA"
    #按魔数判定类型
    raw, mime = _decode_image(_JPEG_B64, 1024 * 1024)
    assert mime == "image/jpeg" and raw == _JPEG_BYTES
    webp = _b64.b64encode(b"RIFF0000WEBPVP8 ").decode()
    _, mime = _decode_image(webp, 1024 * 1024)
    assert mime == "image/webp"
    fake_webp = _b64.b64encode(b"RIFF0000XXXX").decode()  #RIFF 但非 WEBP
    assert _decode_image(fake_webp, 1024 * 1024) == (None, None)
    #超过大小上限
    big = _b64.b64encode(b"\xff\xd8\xff" + b"\x00" * 300).decode()
    assert _decode_image(big, 100) == (None, None)
    print("[PASS] test_decode_image_helpers")


def test_chat_confirm_with_image_attaches_invoice():
    #确认提交后 单据由主服务器落库 图片再写进附件列
    mf = MockFeishu()
    auth, fake = _setup()
    tok = _tok(auth, *EMP)
    s = SessionStore()
    agent = _agent(feishu=mf, table_id="tblX")
    #识别字段随首句并入草稿
    code, p = dispatch("POST", "/chat",
                       {"session_id": "m9e2e", "text": "我是张三，研发部，差旅费 800元 有发票 上海 事由是出差谈客户",
                        "invoice_no": "INV1", "invoice_seller": "某酒店"},
                       agent, s, auth=auth, token=tok)
    assert code == 200 and p["intent"] == "B"
    assert p["draft"].get("invoice_no") == "INV1"
    #确认提交并捎发票图
    code2, p2 = dispatch("POST", "/chat",
                         {"session_id": "m9e2e", "text": "确认提交",
                          "image_b64": _JPEG_B64, "image_mime": "image/jpeg"},
                         agent, s, auth=auth, token=tok)
    assert code2 == 200 and p2["record_id"], p2
    assert fake.submitted[0]["invoice_info"] == "INV1；某酒店", fake.submitted[0]
    assert mf.uploaded and mf.uploaded[0] == ("发票.jpg", _JPEG_BYTES, "image/jpeg")
    assert mf.updated and mf.updated[0][1] == p2["record_id"]
    assert "已存档" in p2["answer"], p2["answer"]
    print("[PASS] test_chat_confirm_with_image_attaches_invoice")


def test_chat_invalid_image_400():
    #聊天带非法图片返回 400
    auth, _ = _setup()
    tok = _tok(auth, *EMP)
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
    test_reimburse_ignores_spoofed_applicant()
    test_reimburse_backend_down_503()
    test_reimburse_rejected_400()
    test_progress_endpoint()
    test_progress_requires_param()
    test_progress_employee_forced_self()
    test_not_found()
    test_auth_wall_fail_closed()
    test_login_flow()
    test_login_rejections()
    test_login_backend_down_503()
    test_login_rate_limit_only_failures()
    test_sid_binding_403()
    test_logout_clears_gateway_cache()
    test_chat_fill_type_word_not_mistaken_for_name()
    test_chat_rejects_reporting_for_others()
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
