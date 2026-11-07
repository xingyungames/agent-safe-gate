#编排层离线单测 不联网不调付费接口
#身份与权限来自主服务器（离线替身），单据读写全部经主服务器
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api import backend_client
from mock_backend import FakeBackend, install, make_row, make_token, make_user
from models.actor import build_actor
from models.retriever import Retriever, Chunk
from models.llm import LLMAdapter, MockProvider
from models.agent import XiaoCaiAgent, normalize_department


CORPUS = [
    Chunk(text="差旅费报销需提供出差审批单、交通票据、住宿发票。单人住宿标准不超过300元/晚。",
          source="报销制度.md"),
    Chunk(text="招待费报销需提供发票、消费清单。单次超过2000元须部门总监审批。",
          source="报销制度.md"),
    Chunk(text="填单规则：报销单必须填写事由、报销类型、金额、申请人工号；发票抬头须与公司一致，否则退回。",
          source="填单规则.md"),
]

IMG_B64 = "aGVsbG8="  #hello 的 base64 仅用于附件路径


#构造测试用的编排 agent
def _agent(feishu=None, table_id="tblX"):
    llm = LLMAdapter(MockProvider(canned="[MOCK] 回复内容"))
    ret = Retriever(CORPUS)
    return XiaoCaiAgent(
        llm, ret, default_category="报销制度",
        feishu=feishu, table_id=table_id,
    )


#构造一个登录主体
def _actor(role="employee", username="employee", name="张三", eid="E0001"):
    user = make_user(username, role, name, eid)
    return build_actor(user, token=make_token(username, role), sign_key="sk-test")


#装上离线主服务器并返回它
def _backend(rows=None):
    fake = FakeBackend(rows=rows)
    install(fake, backend_client)
    return fake


class MockFeishu:
    #离线飞书客户端 只用于发票附件列

    def __init__(self, has_col=True, fail=False):
        self.has_col = has_col
        self.fail = fail
        self.uploaded = []
        self.updated = []

    def upload_attachment(self, name, data, mime):
        self.uploaded.append((name, data, mime))
        if self.fail:
            raise RuntimeError("上传失败")
        return "ftk_1"

    def field_names(self, table_id):
        return {"发票附件", "发票信息"} if self.has_col else {"发票信息"}

    def update_record(self, table_id, record_id, fields):
        self.updated.append((record_id, fields))
        return {"record_id": record_id}


# ---------- 意图识别 ----------

def test_classify_intent_consult():
    a = _agent()
    assert a.classify_intent("差旅费住宿标准是多少") == "A"
    print("[PASS] test_classify_intent_consult")


def test_classify_intent_fill():
    a = _agent()
    assert a.classify_intent("我要报销差旅费") == "B"
    print("[PASS] test_classify_intent_fill")


def test_classify_intent_compliance():
    a = _agent()
    assert a.classify_intent("这笔招待费超标了吗") == "C"
    print("[PASS] test_classify_intent_compliance")


def test_classify_intent_progress():
    a = _agent()
    assert a.classify_intent("我的报销单到哪一步了") == "D"
    print("[PASS] test_classify_intent_progress")


# ---------- A 分支 ----------

def test_consult_offline():
    _backend()
    a = _agent()
    res = a.route("差旅费住宿标准是多少", actor=_actor())
    assert res["intent"] == "A"
    assert res["answer"] == "[MOCK] 回复内容"
    print("[PASS] test_consult_offline")


# ---------- 字段抽取 ----------

def test_extract_fields_basic():
    f = XiaoCaiAgent._extract_fields("我是张三，研发部，招待费 800，有发票，事由是客户餐叙")
    assert f["applicant"] == "张三", f
    assert f["department"] == "研发部", f
    assert f["category"] == "招待费", f
    assert f["amount"] == 800, f
    #口述「有发票」不再计为已开票：必须由 OCR 证据才能置「有票」
    assert f["has_invoice"] == "未提", f
    print("[PASS] test_extract_fields_basic")


def test_extract_department_routes_to_enum():
    assert normalize_department("研发部门") == "研发部"
    assert normalize_department("技术部") == "信息技术部"
    assert normalize_department("hr") == "人力资源部"
    assert normalize_department("火星部") == ""
    print("[PASS] test_extract_department_routes_to_enum")


def test_extract_city_routes_to_whitelist():
    f = XiaoCaiAgent._extract_fields("我要报销差旅费 800 上海")
    assert f["city"] == "上海", f
    print("[PASS] test_extract_city_routes_to_whitelist")


def test_extract_chinese_date_no_year():
    f = XiaoCaiAgent._extract_fields("差旅费 800 9月10号")
    assert f["date"], f
    print("[PASS] test_extract_chinese_date_no_year")


def test_extract_special_category_blocks():
    f = XiaoCaiAgent._extract_fields("我要报团建费500")
    assert f["special"], f
    print("[PASS] test_extract_special_category_blocks")


def test_extract_applicant_no_garbage():
    f = XiaoCaiAgent._extract_fields("我要报销")
    assert f["applicant"] == "", f
    print("[PASS] test_extract_applicant_no_garbage")


# ---------- 合规阈值 ----------

def test_compliance_entertainment_over_limit():
    a = _agent()
    level, msg = a._compliance_check({"category": "招待费", "amount": 3000, "has_invoice": "有票"})
    assert level == "风险", (level, msg)
    assert "2000" in msg, msg
    print("[PASS] test_compliance_entertainment_over_limit")


def test_compliance_no_invoice_blocks():
    a = _agent()
    level, msg = a._compliance_check({"category": "招待费", "amount": 800, "has_invoice": "无票"})
    assert level == "阻断", (level, msg)
    print("[PASS] test_compliance_no_invoice_blocks")


def test_compliance_office_supplies_over_limit():
    a = _agent()
    level, msg = a._compliance_check({"category": "办公用品", "amount": 6000, "has_invoice": "有票"})
    assert level == "风险" and "5000" in msg, (level, msg)
    print("[PASS] test_compliance_office_supplies_over_limit")


def test_compliance_travel_city_cap():
    a = _agent()
    level, msg = a._compliance_check(
        {"category": "差旅费", "amount": 800, "has_invoice": "有票", "city": "上海"})
    assert level == "风险" and "500" in msg, (level, msg)
    print("[PASS] test_compliance_travel_city_cap")


# ---------- B 填单 ----------

def test_fill_missing_asks_when_incomplete():
    _backend()
    a = _agent()
    res = a.route("我要填报销单", actor=_actor())
    assert res["intent"] == "B"
    assert "部门" in res["answer"] or "报销类型" in res["answer"], res["answer"]
    print("[PASS] test_fill_missing_asks_when_incomplete")


def test_fill_waits_for_confirm_before_writing():
    fake = _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=_actor(), invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    assert res["draft"] and res["draft"].get("await_confirm"), res
    assert not fake.submitted, "未确认前不得提交"
    print("[PASS] test_fill_waits_for_confirm_before_writing")


