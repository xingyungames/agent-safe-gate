"""
M4 编排层单测（不联网、不依赖付费 API）
=====================================
覆盖：意图识别(A/B/C/D)、A 咨询分支离线跑通、reranker 注入生效、compressor 注入生效。

运行：
    py -3 tests/test_agent.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.retriever import Retriever, Chunk
from models.llm import LLMAdapter, MockProvider
from models.agent import XiaoCaiAgent, INTENT_RULES, normalize_department


CORPUS = [
    Chunk(text="差旅费报销需提供出差审批单、交通票据、住宿发票。单人住宿标准不超过300元/晚。",
          source="报销制度.md"),
    Chunk(text="招待费报销需提供发票、消费清单。单次超过2000元须部门总监审批。",
          source="报销制度.md"),
    Chunk(text="填单规则：报销单必须填写事由、报销类型、金额、申请人工号；发票抬头须与公司一致，否则退回。",
          source="填单规则.md"),
]


def _agent(compressor=None, reranker=None, feishu=None, table_id=None):
    llm = LLMAdapter(MockProvider(canned="[MOCK] 回复内容"))
    ret = Retriever(CORPUS, reranker=reranker)
    return XiaoCaiAgent(
        llm, ret, compressor=compressor, default_category="报销制度",
        feishu=feishu, table_id=table_id,
    )


class MockFeishu:
    """离线替代飞书客户端：记录调用，按设定返回。"""

    def __init__(self):
        self.created = []
        self.listed = []

    def create_records(self, table_id, records):
        ids = [f"rec_{i}_{len(self.created)}" for i in range(len(records))]
        self.created.extend(records)
        return ids

    def list_records(self, table_id, **kw):
        self.listed.append(kw)
        return [{
            "record_id": "rec_x",
            "fields": {"单号": "BX1", "申请人": "小新新", "报销类型": "差旅费", "状态": "审批中",
                       "审核人": "王审核", "金额": 500, "工号": "A123"},
        }]

    def get_record(self, table_id, record_id, **kw):
        return {"record_id": record_id, "fields": {"单号": "BX-001"}}


class FakeAuth:
    """M8 认证墙 e2e 专用最小认证：固定用户 张三(员工)，verify/bind_sid 离线可用。"""

    def __init__(self):
        self.bindings = {}

    def verify(self, token):
        if token == "tok-test":
            return {"username": "zhangsan", "name": "张三", "role": "员工",
                    "department": "研发部", "employee_id": "E0001"}
        return None

    def bind_sid(self, sid, username):
        bound = self.bindings.setdefault(sid, username)
        return (bound == username, "" if bound == username else f"bound to {bound}")


# ---------------------------------------------------------- 意图识别
def test_classify_intent_consult():
    a = _agent()
    assert a.classify_intent("差旅费住宿标准是多少") == "A"
    assert a.classify_intent("报销制度里怎么规定的") == "A"
    print("[PASS] test_classify_intent_consult")


def test_classify_intent_fill():
    a = _agent()
    assert a.classify_intent("我要填一张报销单") == "B"
    assert a.classify_intent("怎么提交报销") == "B"
    print("[PASS] test_classify_intent_fill")


def test_classify_intent_compliance():
    a = _agent()
    assert a.classify_intent("这笔报销超标了吗有没有风险") == "C"
    print("[PASS] test_classify_intent_compliance")


def test_classify_intent_progress():
    a = _agent()
    assert a.classify_intent("我的报销单现在什么状态") == "D"
    print("[PASS] test_classify_intent_progress")


# ---------------------------------------------------------- A 咨询离线跑通
def test_consult_offline():
    a = _agent()
    res = a.route("差旅费住宿标准是多少")
    assert res["intent"] == "A"
    assert res["answer"] == "[MOCK] 回复内容"
    assert res["chunks"], "应检索到资料"
    assert all(c.category == "报销制度" for c in res["chunks"])
    print("[PASS] test_consult_offline")


# ---------------------------------------------------------- B 填单（写飞书草稿）
def test_b_fill_with_feishu():
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要填差旅费报销单 张三 研发部 金额800 有发票 北京 9月10号")
    # 必填齐全后应进入「确认前核对」态，不直接写表
    assert res["intent"] == "B"
    assert res["record_id"] is None, "确认前不应写表"
    assert "确认提交" in res["answer"], "应提示用户确认"
    # 用户确认 → 真正写表并拿到可读单号
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["intent"] == "B"
    assert res2["record_id"], "确认后应已写入飞书并拿到 record_id"
    assert "BX" in res2["answer"], "答案应带回可读单号"
    assert "rec_" not in res2["answer"], "答案不应泄露内部 record_id（屏蔽）"
    print("[PASS] test_b_fill_with_feishu")


def test_b_fill_without_feishu():
    a = _agent()
    res = a.route("我要填报销单 张三 研发部 招待费 金额800 有发票")
    # 必填齐全 → 确认态（不写表）
    assert res["intent"] == "B"
    assert res["record_id"] is None
    assert "确认提交" in res["answer"], "应进入确认态"
    # 确认后尝试写表 → 飞书未接入
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["record_id"] is None
    assert "未接入" in res2["answer"]
    print("[PASS] test_b_fill_without_feishu")


# ---------------------------------------------------------- C 合规预审
def test_c_compliance():
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("这笔招待费3000超标了吗")
    assert res["intent"] == "C"
    assert res["compliance"] == "风险"
    assert "超2000" in res["risk_summary"]
    print("[PASS] test_c_compliance")


# ---------------------------------------------------------- 字段抽取（本地规则）
def test_extract_fields_basic():
    a = _agent()
    f = a._extract_fields("小新新，研发部，上海出差，2600，招待费，发票有，9月10号")
    assert f["applicant"] == "小新新"
    assert f["department"] == "研发部"
    assert f["category"] == "招待费"
    assert f["amount"] == 2600
    assert f["has_invoice"] == "有票"
    assert f["city"] == "上海"
    assert f["date"] == f"{datetime.now().year}/9/10"
    print("[PASS] test_extract_fields_basic")


def test_extract_department_routes_to_enum():
    """部门归一路由：模糊输入必须映射到企业标准枚举，非标准值留空（不写脏值）。"""
    a = _agent()
    # 抽取层：正则锚定「部/部门」，捕获后经 normalize_department 归一
    assert a._extract_fields("我是张三，研发部门，招待费 800").get("department") == "研发部"
    assert a._extract_fields("张三 研发部 招待费 800").get("department") == "研发部"
    assert a._extract_fields("钱会计 财务部 招待费 300").get("department") == "财务部"
    assert a._extract_fields("李工 技术部 交通费 50").get("department") == "信息技术部"
    # 裸核心词（研发/技术）经 normalize_department 直接归一路由到字典
    assert normalize_department("研发") == "研发部"
    assert normalize_department("信息技术") == "信息技术部"
    # 无法唯一确定 / 非标准 → 留空，交由机器人追问，绝不写出脏值
    assert a._extract_fields("王五 电脑部门 招待费 800").get("department") == ""
    assert a._extract_fields("赵六 管理部门 招待费 800").get("department") == ""
    print("[PASS] test_extract_department_routes_to_enum")


def test_extract_city_routes_to_whitelist():
    """城市抽取只认白名单：命中即归一到标准城市名，非白名单不抽取。"""
    a = _agent()
    assert a._extract_fields("去上海出差 差旅费 800").get("city") == "上海"
    assert a._extract_fields("成都开会 差旅费 600").get("city") == "成都"
    # 非白名单城市（如「火星」）不抽取 → 空，不写出脏值
    assert a._extract_fields("火星出差 差旅费 600").get("city") == ""
    print("[PASS] test_extract_city_routes_to_whitelist")


def test_fill_bogus_department_asked_not_written():
    """回归：部门无法归一（如火星部）时，必填缺失 → 机器人追问部门，且确认后绝不写脏值。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要报销 张三 火星部 招待费 800 有发票")
    # 部门无法归一 → 必填缺失 → 进入追问态，不直接写表
    assert res["intent"] == "B"
    assert res["record_id"] is None
    assert "部门" in res["answer"], "应追问部门"
    # 即便强行「确认提交」，因部门仍缺失不会写表
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["record_id"] is None, "部门缺失不应写表"
    assert not any(rec.get("部门") == "火星部" for rec in a.feishu.created), "绝不应写出非标准部门"
    print("[PASS] test_fill_bogus_department_asked_not_written")


