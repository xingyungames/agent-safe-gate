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
    assert f["has_invoice"] == "有票", f
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
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=_actor())
    assert res["draft"] and res["draft"].get("await_confirm"), res
    assert not fake.submitted, "未确认前不得提交"
    print("[PASS] test_fill_waits_for_confirm_before_writing")


def test_fill_confirm_submits_via_backend():
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor)
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
    res = a.route("我要报销 张三 研发部 招待费 800 有发票", actor=_actor())
    assert res["intent"] == "B" and res["draft"].get("await_reason"), res
    assert "事由" in res["answer"], res["answer"]
    assert not fake.submitted
    print("[PASS] test_fill_asks_reason_when_missing")


def test_fill_reason_from_single_sentence_after_ask():
    #追问过事由后 用户只回一句短语也要收下
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票", actor=actor)
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
    res = a.route("我要报销 张三 研发部 招待费 800 有发票", actor=actor)
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert not res2["record_id"], res2
    assert "事由" in res2["answer"], res2["answer"]
    assert not fake.submitted, "缺少必填项时不应提交"
    print("[PASS] test_fill_confirm_without_reason_does_not_call_backend")


def test_fill_rejects_reporting_for_others():
    fake = _backend()
    a = _agent()
    res = a.route("我要报销 李四 研发部 招待费 800 有发票 事由是客户招待", actor=_actor())
    assert "只能由本人提交" in res["answer"], res["answer"]
    assert "李四" in res["answer"], res["answer"]
    assert res.get("draft") is None, res
    assert not fake.submitted, "替人报账不得提交"
    print("[PASS] test_fill_rejects_reporting_for_others")


def test_fill_rejects_help_someone_pattern():
    #「帮X报」句式同样要拦
    fake = _backend()
    a = _agent()
    res = a.route("帮李四报一笔差旅费 800 有发票 上海", actor=_actor())
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
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor)
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
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor)
    res2 = a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert "主服务器暂时不可用" in res2["answer"], res2["answer"]
    assert not feishu.uploaded and not feishu.updated, "不得直连飞书"
    print("[PASS] test_fill_backend_down_no_direct_write")


def test_fill_bogus_department_asked_not_submitted():
    fake = _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 火星部 招待费 800 有发票 事由是客户招待", actor=actor)
    #部门归一失败应为空 进入追问而不是直接提交
    assert not res["draft"] or not res["draft"].get("department"), res
    assert not fake.submitted
    print("[PASS] test_fill_bogus_department_asked_not_submitted")


def test_reason_not_polluted_by_type_amount():
    _backend()
    a = _agent()
    res = a.route("我要报销 张三 研发部 差旅费 800 有发票", actor=_actor())
    assert res["draft"]["reason"] in ("", None), res["draft"]
    print("[PASS] test_reason_not_polluted_by_type_amount")


def test_fill_sticky_keeps_branch():
    _backend()
    a = _agent()
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor)
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
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor)
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
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor)
    a.route("确认提交", base_draft=res["draft"], actor=actor)
    assert not feishu.uploaded, "无图不应调用上传"
    print("[PASS] test_attach_skipped_without_image")


def test_attach_failure_does_not_block():
    _backend()
    feishu = MockFeishu(fail=True)
    a = _agent(feishu=feishu)
    actor = _actor()
    res = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=actor)
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
    res = a.route("我要报销 张三 研发部 招待费 3000 有发票 事由是客户招待", actor=_actor())
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
    r1 = a.route("我要报销 张三 研发部 招待费 800 有发票", actor=_actor())
    assert r1["answer"].count("🧾") == 1, r1["answer"]
    r2 = a.route("我要报销 张三 研发部 招待费 800 没发票", actor=_actor())
    assert r2["answer"].count("🧾") == 2, r2["answer"]
    print("[PASS] test_invoice_reminder_only_when_action_needed")


def test_messages_avoid_internal_jargon():
    #不向用户暴露「上传飞书」这类内部实现细节
    _backend()
    a = _agent()
    r1 = a.route("我要报销 张三 研发部 招待费 800 有发票 事由是客户招待", actor=_actor())
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
        res = a.route(text, actor=_actor())
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
    res = a.route("我要报销 张三 研发部 招待费 41 有发票 事由是客户喝奶茶", actor=actor)
    assert "有误请直接说要改哪一项" in res["answer"], res["answer"]
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
    print("\n全部 M4 编排层单测通过")