def test_fill_confirm_submits_via_backend():
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert res2["record_id"], res2
    assert "BX-000123" in res2["answer"], res2["answer"]
    assert len(fake.submitted) == 1, fake.submitted
    rec = fake.submitted[0]
    assert rec["department"] == "研发部" and rec["rtype"] == "招待费"
    assert rec["amount"] == 800 and rec["has_invoice"] == "有票"
    assert rec["applicant"] == "张三" and rec["employeeId"] == "E0001"
    assert rec["reason"] == "客户招待", rec["reason"]
    print("[PASS] test_fill_confirm_submits_via_backend")


def test_extract_reason_basic():
    #事由要剥掉引导词里的「是/为」并按字段名截断
    cases = {
        "事由是报销车费": "报销车费",
        "事由：客户招待餐费": "客户招待餐费",
        "用途是出差谈客户": "出差谈客户",
        "因为请客户吃饭": "请客户吃饭",
        "备注是招待客户": "招待客户",
        "事由是客户餐叙金额800城市北京": "客户餐叙",
        "事由为发票OCR冒烟测试": "发票OCR冒烟测试",
    }
    for text, expected in cases.items():
        got = XiaoCaiAgent._extract_fields(text)["reason"]
        assert got == expected, (text, got, expected)
    print("[PASS] test_extract_reason_basic")


def test_fill_asks_reason_when_missing():
    #缺事由要追问 不能送去主服务器让它 400
    fake = _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票", actor=_actor(), invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    assert res["intent"] == "B" and res["draft"].get("await_reason"), res
    assert "事由" in res["answer"], res["answer"]
    assert not fake.submitted
    print("[PASS] test_fill_asks_reason_when_missing")


def test_fill_reason_from_single_sentence_after_ask():
    #追问过事由后 用户只回一句短语也要收下
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    res2 = a.route("客户招待餐费", base_draft=res["draft"], actor=actor)
    assert res2["intent"] == "B", res2
    assert res2["draft"]["reason"] == "客户招待餐费", res2["draft"]
    assert res2["draft"].get("await_confirm"), "补齐后应进入确认态"
    res3 = a.route("确认提交", base_draft=res2["draft"], actor=actor)
    assert res3["record_id"], res3
    assert fake.submitted[0]["reason"] == "客户招待餐费", fake.submitted[0]
    print("[PASS] test_fill_reason_from_single_sentence_after_ask")


def test_reason_excludes_already_known_fields():
    #回归：一句话里同时补多个字段时 事由只保留剩下的那部分
    _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三", actor=actor)
    assert res["draft"].get("await_reason"), res["draft"]
    res2 = a.route("财务部 交通费 200元 通勤", base_draft=res["draft"], actor=actor)
    assert res2["draft"]["department"] == "财务部", res2["draft"]
    assert res2["draft"]["category"] == "交通费", res2["draft"]
    assert res2["draft"]["amount"] == 200, res2["draft"]
    assert res2["draft"]["reason"] == "通勤", res2["draft"]
    print("[PASS] test_reason_excludes_already_known_fields")


def test_reason_not_filled_when_only_field_words():
    #整句都是别的字段时 不能把整句硬塞进事由 应继续追问
    _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三", actor=actor)
    res2 = a.route("财务部 交通费 200元", base_draft=res["draft"], actor=actor)
    assert not res2["draft"].get("reason"), res2["draft"]
    assert "事由" in res2["answer"], res2["answer"]
    assert "不用重复" in res2["answer"], res2["answer"]
    print("[PASS] test_reason_not_filled_when_only_field_words")


def test_fill_confirm_without_reason_does_not_call_backend():
    #确认时仍没事由 只能继续追问 绝不打主服务器
    fake = _backend()
    fake.fail = "rejected"
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert not res2["record_id"], res2
    assert "事由" in res2["answer"], res2["answer"]
    assert not fake.submitted, "缺少必填项时不应提交"
    print("[PASS] test_fill_confirm_without_reason_does_not_call_backend")


def test_fill_rejects_reporting_for_others():
    fake = _backend()
    a = _agent()
    res = a.route("我要报销 李四 研发部 招待费 800 有发票 事由是客户招待", actor=_actor(), invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    assert "只能由本人提交" in res["answer"], res["answer"]
    assert "李四" in res["answer"], res["answer"]
    assert res.get("draft") is None, res
    assert not fake.submitted, "替人报账不得提交"
    print("[PASS] test_fill_rejects_reporting_for_others")


def test_fill_rejects_help_someone_pattern():
    #「帮X报」句式同样要拦
    fake = _backend()
    a = _agent()
    res = a.route("帮李四报一笔差旅费 800 有发票 上海", actor=_actor(), invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    assert "只能由本人提交" in res["answer"], res["answer"]
    assert not fake.submitted
    print("[PASS] test_fill_rejects_help_someone_pattern")


def test_fill_no_false_intercept_on_expense_words():
    #回归：费用词不能被当成人名而误拦本人填单
    fake = _backend()
    a = _agent()
    for text in ("我要报销交通费", "我要报销", "报销差旅费800 有发票"):
        res = a.route(text, actor=_actor())
        assert "只能由本人提交" not in res["answer"], (text, res["answer"])
        assert not fake.submitted, "此时还没到确认，不该提交"
    print("[PASS] test_fill_no_false_intercept_on_expense_words")


def test_extract_applicant_ignores_expense_words():
    #抽取层就不能把费用/动作词当人名
    for text in ("我要报销交通费", "我要报销", "报销差旅费800", "帮我看下报销"):
        f = XiaoCaiAgent._extract_fields(text)
        assert f["applicant"] == "", (text, f)
    #真实姓名仍要抽得到
    assert XiaoCaiAgent._extract_fields("我是张三，研发部")["applicant"] == "张三"
    assert XiaoCaiAgent._extract_fields("我要报销 张三 研发部")["applicant"] == "张三"
    print("[PASS] test_extract_applicant_ignores_expense_words")


def test_fill_no_invoice_blocked():
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 没发票 事由是客户招待", actor=actor)
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert not res2["record_id"], res2
    assert "发票" in res2["answer"], res2["answer"]
    assert not fake.submitted, "无票不得提交"
    print("[PASS] test_fill_no_invoice_blocked")


def test_fill_untipped_invoice_blocked():
    #未提不再放行：必须先补发票或支付记录
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 事由是客户招待", actor=actor)
    assert "票据" in res["answer"], res["answer"]
    assert not res["draft"].get("await_confirm"), "未提不应进入确认态"
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert not res2["record_id"], res2
    assert not fake.submitted, "未提不得提交"
    print("[PASS] test_fill_untipped_invoice_blocked")


def test_fill_backend_rejects_shows_reason():
    fake = _backend()
    fake.fail = "rejected"
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert not res2["record_id"], res2
    assert "没有提交成功" in res2["answer"], res2["answer"]
    print("[PASS] test_fill_backend_rejects_shows_reason")


def test_fill_backend_down_no_direct_write():
    fake = _backend()
    fake.fail = "unavailable"
    feishu = MockFeishu()
    a = _agent(feishu=feishu)
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert "主服务器暂时不可用" in res2["answer"], res2["answer"]
    assert not feishu.uploaded and not feishu.updated, "不得直连飞书"
    print("[PASS] test_fill_backend_down_no_direct_write")


def test_fill_bogus_department_asked_not_submitted():
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 火星部 招待费 800 有发票 事由是客户招待", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    #部门归一失败应为空 进入追问而不是直接提交
    assert not res["draft"] or not res["draft"].get("department"), res
    assert not fake.submitted
    print("[PASS] test_fill_bogus_department_asked_not_submitted")


def test_reason_not_polluted_by_type_amount():
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 差旅费 800 有发票", actor=_actor(), invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    assert res["draft"]["reason"] in ("", None), res["draft"]
    print("[PASS] test_reason_not_polluted_by_type_amount")


def test_fill_sticky_keeps_branch():
    _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    res2 = a.route("办公费", base_draft=res["draft"], actor=actor)
    assert res2["intent"] == "B", res2
    print("[PASS] test_fill_sticky_keeps_branch")


def test_invoice_fields_merged_into_draft():
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=_actor(),
                  invoice_fields={"invoice_no": "2311", "invoice_seller": "某商贸"})
    assert res["draft"].get("invoice_no") == "2311", res["draft"]
    assert res["draft"].get("invoice_seller") == "某商贸", res["draft"]
    assert res["draft"]["has_invoice"] == "有票", res["draft"]
    print("[PASS] test_invoice_fields_merged_into_draft")


def test_ocr_invoice_no_sets_has_invoice():
    #识别出发票号 即使没说票据状态 也必须是有票
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 办公用品 100 事由是买文具", actor=_actor(),
                  invoice_fields={"invoice_no": "2311"})
    assert res["draft"]["has_invoice"] == "有票", res["draft"]
    print("[PASS] test_ocr_invoice_no_sets_has_invoice")