def test_extract_chinese_date_no_year():
    a = _agent()
    f = a._extract_fields("张三 研发部 买办公用品 800 没发票 三月二号")
    assert f["applicant"] == "张三"
    assert f["department"] == "研发部"
    assert f["category"] == "办公用品"
    assert f["has_invoice"] == "无票"
    assert f["date"] == f"{datetime.now().year}/3/2"
    print("[PASS] test_extract_chinese_date_no_year")


def test_extract_special_category_blocks():
    a = _agent()
    f = a._extract_fields("我想报通讯费200元")
    assert f["special"] == "通讯费"
    assert f["category"] == ""
    print("[PASS] test_extract_special_category_blocks")


# ---------------------------------------------------------- 合规预审核心
def test_compliance_entertainment_over_limit():
    a = _agent()
    comp, summary = a._compliance_check(
        {"category": "招待费", "amount": 2600, "has_invoice": "有票", "date": "", "city": ""}
    )
    assert comp == "风险"
    assert "超2000" in summary
    print("[PASS] test_compliance_entertainment_over_limit")


def test_compliance_no_invoice_blocks():
    a = _agent()
    comp, summary = a._compliance_check(
        {"category": "差旅费", "amount": 800, "has_invoice": "无票", "date": "", "city": "北京"}
    )
    assert comp == "阻断"
    print("[PASS] test_compliance_no_invoice_blocks")


# ---------------------------------------------------------- B 追问 / 专项流程
def test_fill_missing_asks_when_incomplete():
    a = _agent()
    res = a.route("我要填报销单")  # 全缺
    assert res["intent"] == "B"
    assert res["record_id"] is None
    assert "补齐" in res["answer"]
    print("[PASS] test_fill_missing_asks_when_incomplete")