def test_ocr_overrides_spoken_no_invoice():
    #口述「没发票」但传了票据 以图片为准 → 有票 且能正常提交
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 办公用品 100 没发票 事由是买文具", actor=actor,
                  invoice_fields={"invoice_no": "2311", "invoice_seller": "某商贸"})
    assert res["draft"]["has_invoice"] == "有票", res["draft"]
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert res2["record_id"], res2["answer"]
    assert fake.submitted[0]["has_invoice"] == "有票", fake.submitted[0]
    print("[PASS] test_ocr_overrides_spoken_no_invoice")


def test_no_ocr_keeps_untipped_status():
    #没传票据时 不应被误改成有票
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 办公用品 100 事由是买文具", actor=_actor())
    assert res["draft"]["has_invoice"] == "未提", res["draft"]
    print("[PASS] test_no_ocr_keeps_untipped_status")


# ---------- 附件（M9） ----------

def test_attach_uploaded_after_submit():
    _backend()
    feishu = MockFeishu()
    a = _agent(feishu=feishu)
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor,
                   image_b64=IMG_B64, image_mime="image/png")
    assert res2["record_id"], res2
    assert feishu.uploaded, "提交成功后才上传附件"
    assert feishu.updated and feishu.updated[0][1]["发票附件"], feishu.updated
    assert "已存档" in res2["answer"], res2["answer"]
    print("[PASS] test_attach_uploaded_after_submit")


def test_attach_skipped_without_image():
    _backend()
    feishu = MockFeishu()
    a = _agent(feishu=feishu)
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert not feishu.uploaded, "无图不应调用上传"
    print("[PASS] test_attach_skipped_without_image")


def test_attach_failure_does_not_block():
    _backend()
    feishu = MockFeishu(fail=True)
    a = _agent(feishu=feishu)
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor,
                   image_b64=IMG_B64, image_mime="image/jpeg")
    assert res2["record_id"], "附件失败不应影响单据"
    assert "未成功" in res2["answer"] or "未存档" in res2["answer"], res2["answer"]
    print("[PASS] test_attach_failure_does_not_block")


# ---------- C 合规预审 ----------

def test_c_blocked_for_employee():
    _backend()
    a = _agent()
    res = a.route("这笔招待费3000超标了吗", actor=_actor())
    assert res["intent"] == "C"
    assert "财务/管理员" in res["answer"], res["answer"]
    print("[PASS] test_c_blocked_for_employee")


def test_c_allowed_for_finance():
    _backend()
    a = _agent()
    res = a.route("这笔招待费3000超标了吗", actor=_actor("finance", "finance", "李四", "E0002"))
    assert res["intent"] == "C"
    assert "预审" in res["answer"], res["answer"]
    assert res["compliance"] == "风险", res
    print("[PASS] test_c_allowed_for_finance")


def test_c_never_creates_record():
    fake = _backend()
    a = _agent(feishu=MockFeishu())
    res = a.route("招待费 800 有发票 研发部 张三 合规吗", actor=_actor("finance", "finance", "李四", "E0002"))
    assert res["intent"] == "C", res
    assert not fake.submitted, "预审不得建单"
    assert res.get("record_id") in (None, ""), res
    print("[PASS] test_c_never_creates_record")


# ---------- D 进度查询 ----------

def _two_rows():
    return [
        make_row("r1", "张三", "E0001", "BX-000013", status="审核中"),
        make_row("r2", "李四", "E0002", "BX-000014", status="已通过"),
    ]


def test_d_employee_sees_only_self():
    _backend(_two_rows())
    a = _agent()
    res = a.route("查一下所有人的报销", actor=_actor())
    assert res["intent"] == "D"
    nos = [r["单号"] for r in res["records"]]
    assert nos == ["BX-000013"], nos
    assert "BX-000014" not in nos, "员工不应看到他人单据"
    print("[PASS] test_d_employee_sees_only_self")


def test_d_finance_sees_all():
    _backend(_two_rows())
    a = _agent()
    res = a.route("查一下报销", actor=_actor("finance", "finance", "李四", "E0002"))
    nos = [r["单号"] for r in res["records"]]
    assert set(nos) == {"BX-000013", "BX-000014"}, nos
    print("[PASS] test_d_finance_sees_all")


def test_d_bill_no_precise():
    _backend(_two_rows())
    a = _agent()
    res = a.route("帮我查 BX-000014", actor=_actor("finance", "finance", "李四", "E0002"))
    nos = [r["单号"] for r in res["records"]]
    assert nos == ["BX-000014"], nos
    assert "BX-000013" not in nos, "不应误命中 BX-000013"
    print("[PASS] test_d_bill_no_precise")


def test_d_answer_is_summary_only():
    #明细只在 records 里 答案里不再重复整张单
    _backend(_two_rows())
    a = _agent()
    res = a.route("查一下报销", actor=_actor("finance", "finance", "李四", "E0002"))
    ans = res["answer"]
    assert "查到 2 条" in ans, ans
    assert "BX-000013" not in ans and "BX-000014" not in ans, f"答案不该重复明细\n{ans}"
    print("[PASS] test_d_answer_is_summary_only")


def test_d_lists_newest_first():
    #回归：25 条记录时 最新那条必须排在最前（旧实现按行序取前 20 条 会看不到最新的）
    rows = [make_row(f"r{i}", "李四", "E0002", f"BX-{i:06d}",
                     submittedAt=str(1700000000000 + i * 60000))
            for i in range(1, 26)]
    _backend(rows)
    a = _agent()
    res = a.route("查一下报销", actor=_actor("finance", "finance", "李四", "E0002"))
    nos = [r["单号"] for r in res["records"]]
    assert nos[0] == "BX-000025", nos[:3]
    assert nos == sorted(nos, reverse=True), "应按提交时间倒序"
    assert len(nos) == 25, "25 条未超上限 应全部返回"
    print("[PASS] test_d_lists_newest_first")


def test_d_truncate_hint():
    #60 条记录时只展示最近 50 条 并明确告知总数
    rows = [make_row(f"r{i}", "李四", "E0002", f"BX-{i:06d}",
                     submittedAt=str(1700000000000 + i * 60000))
            for i in range(1, 61)]
    _backend(rows)
    a = _agent()
    res = a.route("查一下报销", actor=_actor("finance", "finance", "李四", "E0002"))
    assert len(res["records"]) == 50, len(res["records"])
    assert res["records"][0]["单号"] == "BX-000060", res["records"][0]
    ans = res["answer"]
    assert "共 60 条" in ans and "显示最近 50 条" in ans, ans[-200:]
    print("[PASS] test_d_truncate_hint")


def test_d_records_carry_all_fields():
    #后端只给结构化记录 明细由前端卡片渲染 空值也照常带上
    row = {
        "recordId": "r1", "billNo": "BX-000007", "applicant": "小新",
        "employeeId": "", "department": "市场部", "rtype": "交通费",
        "amount": 10, "reason": "打车", "hasInvoice": "",
        "city": "", "invoiceDate": "", "submittedAt": "",
        "compliance": "通过", "riskSummary": "",
        "status": "已驳回", "reviewer": "玩总",
        "reviewOpinion": "不通过，不属于公司报销", "reviewedAt": "1760000000000",
        "invoiceInfo": "2311；北京山柏云商贸中心",
        "raw": {"发票附件": '[{"file_token":"ftk","name":"发票.png"}]'},
    }
    _backend([row])
    a = _agent()
    res = a.route("查询小新的进度", actor=_actor("finance", "finance", "李四", "E0002"))
    assert len(res["records"]) == 1, res["records"]
    rec = res["records"][0]
    assert rec["单号"] == "BX-000007", rec
    assert rec["申请人"] == "小新" and rec["部门"] == "市场部", rec
    assert rec["报销类型"] == "交通费" and rec["金额"] == 10, rec
    assert rec["事由"] == "打车", rec
    assert rec["状态"] == "已驳回", rec
    assert rec["审核人"] == "玩总", rec
    assert rec["审批意见"] == "不通过，不属于公司报销", rec
    assert rec["发票信息"] == "2311；北京山柏云商贸中心", rec
    assert rec["合规结果"] == "通过", rec
    #答案里不再重复明细
    assert "BX-000007" not in res["answer"], res["answer"]
    print("[PASS] test_d_records_carry_all_fields")


def test_d_backend_down_friendly():
    _backend(_two_rows())
    fake = FakeBackend(rows=_two_rows())
    fake.fail = "unavailable"
    install(fake, backend_client)
    a = _agent()
    res = a.route("查一下我的报销", actor=_actor())
    assert "查询失败" in res["answer"], res["answer"]
    print("[PASS] test_d_backend_down_friendly")


def test_d_not_found_message():
    _backend([])
    a = _agent()
    res = a.route("查一下我的报销", actor=_actor())
    assert "没有找到" in res["answer"], res["answer"]
    print("[PASS] test_d_not_found_message")


# ---------- fail-closed 与引导 ----------

def test_route_requires_login():
    _backend()
    a = _agent()
    for text in ("我要报销 张三 研发部 招待费 800", "查一下我的报销", "这笔合规吗"):
        res = a.route(text)
        assert "请先登录" in res["answer"], (text, res)
    print("[PASS] test_route_requires_login")


def test_confirm_card_shows_risk():
    #确认卡提前给出风险提示 别等提交后才被打回
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 招待费 3000 有发票 事由是客户招待", actor=_actor(), invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    assert res["draft"].get("await_confirm"), res["draft"]
    assert "⚠️ 风险提示" in res["answer"], res["answer"]
    assert "2000" in res["answer"], res["answer"]
    print("[PASS] test_confirm_card_shows_risk")


def test_no_invoice_never_reaches_confirm():
    #无票根本不进确认卡 直接要求补票
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 招待费 800 没发票 事由是客户招待", actor=_actor())
    assert not res["draft"].get("await_confirm"), res["draft"]
    assert "确认提交" not in res["answer"], res["answer"]
    assert "发票" in res["answer"], res["answer"]
    print("[PASS] test_no_invoice_never_reaches_confirm")