# ---------------------------------------------------------- B 票据状态：未提可提交 / 无票阻断
def test_b_invoice_untipped_submits():
    """填单规则允许「未提」：未提发票不应强制追问，确认后仍按未提提交（合规层风险提示）。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要报销 张三 研发部 招待费 800")
    # 必填齐全 → 确认态（确认前不写表）
    assert res["intent"] == "B"
    assert res["record_id"] is None, "确认前不写表"
    assert "确认提交" in res["answer"]
    # 确认提交：未提发票也应提交成功
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["record_id"], "未提发票也应提交成功"
    assert res2["draft"] is None, "提交完成后应清空草稿"
    # 改查实际写入飞书的「票据状态」确认默认为未提
    assert a.feishu.created[-1]["票据状态"] == "未提"
    print("[PASS] test_b_invoice_untipped_submits")


def test_b_invoice_none_blocks():
    """C 方案：无票提交先软确认一次（不写表）；用户坚持「仍要提交」则放行写表并标记合规结果=驳回。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要报销 张三 研发部 招待费 800 没发票")
    assert res["intent"] == "B"
    assert res["record_id"] is None
    assert res["draft"]["has_invoice"] == "无票"
    # 第一次确认提交 → 软确认（二选一提示），不写表
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["record_id"] is None, "软确认阶段不写表"
    assert "有发票了" in res2["answer"] and "仍要提交" in res2["answer"], "应给出二选一软确认提示"
    assert res2["draft"] is not None and res2["draft"].get("no_invoice_warned"), "应记住已软确认"
    # 坚持「仍要提交」→ 放行写表，合规结果标记「驳回」+ 风险摘要注明用户坚持
    res3 = a.route("仍要提交", base_draft=res2["draft"])
    assert res3["record_id"], "用户坚持提交应放行写表"
    assert a.feishu.created[-1]["合规结果"] == "驳回", "无票坚持提交应标记合规结果=驳回"
    assert "无票提交" in a.feishu.created[-1]["风险摘要"], "风险摘要应注明无票坚持提交"
    assert res3["draft"] is None, "提交成功后清空草稿"
    print("[PASS] test_b_invoice_none_blocks")


def test_b_invoice_none_then_fixed():
    """无票软确认后回复「有发票了」→ 票据更新为有票，回到确认摘要，正常提交不标驳回。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要报销 张三 研发部 招待费 800 没发票")
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert "有发票了" in res2["answer"], "应先软确认"
    # 补票：票据状态更新，重新回确认摘要（不直接写表）
    res3 = a.route("有发票了", base_draft=res2["draft"])
    assert res3["record_id"] is None, "补票后应先重新核对，不直接写表"
    assert res3["draft"]["has_invoice"] == "有票", "票据状态应更新为有票"
    assert "有票" in res3["answer"], "确认摘要应显示有票"
    # 补票后确认 → 正常写表，合规结果不应是驳回
    res4 = a.route("确认提交", base_draft=res3["draft"])
    assert res4["record_id"], "补票后确认应正常写表"
    assert a.feishu.created[-1]["票据状态"] == "有票"
    assert a.feishu.created[-1]["合规结果"] != "驳回", "补票后不应标记驳回"
    print("[PASS] test_b_invoice_none_then_fixed")


def test_fill_special_category_reply():
    a = _agent()
    res = a.route("我要报团建费500")
    assert res["intent"] == "B"
    assert res["record_id"] is None
    assert "专项流程" in res["answer"]
    print("[PASS] test_fill_special_category_reply")


# ---------------------------------------------------------- B 软提示：事由 / 差旅城市
def test_soft_hints_reason_missing():
    a = _agent()
    hints = a._soft_hints({"reason": "", "category": "招待费", "city": "北京"})
    assert any("事由" in h and "通过率" in h for h in hints), "缺事由应提示补充以提高通过率"
    assert not any("城市" in h for h in hints), "非差旅不应提示城市"
    print("[PASS] test_soft_hints_reason_missing")


def test_soft_hints_travel_city_missing():
    a = _agent()
    hints = a._soft_hints({"reason": "开会", "category": "差旅费", "city": ""})
    assert any("城市" in h and "加快审核" in h for h in hints), "差旅缺城市应提示补充以加快审核"
    print("[PASS] test_soft_hints_travel_city_missing")


def test_ask_missing_includes_soft_hints():
    """多轮追问：必填缺失时，同时给出 事由 软提示。"""
    a = _agent()
    res = a.route("我要报销")  # 全缺 → 进 _ask_missing
    assert res["record_id"] is None
    assert "补齐" in res["answer"]
    assert "提高审核通过率" in res["answer"], "追问应含 事由 软提示"
    print("[PASS] test_ask_missing_includes_soft_hints")


def test_fill_success_shows_real_bill_no():
    """提交成功：确认后回复必须带回真实单号，绝不显示「系统生成中」。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要报销 张三 研发部 招待费 800 有发票")
    # 必填齐全 → 确认态（确认前不写表）
    assert res["intent"] == "B"
    assert res["record_id"] is None, "确认前不写表"
    assert "确认提交" in res["answer"]
    # 用户确认 → 真正写表
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["intent"] == "B"
    assert res2["record_id"], "确认后应已写入飞书"
    assert res2["bill_no"] == "BX-001", "应取回真实单号"
    assert "BX-001" in res2["answer"]
    assert "张三" in res2["answer"], "成功回复应回显申请人（确认参数生效）"
    assert "招待费" in res2["answer"], "成功回复应回显报销类型"
    assert "(系统生成中)" not in res2["answer"], "严禁误导占位符"
    assert "rec_" not in res2["answer"], "不应泄露 record_id"
    print("[PASS] test_fill_success_shows_real_bill_no")