def test_invoice_reminder_only_when_action_needed():
    #有票不重复提醒 无票提醒一次
    _backend()
    a = _agent()
    r1 = a.route("我要报销 张三 研发部 招待费 800 有发票", actor=_actor(), invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    assert r1["answer"].count("🧾") == 1, r1["answer"]
    r2 = a.route("我要报销 张三 研发部 招待费 800 没发票", actor=_actor())
    assert r2["answer"].count("🧾") == 2, r2["answer"]
    print("[PASS] test_invoice_reminder_only_when_action_needed")


def test_messages_avoid_internal_jargon():
    #不向用户暴露「上传飞书」这类内部实现细节
    _backend()
    a = _agent()
    r1 = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=_actor(), invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    r2 = a.route("我要报销 张三 研发部 招待费 800", actor=_actor())
    for ans in (r1["answer"], r2["answer"]):
        assert "上传飞书" not in ans, ans
    print("[PASS] test_messages_avoid_internal_jargon")


def test_untipped_asks_once_without_duplicate():
    #未提时票据引导只出现一次（已并入「还差」清单 不再单独重复）
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 交通费 200 事由是通勤", actor=_actor())
    ans = res["answer"]
    assert ans.count("🧾 还没记票据状态") == 1, ans
    assert "票据：未提" in ans, ans
    print("[PASS] test_untipped_asks_once_without_duplicate")


def test_only_invoiced_can_reach_confirm():
    #三种票据状态里只有「有票」能进确认卡
    _backend()
    a = _agent()
    cases = (
        ("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", True),
        ("我要报销 张三 研发部 招待费 800 没发票 事由是客户招待", False),
        ("我要报销 张三 研发部 招待费 800 事由是客户招待", False),
    )
    for text, expect in cases:
        #口述「有发票」不算数，必须有 OCR 证据才能进确认卡
        inv = {"invoice_no": "2311", "invoice_seller": "某商贸"} if "有发票" in text else None
        res = a.route(text, actor=_actor(), invoice_fields=inv)
        got = bool(res["draft"] and res["draft"].get("await_confirm"))
        assert got is expect, (text, res["answer"][:100])
    print("[PASS] test_only_invoiced_can_reach_confirm")


def test_do_submit_blocks_untipped():
    #第二道闸：绕过追问直接调提交 也必须拦住
    fake = _backend()
    a = _agent()
    f = {"applicant": "张三", "department": "研发部", "category": "招待费",
         "amount": 800, "reason": "客户招待", "has_invoice": "未提",
         "city": "", "date": ""}
    ans, rid, no = a._do_submit(f, "未提", actor=_actor())
    assert rid is None and not no, (ans, rid, no)
    assert "补上发票" in ans or "支付记录" in ans, ans
    assert not fake.submitted
    print("[PASS] test_do_submit_blocks_untipped")


def test_confirm_card_mentions_edit_path():
    #确认卡要告诉用户「说错了可以直接改」 并且改完确实生效
    _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 41 有发票 事由是客户喝奶茶", actor=actor, invoice_fields={"invoice_no":"2311","invoice_seller":"某商贸"})
    assert "有误请直接说明哪一项修改为什么" in res["answer"], res["answer"]
    #按提示改一项 → 草稿更新并重新出确认卡
    res2 = a.route("金额改成 50", base_draft=res["draft"], actor=actor)
    assert res2["draft"]["amount"] == 50, res2["draft"]
    assert "¥50" in res2["answer"], res2["answer"]
    print("[PASS] test_confirm_card_mentions_edit_path")


def test_after_submit_ask_guides_manual():
    #已提交后想补票/改单 不给假承诺 直接引导找财务或重提
    fake = _backend()
    a = _agent()
    for q in ("我已经提交了还能补发票吗", "能改单吗", "可以撤回吗"):
        res = a.route(q, actor=_actor())
        assert "联系财务" in res["answer"], (q, res["answer"])
        assert "重新上传材料齐全的报销单进飞书财务表" in res["answer"], (q, res["answer"])
        assert res["record_id"] is None, (q, res)
    assert not fake.submitted
    print("[PASS] test_after_submit_ask_guides_manual")


def test_fill_guide_button():
    #必须用精确触发词（UI 快捷按钮发的就是「填单引导」）
    _backend()
    a = _agent()
    res = a.route("填单引导", actor=_actor())
    assert res["intent"] == "B"
    assert "【必填 · 一项都不能少】" in res["answer"], res["answer"]
    assert "申请人" in res["answer"], res["answer"]
    #事由是必填 必须在必填区；票据要提示可传支付记录
    assert "📝 事由" in res["answer"], res["answer"]
    assert "支付记录" in res["answer"], res["answer"]
    print("[PASS] test_fill_guide_button")


# ---------- 问题3：口述「有发票」不再计为已开票 ----------

def test_spoken_invoice_claim_does_not_count():
    #只口述「有发票」但无任何 OCR 证据 → 仍是未提，且不能进确认卡
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=_actor())
    assert res["draft"]["has_invoice"] == "未提", res["draft"]
    assert not (res["draft"] and res["draft"].get("await_confirm")), "无证据不应到确认态"
    assert "发票" in res["answer"], res["answer"]
    print("[PASS] test_spoken_invoice_claim_does_not_count")


# ---------- 问题1：只传发票（无前置文字）按发票信息补齐 ----------

def test_only_upload_fills_from_ocr_and_asks_missing():
    _backend()
    a = _agent()
    res = a.route("📎 发票已上传，请帮我填单", actor=_actor(),
                  invoice_fields={"invoice_no": "2311", "invoice_seller": "某商贸",
                                  "amount": 50.98, "category": "办公用品",
                                  "date": "2024/4/7", "source": "invoice"})
    d = res["draft"]
    assert d["has_invoice"] == "有票", d
    assert d["amount"] == 50.98, d          #精度不丢
    assert d["category"] == "办公用品", d
    assert d["invoice_no"] == "2311", d
    #缺部门/事由 → 继续追问，而不是误判齐了
    assert "部门" in res["answer"] or "事由" in res["answer"], res["answer"]
    print("[PASS] test_only_upload_fills_from_ocr_and_asks_missing")


# ---------- 问题2：先文字后发票，以票为准 + 事由保留 ----------

def test_text_then_upload_conflict_uses_invoice():
    _backend()
    a = _agent()
    #先文字：800 差旅费 事由「出差住酒店」
    r1 = a.route("我要报销 张三 研发部 差旅费 800 事由是出差住酒店", actor=_actor())
    #再传发票：50.98 办公用品（金额/类型以票为准，事由保留）
    r2 = a.route("📎 发票已上传，请帮我填单", base_draft=r1["draft"], actor=_actor(),
                 invoice_fields={"invoice_no": "2311", "invoice_seller": "某商贸",
                                 "amount": 50.98, "category": "办公用品",
                                 "date": "2024/4/7", "source": "invoice"})
    d = r2["draft"]
    assert d["amount"] == 50.98, d
    assert d["category"] == "办公用品", d
    assert d["reason"] == "出差住酒店", d            #事由保留用户原值
    ans = r2["answer"]
    assert "不一致" in ans, ans                      #金额冲突提示
    assert "已按发票改为" in ans, ans                #类型冲突提示
    assert "事由是否仍适用" in ans, ans              #事由保留提示
    print("[PASS] test_text_then_upload_conflict_uses_invoice")


# ---------- 支付凭证（voucher）也算有票 ----------

def test_voucher_counts_as_invoiced():
    fake = _backend()
    a = _agent()
    #只传支付凭证（无发票号）→ 仍算有票
    res = a.route("📎 支付凭证已上传，请帮我填单", actor=_actor(),
                  invoice_fields={"amount": 88.5, "date": "2024/4/7",
                                  "category": "交通费", "source": "voucher"})
    d = res["draft"]
    assert d["has_invoice"] == "有票", d             #凭证也算有票
    assert d.get("invoice_source") == "voucher", d
    #补齐部门/事由后再确认提交
    res2 = a.route("我是张三 研发部 事由是打车", base_draft=d, actor=_actor())
    assert res2["draft"] and res2["draft"].get("await_confirm"), res2["answer"]
    actor = _actor()
    res3 = a.route("确认提交", base_draft=res2["draft"], actor=actor)
    assert res3["record_id"], res3["answer"]
    #提交后发票信息带「支付凭证：」前缀
    assert fake.submitted[0]["invoice_info"].startswith("支付凭证"), fake.submitted[0]
    print("[PASS] test_voucher_counts_as_invoiced")


# ---------- 票据状态迁移规则（用户 2026-10-25 拍板） ----------

def test_existing_ticket_not_downgraded_by_spoken_no_invoice():
    #有票 + 用户明说「没发票」→ 维持有票（必须有票才能提交，无票无意义）
    fake = _backend()
    a = _agent()
    actor = _actor()
    #第一轮：上传发票 → 有票
    r1 = a.route("我要报销 张三 研发部 办公用品 100 事由是买文具", actor=actor,
                 invoice_fields={"invoice_no": "2311", "invoice_seller": "某商贸",
                                 "amount": 100, "category": "办公用品", "source": "invoice"})
    assert r1["draft"]["has_invoice"] == "有票", r1["draft"]
    #第二轮：用户改口说「没发票」→ 票据状态不回退
    r2 = a.route("没发票", base_draft=r1["draft"], actor=actor)
    assert r2["draft"]["has_invoice"] == "有票", r2["draft"]
    #且要明确告诉用户按票据计算，不静默吞掉这句话
    assert "按「有票」计算" in r2["answer"], r2["answer"]
    #仍可正常提交
    r3 = a.route("确认提交", base_draft=r2["draft"], actor=actor)
    assert r3["record_id"], r3["answer"]
    assert fake.submitted[0]["has_invoice"] == "有票", fake.submitted[0]
    print("[PASS] test_existing_ticket_not_downgraded_by_spoken_no_invoice")


def test_no_invoice_then_upload_evidence_becomes_invoiced():
    #无票 + 下一轮上传证据 → 有票（实物纠正口误），跨轮生效
    _backend()
    a = _agent()
    actor = _actor()
    r1 = a.route("我要报销 张三 研发部 办公用品 100 没发票 事由是买文具", actor=actor)
    assert r1["draft"]["has_invoice"] == "无票", r1["draft"]
    r2 = a.route("📎 发票已上传，请帮我填单", base_draft=r1["draft"], actor=actor,
                 invoice_fields={"invoice_no": "2311", "invoice_seller": "某商贸",
                                 "amount": 100, "category": "办公用品", "source": "invoice"})
    assert r2["draft"]["has_invoice"] == "有票", r2["draft"]
    print("[PASS] test_no_invoice_then_upload_evidence_becomes_invoiced")


# ---------- 填单中吐槽「金额没显示」不该被踢去政策咨询 ----------

def test_complaint_about_amount_stays_in_fill_branch():
    #「财务部 招待费 金额不是有显示吗 喝奶茶」含问号 但是在填单 → 必须走 B
    _backend()
    a = _agent()
    actor = _actor()
    r1 = a.route("📎 发票已上传，请帮我填单", actor=actor,
                 invoice_fields={"invoice_no": "2311", "invoice_seller": "某商贸",
                                 "amount": 14.42, "category": "招待费",
                                 "date": "2026/10/23", "source": "invoice"})
    assert r1["draft"]["amount"] == 14.42, r1["draft"]
    r2 = a.route("财务部 招待费 金额不是有显示吗 喝奶茶",
                 base_draft=r1["draft"], actor=actor)
    assert r2["intent"] == "B", f"不该走咨询分支: {r2}"
    #吐槽句不能被当成事由
    assert "有显示吗" not in (r2["draft"].get("reason") or ""), r2["draft"]
    #已识别的金额要如实回显 不能说「读不到」
    assert "14.42" in r2["answer"], r2["answer"]
    assert "不会自动读取" not in r2["answer"], r2["answer"]
    print("[PASS] test_complaint_about_amount_stays_in_fill_branch")


def test_policy_question_in_fill_still_goes_consult():
    #填单中问真制度问题（标准/上限）→ 仍放行 A 咨询 不能被拉进填单
    _backend()
    a = _agent()
    actor = _actor()
    r1 = a.route("我要报销 张三 研发部 差旅费 800 事由是出差", actor=actor)
    r2 = a.route("差旅费住宿标准是多少", base_draft=r1["draft"], actor=actor)
    assert r2["intent"] == "A", f"制度问句应走咨询: {r2}"
    print("[PASS] test_policy_question_in_fill_still_goes_consult")


# ---------- 用户文字改字段（票据状态不可改 其他字段可改） ----------

def test_text_edit_overrides_ocr_fields_except_invoice_status():
    #复现用户截图：上传凭证后 连说「是交通费」「不是财务部是市场部」都被弹回旧值
    _backend()
    a = _agent()
    actor = _actor()
    #第1轮：上传支付凭证（OCR 识别 14.42 / 办公用品）
    r1 = a.route("📎 支付凭证已上传，请帮我填单", actor=actor,
                 invoice_fields={"invoice_no": "", "invoice_seller": "某商贸",
                                 "amount": 14.42, "category": "办公用品",
                                 "date": "2026/10/23", "source": "voucher",
                                 "upload_id": "u1"})
    assert r1["draft"]["amount"] == 14.42, r1["draft"]
    assert r1["draft"]["category"] == "办公用品", r1["draft"]
    #第2轮：用户说「是交通费」——前端仍捎带同一份 OCR 字段
    r2 = a.route("是交通费", base_draft=r1["draft"], actor=actor,
                 invoice_fields={"invoice_no": "", "invoice_seller": "某商贸",
                                 "amount": 14.42, "category": "办公用品",
                                 "date": "2026/10/23", "source": "voucher",
                                 "upload_id": "u1"})
    assert r2["draft"]["category"] == "交通费", f"类型没改过来: {r2['draft']}"
    #金额没被用户提到 → 保持票据值 14.42
    assert r2["draft"]["amount"] == 14.42, r2["draft"]
    #票据状态不受影响
    assert r2["draft"]["has_invoice"] == "有票", r2["draft"]
    print("[PASS] test_text_edit_overrides_ocr_fields_except_invoice_status")


def test_negation_correction_picks_new_value():
    #「不是办公用品是招待费」→ 招待费；「不是财务部是市场部」→ 市场部
    a = _agent()
    assert a._extract_fields("不是办公用品是招待费")["category"] == "招待费"
    assert a._extract_fields("不是财务部是市场部")["department"] == "市场部"
    assert a._extract_fields("改成交通费")["category"] == "交通费"
    assert a._extract_fields("金额不是14.42是200")["amount"] == 200.0
    print("[PASS] test_negation_correction_picks_new_value")


def test_plain_affirmative_department_edit():
    #复现用户截图：旧草稿部门=市场部，用户说「部门是财务部」应改成财务部
    #（之前正则把"是"吞进部门名 → normalize 失败 → 部门纹丝不动；金额却能改）
    a = _agent()
    #单元层：平铺肯定句「部门是/叫/为 X」都能归一
    assert a._extract_fields("部门是财务部")["department"] == "财务部", \
        a._extract_fields("部门是财务部")
    assert a._extract_fields("部门叫市场部")["department"] == "市场部"
    assert a._extract_fields("部门为研发部")["department"] == "研发部"
    #端到端：在已有草稿上改部门，其它字段保持、票据状态依旧锁定
    #（直接走 _handle_fill：这正是 bug 所在层；真实场景因 await_confirm 粘性也会落到这里）
    _backend()
    actor = _actor()
    base = {"applicant": "张三", "department": "市场部", "category": "交通费",
            "amount": 20.0, "reason": "送顾客", "has_invoice": "有票",
            "city": "", "date": "2026/10/23", "await_confirm": True}
    r = a._handle_fill("部门是财务部", base_draft=base, actor=actor)
    assert r["draft"]["department"] == "财务部", f"部门没改过来: {r['draft']}"
    assert r["draft"]["amount"] == 20.0, r["draft"]
    assert r["draft"]["category"] == "交通费", r["draft"]
    assert r["draft"]["has_invoice"] == "有票", r["draft"]
    print("[PASS] test_plain_affirmative_department_edit")