def test_fill_success_soft_hints_when_reason_missing():
    """提交成功但缺事由：成功回复应带 事由 软提示（可更快通过）。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要报销 张三 研发部 招待费 800 有发票")
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["record_id"]
    assert "提高审核通过率" in res2["answer"], "成功回复也应带 事由 软提示"
    print("[PASS] test_fill_success_soft_hints_when_reason_missing")


def test_fill_success_travel_city_hint():
    """差旅费提交成功但缺城市：成功回复应带 城市 软提示（加快审核）。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要报销 张三 研发部 差旅费 800 有发票")
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["record_id"]
    assert res2["draft"] is None, "提交完成后应清空草稿"
    assert a.feishu.created[-1]["报销类型"] == "差旅费"
    assert "加快审核" in res2["answer"], "差旅缺城市应提示补充城市"
    print("[PASS] test_fill_success_travel_city_hint")


def test_fill_success_honest_fallback_when_bill_no_empty():
    """已写入成功但 AutoNumber 暂不可取：诚实兜底，不显示「系统生成中」。"""

    class NoBillFeishu:
        def create_records(self, table_id, records):
            return ["rec_0_0"]

        def get_record(self, table_id, record_id, **kw):
            return {"record_id": record_id, "fields": {}}  # 单号缺失

    a = _agent(feishu=NoBillFeishu(), table_id="tblX")
    res = a.route("我要报销 张三 研发部 招待费 800 有发票")
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["record_id"], "仍应视为提交成功"
    assert "(系统生成中)" not in res2["answer"], "严禁误导占位符"
    assert "进度查询" in res2["answer"], "应诚实引导去进度查询"
    print("[PASS] test_fill_success_honest_fallback_when_bill_no_empty")