def test_field_directed_edits_template():
    #A方案：模板句「aa字段修改为xxx」每个字段都改得准 事由不再把「修改为」吞进值里
    a = _agent()
    cases = [
        ("部门修改为财务部", "department", "财务部"),
        ("报销类型修改为招待费", "category", "招待费"),
        ("城市修改为上海", "city", "上海"),
        ("日期修改为2026/10/1", "date", "2026/10/1"),
        ("事由修改为请客户吃饭", "reason", "请客户吃饭"),
    ]
    base = {"department": "市场部", "category": "办公用品", "amount": 100.0,
            "city": "北京", "date": "2026/9/9", "reason": "旧事由"}
    for text, key, expect in cases:
        f = a._merge_draft(dict(base), a._extract_fields(text))
        notes = []
        edited = a._apply_field_edits(f, text, notes, base_draft=dict(base))
        assert f[key] == expect, f"{text} → {f[key]!r} want {expect!r}; notes={notes}"
        assert key in edited, f"{text} 未命中定向编辑回执; notes={notes}"
        assert any("已把" in n for n in notes), notes
    #金额锁定（2026-10-30）：草稿带 amount_locked 时定向改金额被拒并明示
    locked = dict(base, amount_locked=True)
    f = a._merge_draft(dict(locked), a._extract_fields("金额修改为200"))
    n = []
    edited = a._apply_field_edits(f, "金额修改为200", n, base_draft=dict(locked))
    assert f["amount"] == 100.0, (f, n)
    assert "amount" not in edited, (edited, n)
    assert any("以发票识别为准" in x for x in n), n
    print("[PASS] test_field_directed_edits_template")


def test_field_edit_casual_phrasings():
    #A方案：不带「字段名+修改为」模板的口语说法也能改
    a = _agent()
    base = {"department": "市场部", "amount": 100.0}
    #口语前缀「帮我把…」
    f = a._merge_draft(dict(base), a._extract_fields("帮我把部门改成财务部"))
    n = []
    a._apply_field_edits(f, "帮我把部门改成财务部", n, base_draft=dict(base))
    assert f["department"] == "财务部", (f, n)
    #尾部语气词「了」不混进值
    f = a._merge_draft(dict(base), a._extract_fields("部门改成财务部了"))
    n = []
    a._apply_field_edits(f, "部门改成财务部了", n, base_draft=dict(base))
    assert f["department"] == "财务部", (f, n)
    #一句话改两个字段
    f = a._merge_draft(dict(base), a._extract_fields("部门改成财务部 金额改成200"))
    n = []
    a._apply_field_edits(f, "部门改成财务部 金额改成200", n, base_draft=dict(base))
    assert f["department"] == "财务部" and f["amount"] == 200.0, (f, n)
    #疑问句不当修改指令（解析器零产出；泛用抽取捞到类型词是既有行为 与定向编辑无关）
    assert a._parse_field_edits("报销类型是交通费吗") == [], a._parse_field_edits("报销类型是交通费吗")
    assert a._parse_field_edits("事由是什么意思") == [], a._parse_field_edits("事由是什么意思")
    print("[PASS] test_field_edit_casual_phrasings")


def test_field_edit_end_to_end_via_fill():
    #A方案端到端：确认卡场景一句话改部门+金额 有回执 锁死字段不动
    #金额已锁定（有发票识别值）→ 金额改不动并明示 其他字段照常改
    _backend()
    a = _agent()
    actor = _actor()
    base = {"applicant": "张三", "department": "市场部", "category": "办公用品",
            "amount": 100.0, "reason": "书本书费", "has_invoice": "有票",
            "city": "北京", "date": "2026/10/1", "await_confirm": True,
            "amount_locked": True}
    r = a._handle_fill("部门修改为财务部，金额修改为200", base_draft=dict(base), actor=actor)
    assert r["draft"]["department"] == "财务部", r["draft"]
    assert r["draft"]["amount"] == 100.0, r["draft"]
    assert r["draft"]["city"] == "北京" and r["draft"]["has_invoice"] == "有票", r["draft"]
    assert "已把部门改为 「财务部」" in r["answer"], r["answer"]
    assert "以发票识别为准" in r["answer"], r["answer"]
    print("[PASS] test_field_edit_end_to_end_via_fill")


def test_field_edit_protected_fields():
    #A方案：申请人/票据状态两个锁死字段 明确拒改并提示
    a = _agent()
    f = {"applicant": "张三", "has_invoice": "有票"}
    n = []
    a._apply_field_edits(f, "申请人改成李四 票据改成无票", n, base_draft=dict(f))
    assert f["applicant"] == "张三" and f["has_invoice"] == "有票", (f, n)
    assert any("登录身份" in x for x in n), n
    assert any("票据状态" in x for x in n), n
    print("[PASS] test_field_edit_protected_fields")


def test_field_edit_fresh_upload_invoice_wins():
    #A方案 × 以票为准：本轮刚上传新票据时 定向改金额不吃文字值
    _backend()
    a = _agent()
    actor = _actor()
    r1 = a.route("📎 发票已上传，请帮我填单", actor=actor,
                 invoice_fields={"invoice_no": "123", "invoice_seller": "某公司",
                                 "amount": 14.42, "source": "invoice", "upload_id": "u9"})
    assert r1["draft"]["amount"] == 14.42, r1["draft"]
    #换新票据（upload_id 变）+ 同句要改金额 → 金额锁定为发票值并明示
    r2 = a.route("金额修改为200", base_draft=r1["draft"], actor=actor,
                 invoice_fields={"invoice_no": "123", "invoice_seller": "某公司",
                                 "amount": 14.42, "source": "invoice", "upload_id": "u10"})
    assert r2["draft"]["amount"] == 14.42, r2["draft"]
    assert "以发票识别为准" in r2["answer"], r2["answer"]
    print("[PASS] test_field_edit_fresh_upload_invoice_wins")