def test_reason_not_polluted_by_type_amount():
    """回归：'出差费200元' 是「类型+金额」，不是事由；不得污染事由字段，且确认后仍触发事由软提示。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    txt = "我要填报销单，我是张三，研发部，出差费200元"
    f = a._extract_fields(txt)
    assert f["category"] == "差旅费"
    assert f["amount"] == 200
    assert f["reason"] == "", f"事由不应被'出差费200元'污染, got {f['reason']!r}"
    res = a.route(txt)
    # 必填齐全（张三/研发部/差旅费/200）→ 确认态，不直接提交
    assert res["record_id"] is None, "首次齐全应进入确认态"
    assert "确认提交" in res["answer"]
    res2 = a.route("确认提交", base_draft=res["draft"])
    assert res2["record_id"], "确认后应提交成功"
    assert "提高审核通过率" in res2["answer"], "缺事由应给出事由软提示"
    assert "加快审核" in res2["answer"], "差旅缺城市应给出城市软提示"
    print("[PASS] test_reason_not_polluted_by_type_amount")


# ---------------------------------------------------------- D 进度查询
def test_d_progress_with_feishu():
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("查一下工号A123的报销")
    assert res["intent"] == "D"
    assert "审批中" in res["answer"]
    print("[PASS] test_d_progress_with_feishu")


def test_d_full_fields_rendered():
    """D 查询全字段展示：审核人/审批意见/审批时间/发票信息/附件都要返回，空值写「无」。"""
    class FullRecFeishu:
        def list_records(self, table_id, **kw):
            return [{
                "record_id": "r1",
                "fields": {
                    "单号": "BX-000007", "申请人": "小新", "部门": "市场部",
                    "报销类型": "交通费", "金额": 10, "事由": "打车",
                    "状态": "已驳回", "合规结果": "通过", "风险摘要": "",
                    "审核人": "玩总", "审批意见": "不通过，不属于公司报销",
                    "审批时间": 1760000000000,  # 毫秒时间戳 → 应格式化
                    "发票信息": "2311；北京山柏云商贸中心",
                    "发票附件": [{"file_token": "ftk", "name": "发票.png"}],
                    # 城市/工号/票据状态/发票日期/记录提交时间 故意缺 → 应显示「无」
                },
            }]

    a = _agent(feishu=FullRecFeishu(), table_id="tblX")
    res = a.route("查询小新的进度")
    ans = res["answer"]
    # 有值的字段必须出现
    for needle in ("审核人：玩总", "审批意见：不通过，不属于公司报销",
                   "审批时间：", "发票信息：2311；北京山柏云商贸中心",
                   "发票附件：发票.png", "合规结果：通过"):
        assert needle in ans, f"答案应含「{needle}」\n{ans}"
    # 毫秒时间戳应格式化为日期而非裸数字
    assert "202" in ans and str(1760000000000) not in ans, "审批时间应格式化"
    # 空字段必须写「无」
    for line in ("城市：无", "工号：无", "票据状态：无", "风险摘要：无"):
        assert line in ans, f"空字段应写「无」：{line}\n{ans}"
    print("[PASS] test_d_full_fields_rendered")


def test_d_progress_bx_number_precise():
    """D 进度查询按 BX 单号(含连字符)精确匹配，不应误命中其它单号。"""
    class TwoRecFeishu:
        def list_records(self, table_id, **kw):
            return [
                {"record_id": "r1", "fields": {"单号": "BX-000013", "申请人": "小新新"}},
                {"record_id": "r2", "fields": {"单号": "BX-000014", "申请人": "李雷"}},
            ]

    a = _agent(feishu=TwoRecFeishu(), table_id="tblX")
    res = a.route("帮我查 BX-000014")
    assert res["intent"] == "D"
    assert "BX-000014" in res["answer"]
    assert "BX-000013" not in res["answer"], "不应误命中 BX-000013"
    print("[PASS] test_d_progress_bx_number_precise")


def test_d_progress_without_feishu():
    a = _agent()  # 无 feishu
    res = a.route("查一下工号A123的报销")
    assert res["intent"] == "D"
    assert "未接入" in res["answer"]
    print("[PASS] test_d_progress_without_feishu")


# ---------------------------------------------------------- reranker 注入生效
def test_reranker_injection():
    calls = {"n": 0}

    def mock_reranker(query, chunks):
        calls["n"] += 1
        reordered = list(reversed(chunks))
        for i, c in enumerate(reordered):
            c.score = round(1.0 - i * 0.1, 4)
        return reordered

    a = _agent(reranker=mock_reranker)
    res = a.route("差旅费住宿标准是多少")
    assert calls["n"] == 1, "reranker 应被调用一次"
    # 反转后，原 BM25 第一应排到末位；这里只验证重排确实生效（顺序按 reranker 返回）
    assert res["chunks"][0].score >= res["chunks"][-1].score
    print("[PASS] test_reranker_injection")


# ---------------------------------------------------------- compressor 注入生效
def test_compressor_injection():
    def mock_compress(query, text):
        return "COMPRESSED"

    a = _agent(compressor=mock_compress)
    res = a.route("差旅费住宿标准")
    # compress 作用在「喂给 LLM 的 user 消息上下文」里（format_context 返回字符串，不改 chunk.text）
    user_msg = a.llm.provider.calls[-1]["messages"][-1]["content"]
    assert "COMPRESSED" in user_msg, "压缩应生效于上下文"
    print("[PASS] test_compressor_injection")


# ---------------------------------------------------------- 路由稳定性（截图问题回归）
def test_classify_fill_continuation_type_word():
    """回归：'办公费 20块'（无填单触发词）也应判 B 填单，而非兜底成咨询。"""
    a = _agent()
    assert a.classify_intent("办公费 20块") == "B"
    # 但明显的政策问句仍判 A，不被类型词误伤
    assert a.classify_intent("差旅费住宿标准是多少") == "A"
    print("[PASS] test_classify_fill_continuation_type_word")


def test_extract_applicant_no_garbage():
    """回归：'有电子发票' 这类噪声不得截出姓名（如误把'有电子发'当申请人）。"""
    a = _agent()
    f = a._extract_fields("有电子发票")
    assert f["applicant"] == "", f"申请人不应被噪声污染, got {f['applicant']!r}"
    assert f["has_invoice"] == "有票", f"有电子发票应判有票, got {f['has_invoice']!r}"
    print("[PASS] test_extract_applicant_no_garbage")


def test_route_fill_sticky_keeps_name():
    """回归：填单进行中，续答只补类型(办公费)应粘连 B，且已确认的申请人(张三)不被冲掉。"""
    a = _agent()
    base = {"applicant": "张三", "department": "研发部", "category": "", "amount": 0,
            "reason": "", "has_invoice": "未提", "city": "", "date": "", "special": None}
    res = a.route("办公费", base_draft=base)
    assert res["intent"] == "B", "应留在 B 填单"
    assert res["draft"] is not None, "仍缺金额应处于追问态（草稿保留）"
    assert res["draft"]["applicant"] == "张三", "已确认姓名不应被冲掉"
    assert res["draft"]["category"] == "办公用品", "应抽取出报销类型"
    assert res["draft"]["amount"] == 0, "未提供金额不应臆造"
    print("[PASS] test_route_fill_sticky_keeps_name")


def test_route_fill_sticky_bare_name():
    """回归：填单进行中，仅说姓名(无触发词)也应粘连 B 并补全申请人。"""
    a = _agent()
    base = {"applicant": "", "department": "研发部", "category": "", "amount": 0,
            "reason": "", "has_invoice": "未提", "city": "", "date": "", "special": None}
    res = a.route("张三", base_draft=base)
    assert res["intent"] == "B"
    assert res["draft"]["applicant"] == "张三"
    print("[PASS] test_route_fill_sticky_bare_name")


def test_chat_fill_sticky_e2e():
    """端到端回归截图场景：填单中续答不得误判咨询，且张三不被'有电子发'冲掉；最终确认提交。"""
    from api.main import dispatch
    from api.session import SessionStore
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    s = SessionStore()
    auth, tok = FakeAuth(), "tok-test"
    r1 = dispatch("POST", "/chat", {"session_id": "e2e", "text": "我要填报销单，我是张三，研发部"},
                  a, s, auth=auth, token=tok)
    assert r1[0] == 200 and r1[1]["intent"] == "B"
    # 续答仅类型+金额（无触发词）→ 粘连 B 并进入确认态（不直接提交）
    r2 = dispatch("POST", "/chat", {"session_id": "e2e", "text": "办公费 20块"},
                  a, s, auth=auth, token=tok)
    assert r2[0] == 200 and r2[1]["intent"] == "B", "「办公费 20块」应留在 B"
    assert "张三" in r2[1]["answer"], "确认摘要应含申请人张三"
    assert r2[1]["record_id"] is None, "确认前不应写表"
    assert "确认提交" in r2[1]["answer"]
    # 用户确认 → 真正提交
    r3 = dispatch("POST", "/chat", {"session_id": "e2e", "text": "确认提交"},
                  a, s, auth=auth, token=tok)
    assert r3[0] == 200 and r3[1]["intent"] == "B"
    assert "张三" in r3[1]["answer"], "提交申请人应为张三"
    assert r3[1]["record_id"], "确认后字段齐全应已写表"
    assert a.feishu.created[-1].get("工号") == "E0001", "登录身份工号应写入"
    print("[PASS] test_chat_fill_sticky_e2e")


def test_fill_guide_button():
    """填单引导按钮口令：输出全部参数的友好引导（emoji+文字，禁止 JSON），并留空草稿承接续答。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("填单引导")
    assert res["intent"] == "B"
    assert res["record_id"] is None, "引导只讲解，不写表"
    assert res["draft"] == {}, "应留空草稿，承接后续逐项补齐"
    for kw in ("申请人", "部门", "报销类型", "金额", "票据", "事由", "城市", "发票日期"):
        assert kw in res["answer"], f"引导应包含参数 {kw}"
    assert "{" not in res["answer"] and "}" not in res["answer"], "引导禁止输出 JSON"
    # 空草稿具有粘性：用户接着一次性说完 → 直接进入确认态
    r2 = a.route("我是张三，研发部，招待费 800 有发票", base_draft=res["draft"])
    assert r2["intent"] == "B"
    assert "确认提交" in r2["answer"], "补齐必填后应进入确认态"
    print("[PASS] test_fill_guide_button")


def test_progress_name_followup():
    """D 续答回归：机器人刚要求提供姓名后，用户只回纯姓名应继续 D 查询，不误判成咨询。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    r1 = a.route("查询一下我的报销进度")
    assert r1["intent"] == "D"
    assert "请提供姓名或报销单号" in r1["answer"]
    history = [
        {"role": "user", "content": "查询一下我的报销进度"},
        {"role": "assistant", "content": r1["answer"]},
    ]
    r2 = a.route("小新", history=history)
    assert r2["intent"] == "D", "纯姓名续答应继续 D 查询"
    assert "BX" in r2["answer"], "应查到记录并展示单号"
    print("[PASS] test_progress_name_followup")


def test_d_query_with_name_phrase():
    """回归截图：「查询XX的进度」应直接抽出姓名检索，不再反问是谁；查无此人也要真去查。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    for q in ("查询小新的进度", "查询张三的进度"):
        r = a.route(q)
        assert r["intent"] == "D"
        assert "请提供姓名或报销单号" not in r["answer"], f"{q} 不应再反问姓名"
    r3 = a.route("查询贝星东的进度")
    assert r3["intent"] == "D"
    assert "没有找到" in r3["answer"], "查无此人应提示未找到（确实查过），而不是反问姓名"
    # 「查询我的进度」代词不应被当姓名，仍应礼貌反问
    r4 = a.route("查询我的进度")
    assert "请提供姓名或报销单号" in r4["answer"]
    print("[PASS] test_d_query_with_name_phrase")


def test_d_followup_possessive_name():
    """回归截图：机器人问姓名后，用户回「小新的」这类带「的」后缀也应继续 D 查询。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    r1 = a.route("查询一下我的报销进度")
    assert "请提供姓名或报销单号" in r1["answer"]
    history = [
        {"role": "user", "content": "查询一下我的报销进度"},
        {"role": "assistant", "content": r1["answer"]},
    ]
    r2 = a.route("小新的", history=history)
    assert r2["intent"] == "D", "「小新的」应继续 D 查询，不误判成咨询"
    assert "请提供姓名或报销单号" not in r2["answer"]
    print("[PASS] test_d_followup_possessive_name")


def test_d_bill_no_no_cross_match():
    """回归截图：查 BX-000098 不得因「金额8/连字符子串」误命中 BX-000097。"""
    class TwoRec:
        def list_records(self, table_id, **kw):
            return [
                {"record_id": "r1", "fields": {"单号": "BX-000097", "城市": "北京", "金额": 8,
                                                "报销类型": "差旅费", "状态": "待审核"}},
                {"record_id": "r2", "fields": {"单号": "BX-000098", "申请人": "小新", "部门": "运营部",
                                                "金额": 50, "报销类型": "交通费", "状态": "待审核"}},
            ]

    a = _agent(feishu=TwoRec(), table_id="tblX")
    r = a.route("查询BX-000098的")
    assert r["intent"] == "D"
    assert "BX-000098" in r["answer"], "应查到目标单号"
    assert "BX-000097" not in r["answer"], "金额8/字段子串不应导致误命中 97"
    # 连字符省略/大小写也能对上
    r2 = a.route("查询BX000098")
    assert "BX-000098" in r2["answer"]
    print("[PASS] test_d_bill_no_no_cross_match")


def test_d_followup_bill_no():
    """回归截图：机器人问「姓名或单号」后，回 BX-000098 应继续 D 查询，不误判成咨询。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    r1 = a.route("查询一下我的报销进度")
    assert "请提供姓名或报销单号" in r1["answer"]
    history = [
        {"role": "user", "content": "查询一下我的报销进度"},
        {"role": "assistant", "content": r1["answer"]},
    ]
    for reply in ("BX-000098", "单号BX000098", "bx-000098", "我是张三"):
        r2 = a.route(reply, history=history)
        assert r2["intent"] == "D", f"「{reply}」应继续 D 查询"
        assert "政策" not in (r2.get("answer") or "")[:20], f"「{reply}」不应落到咨询分支"
    print("[PASS] test_d_followup_bill_no")