def test_reason_digits_stay_in_reason():
    #问题27（2026-10-30）：事由里的规格数字（A4/5箱/20支）不能被剥掉、更不能被当金额
    a = _agent()
    #① 补事由路径（await_reason）：裸数字原样保留在事由里
    n = a._reason_from_answer("采购A4纸5箱、签字笔20支",
                              {"department": "", "category": "", "city": ""})
    assert n == "采购A4纸5箱、签字笔20支", n
    #② 没有带单位的金额 → 金额不乱取（以前「A4」的4会被当成 ¥4）
    f = a._extract_fields("采购A4纸5箱、签字笔20支")
    assert f["amount"] == 0.0, f
    #③ 带单位的金额照常识别 且事由截到金额前面
    f2 = a._extract_fields("研发部，事由是123 800 元，有发票")
    assert f2["amount"] == 800.0, f2
    assert f2["reason"] == "123", f2
    #④ 端到端：金额锁定 + 补事由 → 事由数字完整、发票金额不动
    _backend()
    actor = _actor()
    base = {"applicant": "张三", "department": "财务部", "category": "办公用品",
            "amount": 100.0, "has_invoice": "有票", "await_reason": True,
            "amount_locked": True}
    r = a._handle_fill("采购A4纸5箱、签字笔20支", base_draft=dict(base), actor=actor)
    assert "A4" in r["draft"]["reason"], r["draft"]
    assert r["draft"]["amount"] == 100.0, r["draft"]
    print("[PASS] test_reason_digits_stay_in_reason")


def test_amount_locked_text_digits_cannot_override():
    #金额锁定（2026-10-30）：上传发票后 普通文字里的数字金额不再覆盖发票值
    _backend()
    a = _agent()
    actor = _actor()
    r1 = a.route("📎 发票已上传，请帮我填单", actor=actor,
                 invoice_fields={"invoice_no": "N1", "invoice_seller": "某商贸",
                                 "amount": 50.98, "source": "invoice", "upload_id": "uA"})
    assert r1["draft"]["amount"] == 50.98, r1["draft"]
    assert r1["draft"].get("amount_locked") is True, r1["draft"]
    #同票据重复携带的下一轮 用户话里带别的数字 → 发票金额不动
    r2 = a.route("事由是招待客户花了300元", base_draft=r1["draft"], actor=actor,
                 invoice_fields={"invoice_no": "N1", "invoice_seller": "某商贸",
                                 "amount": 50.98, "source": "invoice", "upload_id": "uA"})
    assert r2["draft"]["amount"] == 50.98, r2["draft"]
    #换新票据 → 金额跟随新发票（金额永远以最近一次识别的发票为准）
    r3 = a.route("📎 发票已上传，请帮我填单", base_draft=r2["draft"], actor=actor,
                 invoice_fields={"invoice_no": "N2", "invoice_seller": "某酒店",
                                 "amount": 88.5, "source": "invoice", "upload_id": "uB"})
    assert r3["draft"]["amount"] == 88.5, r3["draft"]
    print("[PASS] test_amount_locked_text_digits_cannot_override")


def test_invoice_status_still_not_text_editable():
    #票据状态依旧不可被文字改（用户已拍板）但其他字段可以
    _backend()
    a = _agent()
    actor = _actor()
    r1 = a.route("📎 支付凭证已上传，请帮我填单", actor=actor,
                 invoice_fields={"invoice_no": "", "invoice_seller": "某商贸",
                                 "amount": 14.42, "source": "voucher", "upload_id": "u1"})
    #用户说「无发票」→ 票据状态维持有票（不可改）
    r2 = a.route("无发票", base_draft=r1["draft"], actor=actor)
    assert r2["draft"]["has_invoice"] == "有票", r2["draft"]
    assert "按「有票」计算" in r2["answer"], r2["answer"]
    print("[PASS] test_invoice_status_still_not_text_editable")


if __name__ == "__main__":
    test_classify_intent_consult()
    test_classify_intent_fill()
    test_classify_intent_compliance()
    test_classify_intent_progress()
    test_consult_offline()
    test_extract_fields_basic()
    test_extract_department_routes_to_enum()
    test_extract_city_routes_to_whitelist()
    test_extract_chinese_date_no_year()
    test_extract_special_category_blocks()
    test_extract_applicant_no_garbage()
    test_compliance_entertainment_over_limit()
    test_compliance_no_invoice_blocks()
    test_compliance_office_supplies_over_limit()
    test_compliance_travel_city_cap()
    test_fill_missing_asks_when_incomplete()
    test_fill_waits_for_confirm_before_writing()
    test_fill_confirm_submits_via_backend()
    test_extract_reason_basic()
    test_fill_asks_reason_when_missing()
    test_fill_reason_from_single_sentence_after_ask()
    test_reason_excludes_already_known_fields()
    test_reason_not_filled_when_only_field_words()
    test_fill_confirm_without_reason_does_not_call_backend()
    test_fill_rejects_reporting_for_others()
    test_fill_rejects_help_someone_pattern()
    test_fill_no_false_intercept_on_expense_words()
    test_extract_applicant_ignores_expense_words()
    test_fill_no_invoice_blocked()
    test_fill_untipped_invoice_blocked()
    test_fill_backend_rejects_shows_reason()
    test_fill_backend_down_no_direct_write()
    test_fill_bogus_department_asked_not_submitted()
    test_reason_not_polluted_by_type_amount()
    test_fill_sticky_keeps_branch()
    test_invoice_fields_merged_into_draft()
    test_ocr_invoice_no_sets_has_invoice()
    test_ocr_overrides_spoken_no_invoice()
    test_no_ocr_keeps_untipped_status()
    test_attach_uploaded_after_submit()
    test_attach_skipped_without_image()
    test_attach_failure_does_not_block()
    test_c_blocked_for_employee()
    test_c_allowed_for_finance()
    test_c_never_creates_record()
    test_d_employee_sees_only_self()
    test_d_finance_sees_all()
    test_d_bill_no_precise()
    test_d_lists_newest_first()
    test_d_truncate_hint()
    test_d_records_carry_all_fields()
    test_d_answer_is_summary_only()
    test_d_backend_down_friendly()
    test_d_not_found_message()
    test_route_requires_login()
    test_confirm_card_shows_risk()
    test_no_invoice_never_reaches_confirm()
    test_invoice_reminder_only_when_action_needed()
    test_untipped_asks_once_without_duplicate()
    test_only_invoiced_can_reach_confirm()
    test_do_submit_blocks_untipped()
    test_messages_avoid_internal_jargon()
    test_confirm_card_mentions_edit_path()
    test_after_submit_ask_guides_manual()
    test_fill_guide_button()
    test_spoken_invoice_claim_does_not_count()
    test_only_upload_fills_from_ocr_and_asks_missing()
    test_text_then_upload_conflict_uses_invoice()
    test_voucher_counts_as_invoiced()
    test_existing_ticket_not_downgraded_by_spoken_no_invoice()
    test_no_invoice_then_upload_evidence_becomes_invoiced()
    test_complaint_about_amount_stays_in_fill_branch()
    test_policy_question_in_fill_still_goes_consult()
    test_text_edit_overrides_ocr_fields_except_invoice_status()
    test_negation_correction_picks_new_value()
    test_plain_affirmative_department_edit()
    test_field_directed_edits_template()
    test_field_edit_casual_phrasings()
    test_field_edit_end_to_end_via_fill()
    test_field_edit_protected_fields()
    test_field_edit_fresh_upload_invoice_wins()
    test_amount_locked_text_digits_cannot_override()
    test_reason_digits_stay_in_reason()
    test_invoice_status_still_not_text_editable()
    print("\n全部 M4 编排层单测通过")