# ---------------------------------------------------------- M8 角色权限（user 上下文）
_EMPLOYEE = {"username": "zhangsan", "name": "张三", "role": "员工",
             "department": "研发部", "employee_id": "E0001"}
_FINANCE = {"username": "caiwu", "name": "钱会计", "role": "财务",
            "department": "财务部", "employee_id": "E0002"}


def test_role_c_blocked_for_employee():
    """员工触发合规预审 → 语义化拒绝，不写表（服务端强制，与前端隐藏按钮无关）。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("帮我预审一下 招待费 3000 有发票", user=_EMPLOYEE)
    assert res["intent"] == "C"
    assert "财务" in res["answer"], "应提示仅财务/管理员可预审"
    assert res["record_id"] is None
    assert not a.feishu.created, "员工预审不得写表"
    print("[PASS] test_role_c_blocked_for_employee")


def test_role_c_allowed_for_finance():
    """财务可正常预审（C 流程本身不受角色影响）。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("帮我预审一下 我是钱会计 财务部 招待费 300 有发票", user=_FINANCE)
    assert res["intent"] == "C"
    assert res["compliance"] in ("通过", "风险")
    print("[PASS] test_role_c_allowed_for_finance")


def test_role_d_employee_forced_self():
    """员工查他人进度 → 强制只查自己；财务可查任意人。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")  # MockFeishu 记录申请人=小新新
    res = a.route("查询小新新的进度", user=_EMPLOYEE)
    assert res["intent"] == "D"
    assert "没有找到" in res["answer"] and "小新新" not in res["answer"], \
        "员工查他人应被强制改为查自己（张三无记录）"
    res2 = a.route("查询小新新的进度", user=_FINANCE)
    assert res2["intent"] == "D" and "小新新" in res2["answer"], "财务应可查任意人"
    print("[PASS] test_role_d_employee_forced_self")


def test_role_b_autofill_identity():
    """员工填单：姓名冒充被覆写；未说部门时回退账号资料部门。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我要报销 李四 招待费 800 有发票", user=_EMPLOYEE)
    assert res["draft"]["applicant"] == "张三", "冒充他人应被登录身份覆写"
    assert res["draft"]["department"] == "研发部", "未口述部门应回退账号资料部门"
    assert "确认提交" in res["answer"]
    res2 = a.route("确认提交", base_draft=res["draft"], user=_EMPLOYEE)
    assert res2["record_id"], "确认后应写表"
    rec = a.feishu.created[-1]
    assert rec["申请人"] == "张三" and rec["部门"] == "研发部"
    assert rec.get("工号") == "E0001", "登录身份的工号应写入新表工号字段"
    print("[PASS] test_role_b_autofill_identity")


def test_role_b_spoken_department_wins():
    """口述部门优先：账号是研发部，用户说市场部 → 落库市场部（2026-10-11 修订）。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("我是管大爷，市场部的，差旅费 2000 有发票", user=_EMPLOYEE)
    assert res["draft"]["applicant"] == "张三", "姓名仍以登录账号为准"
    assert res["draft"]["department"] == "市场部", "口述部门应优先于账号资料部门"
    print("[PASS] test_role_b_spoken_department_wins")


def test_role_anonymous_backward_compatible():
    """user=None（匿名）保持旧行为：C 可用、D 按问句查询（离线测试/兼容性保障）。"""
    a = _agent(feishu=MockFeishu(), table_id="tblX")
    res = a.route("帮我预审一下 招待费 300 有发票")
    assert res["intent"] == "C" and res["compliance"] == "通过", "匿名应保持旧行为"
    res2 = a.route("查询小新新的进度")
    assert "小新新" in res2["answer"], "匿名 D 查询应按问句关键词"
    print("[PASS] test_role_anonymous_backward_compatible")


# ---------------------------------------------------------- M9 发票 OCR：附件存档
class MockFeishuM9(MockFeishu):
    """M9 扩展：支持 field_names / upload_attachment / update_record 记录调用。"""

    def __init__(self, upload_error=None, with_invoice_columns=True):
        super().__init__()
        self.uploaded = []   # (filename, bytes, mime)
        self.updated = []    # (table_id, record_id, fields)
        self._upload_error = upload_error
        self._names = ({"发票信息", "发票附件"} if with_invoice_columns
                       else {"事由", "金额", "申请人", "部门"})

    def field_names(self, table_id, **kw):
        return self._names

    def upload_attachment(self, filename, file_bytes, content_type="application/octet-stream",
                          app_token=""):
        if self._upload_error is not None:
            raise self._upload_error
        self.uploaded.append((filename, bytes(file_bytes), content_type))
        return "boxcnenFakeTokenM9"

    def update_record(self, table_id, record_id, fields, app_token=""):
        self.updated.append((table_id, record_id, dict(fields)))
        return record_id


_FULL_DRAFT = {
    "applicant": "张三", "department": "研发部", "category": "差旅费",
    "amount": 800.0, "reason": "出差住宿", "has_invoice": "有票",
    "city": "北京", "date": "2026/10/1", "await_confirm": True,
    "special": None,  # 上一轮 route() 返回的 draft 必带全键
    "invoice_no": "INV9001", "invoice_seller": "北京示例酒店",
}


def test_submit_with_invoice_info_and_image():
    """M9 全链路：确认提交带图 → 发票信息随单写入 + 附件上传并回写关联。"""
    import base64 as _b64
    mf = MockFeishuM9()
    a = _agent(feishu=mf, table_id="tblX")
    img = _b64.b64encode(b"\xff\xd8\xff\xe0FAKEJPG").decode()
    res = a.route("确认提交", base_draft=dict(_FULL_DRAFT),
                  image_b64=img, image_mime="image/jpeg")
    assert res["record_id"], "应提交成功拿到 record_id"
    rec = mf.created[0]
    assert rec.get("发票信息") == "INV9001；北京示例酒店", "发票信息应随单写入"
    assert mf.uploaded and mf.uploaded[0][0] == "发票.jpg"
    assert bytes(mf.uploaded[0][1]) == b"\xff\xd8\xff\xe0FAKEJPG"
    assert mf.updated, "应回写发票附件"
    tid, rid, fields = mf.updated[0]
    assert rid == res["record_id"] and fields["发票附件"] == [{"file_token": "boxcnenFakeTokenM9"}]
    assert "已存档" in res["answer"]
    print("[PASS] test_submit_with_invoice_info_and_image")


def test_submit_without_image_no_attachment_call():
    """无图提交：行为与 M9 之前完全一致，无发票信息/无上传调用。"""
    mf = MockFeishuM9()
    a = _agent(feishu=mf, table_id="tblX")
    draft = {k: v for k, v in _FULL_DRAFT.items() if not k.startswith("invoice")}
    res = a.route("确认提交", base_draft=draft)
    assert res["record_id"]
    assert "发票信息" not in mf.created[0]
    assert not mf.uploaded and not mf.updated
    assert "已存档" not in res["answer"]
    print("[PASS] test_submit_without_image_no_attachment_call")


def test_attach_failure_degrades_not_blocks():
    """附件上传失败 → 单据仍提交成功，回答里有降级提示。"""
    from models.feishu import FeishuError
    mf = MockFeishuM9(upload_error=FeishuError("飞书附件上传失败 code=91403 msg=Forbidden"))
    a = _agent(feishu=mf, table_id="tblX")
    import base64 as _b64
    img = _b64.b64encode(b"JPG").decode()
    res = a.route("确认提交", base_draft=dict(_FULL_DRAFT), image_b64=img)
    assert res["record_id"], "附件失败不应影响主流程"
    assert "存档未成功" in res["answer"]
    print("[PASS] test_attach_failure_degrades_not_blocks")


def test_invoice_fields_merged_into_draft():
    """route(invoice_fields=...) → OCR 发票号码/销售方并入草稿，且 draft 不含图片数据。"""
    mf = MockFeishuM9()
    a = _agent(feishu=mf, table_id="tblX")
    res = a.route("我是张三，研发部，差旅费 800元，有发票",
                  invoice_fields={"invoice_no": "INV777", "invoice_seller": "某商务酒店"})
    d = res.get("draft") or {}
    assert d.get("invoice_no") == "INV777" and d.get("invoice_seller") == "某商务酒店"
    assert "aGVsbG8" not in str(d), "草稿中绝不允许出现图片 base64"
    print("[PASS] test_invoice_fields_merged_into_draft")


if __name__ == "__main__":
    test_classify_intent_consult()
    test_classify_intent_fill()
    test_classify_intent_compliance()
    test_classify_intent_progress()
    test_consult_offline()
    test_b_fill_with_feishu()
    test_b_fill_without_feishu()
    test_c_compliance()
    test_extract_fields_basic()
    test_extract_department_routes_to_enum()
    test_extract_city_routes_to_whitelist()
    test_fill_bogus_department_asked_not_written()
    test_extract_chinese_date_no_year()
    test_extract_special_category_blocks()
    test_compliance_entertainment_over_limit()
    test_compliance_no_invoice_blocks()
    test_fill_missing_asks_when_incomplete()
    test_b_invoice_untipped_submits()
    test_b_invoice_none_blocks()
    test_b_invoice_none_then_fixed()
    test_fill_special_category_reply()
    test_soft_hints_reason_missing()
    test_soft_hints_travel_city_missing()
    test_ask_missing_includes_soft_hints()
    test_fill_success_shows_real_bill_no()
    test_fill_success_soft_hints_when_reason_missing()
    test_fill_success_travel_city_hint()
    test_fill_success_honest_fallback_when_bill_no_empty()
    test_reason_not_polluted_by_type_amount()
    test_d_progress_with_feishu()
    test_d_full_fields_rendered()
    test_d_progress_without_feishu()
    test_d_progress_bx_number_precise()
    test_reranker_injection()
    test_compressor_injection()
    test_classify_fill_continuation_type_word()
    test_extract_applicant_no_garbage()
    test_route_fill_sticky_keeps_name()
    test_route_fill_sticky_bare_name()
    test_chat_fill_sticky_e2e()
    test_fill_guide_button()
    test_progress_name_followup()
    test_d_query_with_name_phrase()
    test_d_followup_possessive_name()
    test_d_bill_no_no_cross_match()
    test_d_followup_bill_no()
    test_role_c_blocked_for_employee()
    test_role_c_allowed_for_finance()
    test_role_d_employee_forced_self()
    test_role_b_autofill_identity()
    test_role_b_spoken_department_wins()
    test_role_anonymous_backward_compatible()
    test_submit_with_invoice_info_and_image()
    test_submit_without_image_no_attachment_call()
    test_attach_failure_degrades_not_blocks()
    test_invoice_fields_merged_into_draft()
    print("\n全部 M4 编排层单测通过")
