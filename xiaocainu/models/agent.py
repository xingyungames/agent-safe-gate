#编排路由层 检索加生成 分支路由
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime

from .retriever import Retriever, format_context
from .config import get_provider_config, knowledge_dir
from .llm import LLMAdapter
from .feishu import (
    FeishuBitableClient,
    FeishuError,
    ALLOWED_TYPES,
    DEPT_WHITELIST,
    CITY_WHITELIST,
    CITY_TIER,
)
from .actor import Actor, PERM_INVOICE_READ_ALL, PERM_INVOICE_REVIEW

SYSTEM_CONSULT = (
    "你是小财，一个企业财务报销助手。只能依据下方[资料]回答用户问题；"
    "若资料中没有相关信息，如实说明「资料中未提及」，不要编造或猜测。"
    "回答简洁、用中文、面向普通员工。"
    "严禁输出「已记录/预审通过/提交到飞书/确认后帮您提交」等系统操作话术，"
    "也不要复述资料里的示例对话；你只负责解答报销制度疑问。"
)

#备用 LLM 抽取提示词 默认不启用
FILL_EXTRACT_SYSTEM_PROMPT = (
    "你是报销信息的结构化抽取引擎，只输出一个 JSON 对象（字段：applicant/department/"
    "category/amount/reason/has_invoice/city/date），category 仅取差旅费/招待费/办公用品/"
    "交通费，has_invoice 仅取有票/无票/未提，date 格式 YYYY/M/D。"
)

#意图识别 命中关键词分支 未命中兜底 A
INTENT_RULES = [
    ("D", ["进度", "到哪", "查一下", "我的报销单", "审批没", "到账", "现在什么状态", "查询", "查"]),
    ("B", ["填", "提交", "上传", "创建", "新建", "开报销单", "怎么报", "报一笔",
           "帮我报", "我要报销", "我要报", "报销单"]),
    ("C", ["合规", "预审", "能不能报", "有没有风险", "超标", "能不能过"]),
]

#城市白名单 抽取与分级共用
CITY_KEYWORDS = CITY_WHITELIST

#部门简称映射 不确定的留空追问
DEPT_ALIASES = {
    "研发": "研发部", "研发部": "研发部",
    "技术": "信息技术部", "信息技术": "信息技术部", "信息": "信息技术部", "it": "信息技术部",
    "人力": "人力资源部", "人力资源": "人力资源部", "hr": "人力资源部",
    "行政": "行政部", "财务": "财务部", "市场": "市场部", "销售": "销售部",
    "生产": "生产部", "采购": "采购部", "质管": "质量管理部", "质量": "质量管理部",
    "客服": "客户服务部", "客户": "客户服务部",
}


def normalize_department(raw: str) -> str:
    #部门名归一为标准枚举
    if not raw:
        return ""
    if raw in DEPT_WHITELIST:
        return raw
    #先剥掉前置判断词（"是/叫/为财务部"）→ 让"部门是财务部"这类平铺肯定句也能归一
    core = raw.lstrip("是叫为").replace("部门", "").replace("部", "").strip().lower()
    return DEPT_ALIASES.get(core, "")

#报销类型模糊映射 招待费优先
CATEGORY_MAP = [
    ("招待费", ["招待", "请客", "客户吃饭", "吃饭", "用餐", "宴请", "业务招待"]),
    ("差旅费", ["差旅", "出差", "住酒店", "住宿", "高铁", "车票", "机票", "火车"]),
    ("办公用品", ["办公用品", "办公", "采购", "文具", "耗材", "低值"]),
    ("交通费", ["交通费", "打车", "滴滴", "加油", "油费", "过路费", "市内交通"]),
]

#专项流程类别 不直交须引导走专项
SPECIAL_CATEGORY_MAP = [
    ("团建/员工活动", ["团建", "员工活动", "活动费"]),
    ("通讯费", ["通讯费", "话费", "电话费"]),
    ("培训费", ["培训", "职业发展"]),
    ("市场推广费", ["推广", "宣传", "市场推广"]),
    ("加班餐补", ["加班餐", "误餐", "餐补"]),
    ("私车公用", ["私车公用", "公里补贴", "里程"]),
]

#政策问句标记 命中放行到 A
_A_QUESTION_MARKERS = ("吗", "呢", "？", "?", "多少", "什么", "标准", "规定", "介绍",
                      "说明", "可以", "能否", "是否", "为什么", "有啥")

#真正的制度问句 填单中也要放行到 A（区别于「金额不是有显示吗」这类吐槽）
_POLICY_QUESTION_WORDS = ("标准", "规定", "上限", "限额", "最多", "至少", "流程", "政策",
                          "制度", "材料", "多久", "几天", "怎么报", "怎么办", "能不能报",
                          "可不可以", "谁审批", "几级", "需要什么")

#吐槽「识别/显示」类的话术 里面往往混着真正的填单内容 不能整句当事由
_COMPLAINT_VERBS = ("显示", "看到", "识别", "读取", "上传")

#否定改口 「不是A是B」→ 用户在改口 取B（A是被否定的旧值 不能留）
_CORRECT_NEG_RE = re.compile(
    r"(?:不是|不要|搞错|错了|改错)(?P<denied>[^，。；;,\s]{1,12}?)"
    r"(?:是|应该是|要|改[成是为]|换[成是为])")
#直接改口 「改成B」「应该是B」→ 取B
_CORRECT_SET_RE = re.compile(
    r"(?:改成|改为|换成|改成了|应该是)(?P<tail>[^，。；;,\s]{1,12})")

#报销类型固定匹配顺序（不用 set 的随机序 结果可复现）
_TYPE_ORDER = tuple(sorted(ALLOWED_TYPES, key=lambda c: (-len(c), c)))

#字段定向修改（2026-10-28）：「部门改成财务部」「金额修改为200」→ 只改命中的字段
#别名顺序即匹配优先级 长词在前 防「报销类型」被「类型」截胡
_FIELD_TOKENS = ("报销类型", "开票日期", "类型", "部门", "金额", "城市",
                 "事由", "用途", "备注", "日期", "时间", "申请人", "票据")
_FIELD_KEY_MAP = {
    "报销类型": "category", "类型": "category",
    "部门": "department", "金额": "amount", "城市": "city",
    "事由": "reason", "用途": "reason", "备注": "reason",
    "开票日期": "date", "日期": "date", "时间": "date",
    "申请人": "applicant", "票据": "has_invoice",
}
#回执里显示的字段名
_FIELD_LABELS = {"category": "报销类型", "department": "部门", "amount": "金额",
                 "city": "城市", "reason": "事由", "date": "日期",
                 "applicant": "申请人", "has_invoice": "票据"}
#连接词 最长优先（单字「是/为/：」殿后）
_FIELD_EDIT_VERBS = ("修改为", "修改成", "调整为", "设置为", "改成", "改为",
                     "换成", "写成", "填成", "弄成", "设为", "是", "为", "：", ":")
#值取到空白/标点为止；限长 60 防长句误吞
_FIELD_EDIT_RE = re.compile(
    r"(?P<field>" + "|".join(_FIELD_TOKENS) + r")"
    r"[^，。；;！!？?\n]{0,3}?"
    r"(?P<verb>" + "|".join(_FIELD_EDIT_VERBS) + r")\s*"
    r"(?P<value>[^，。；;！!？?\s\n]{1,60})")
#值尾部的语气词/结构助词 剥掉再归一（「财务部了」「财务部的」）
_FIELD_VALUE_TAILS = "了呀吧哦呢嘛的"

#填单引导口令 直接输出引导不走抽取
FILL_GUIDE_TRIGGERS = {"填单引导", "填单指南"}

#已提交后想补票或改单 对话里做不到 统一引导人工
_AFTER_SUBMIT_KEYWORDS = ("补发票", "补传", "补个票", "补票", "改单", "修改报销单",
                          "撤回", "作废", "提交后能改", "提交了还能改")
_AFTER_SUBMIT_ANSWER = (
    "🧾 已提交的报销单，对话里没法补传发票或修改。\n"
    "请联系财务，或者重新上传材料齐全的报销单进飞书财务表。"
)

#引导开场收尾话术素材 随机切换
_FILL_GUIDE_OPENERS = (
    "来啦来啦~ 填单引导这就安排上 (๑•̀ㅂ•́)و✧",
    "别急别急，小财奴手把手教您填 ~(＾▽＾)~",
    "好嘞好嘞~ 其实填报销单超简单，听我细细道来 ✨",
    "收到！先花 30 秒看完这段，填单不迷路 (￣▽￣)／",
)
_FILL_GUIDE_CLOSERS = (
    "一次性说完或者分开说都行，我帮您记着呢~ ✨",
    "想到哪说到哪，剩下的交给我帮您补齐 (´▽`ʃ♡ƪ)",
    "随时补充，我随时候着~ 加油，报销人！💪",
    "看懂了吗？没看懂再问我一遍也好呀 (๑´ㅂ`๑)",
)
#报销类型词 命中视为填单续答
_TYPE_WORDS = ("差旅费", "招待费", "办公费", "交通费", "办公用品", "报销")
#姓名排除词 常见口语与代词
_SKIP_NAME = {"好的", "谢谢", "没有", "麻烦", "请问", "帮我", "这个", "那个",
              "我们", "你们", "他们", "她们", "自己", "大家", "没事", "在哪", "这里"}
#非人名字符 含则排除
_NON_NAME_CHARS = set(
    "票费电发开报填审请查购采差出住餐吃车机油路话训推广活动团私司单金额类型部门"
    "销交通补贴款宿"
    "理由事因为用于您我他她们这那没了的是不也都还再就才已有和与否如果比较标准规定"
    "介绍说明说么怎帮想要去回在哪能可该应或等及与"
)

#中文数字 日期月份用
_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
    "八": 8, "九": 9, "十": 10, "〇": 0,
}

#进度查询的伪姓名词
_D_NAME_BLOCKLIST = {"进度", "报销", "记录", "单号", "状态", "结果", "详情", "信息"}
_D_PRONOUN_CHARS = set("我你他她它谁咱")
#泛指词 抽到这些等于没给筛选条件
_D_GENERIC_KEYWORDS = {"所有人", "全部", "所有", "全员", "大家", "公司", "团队", "一下", "最近"}
#进度查询单次最多展示条数 超出时按提交时间倒序取最近的
_D_LIST_LIMIT = 50
#事由遇到这些字段名就截断 避免把后面的参数一起吞进来
_REASON_STOP = ("金额", "部门", "报销类型", "票据", "城市", "日期",
                "发票号", "发票", "工号", "申请人", "备注")
#追问事由时 这些短句不算回答
_REASON_SMALL_TALK = {"好的", "好", "嗯", "可以", "行", "收到", "谢谢", "没有了", "无", "没了"}


#判断一个词是否像真实姓名 用于申请人识别与代报拦截
def _valid_person_name(name: str) -> bool:
    name = (name or "").strip()
    if not re.fullmatch(r"[一-龥]{2,4}", name):
        return False
    if name in _SKIP_NAME or name in _D_NAME_BLOCKLIST or name in _TYPE_WORDS:
        return False
    if any(ch in _NON_NAME_CHARS for ch in name):
        return False
    if any(ch in _D_PRONOUN_CHARS for ch in name):
        return False
    return True


#毫秒时间戳转 int 用于按提交时间倒序 非法值当 0
def _ms_key(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _cn_num_token(tok: str):
    #中文或阿拉伯数字转 int
    tok = tok.strip()
    if not tok:
        return None
    if tok.isdigit():
        return int(tok)
    if "十" in tok:
        if tok == "十":
            return 10
        if tok.startswith("十"):
            return 10 + (_cn_num_token(tok[1:]) or 0)
        if tok.endswith("十"):
            return (_cn_num_token(tok[:-1]) or 1) * 10
        a, b = tok.split("十", 1)
        return (_cn_num_token(a) or 1) * 10 + (_cn_num_token(b) or 0)
    val = 0
    for ch in tok:
        d = _CN_NUM.get(ch)
        if d is None:
            return None
        val = val * 10 + d
    return val if val > 0 else None


def _normalize_date(text: str) -> str:
    #日期归一为 YYYY/M/D
    if not text:
        return ""
    #阿拉伯日期格式
    m = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", text)
    if m:
        return f"{m.group(1)}/{int(m.group(2))}/{int(m.group(3))}"
    #中文日期带年
    m = re.search(r"(\d{4})年\s*([0-9一-龥]{1,3})月\s*([0-9一-龥]{1,3})[日号]?", text)
    if m:
        mo = _cn_num_token(m.group(2))
        da = _cn_num_token(m.group(3))
        if mo and da:
            return f"{m.group(1)}/{mo}/{da}"
    #中文日期不带年
    m = re.search(r"([0-9一-龥]{1,3})月\s*([0-9一-龥]{1,3})[日号]", text)
    if m:
        mo = _cn_num_token(m.group(1))
        da = _cn_num_token(m.group(2))
        if mo and da:
            return f"{datetime.now().year}/{mo}/{da}"
    return ""


class XiaoCaiAgent:
    #检索加生成加分支路由的统一编排

    def __init__(
        self,
        llm: LLMAdapter,
        retriever: Retriever,
        compressor=None,
        top_k: int = 3,
        default_category: str | None = "报销制度",
        feishu=None,
        table_id: str | None = None,
    ) -> None:
        #保存依赖与配置
        self.llm = llm
        self.retriever = retriever
        self.compressor = compressor
        self.top_k = top_k
        self.default_category = default_category
        self.feishu = feishu          #飞书客户端 可插拔 未接入则降级
        self.table_id = table_id      #报销单主表

    #意图识别 关键词命中分支
    def classify_intent(self, text: str) -> str:
        t = text or ""
        #进度查询 优先级最高
        if any(k in t for k in ("进度", "到哪", "查一下", "我的报销单", "审批没",
                                "到账", "现在什么状态", "查询", "查")):
            return "D"
        #合规预审
        if any(k in t for k in ("合规", "预审", "能不能报", "有没有风险", "超标", "能不能过")):
            return "C"
        #政策问句优先判 A 咨询
        if any(q in t for q in _A_QUESTION_MARKERS):
            return "A"
        #填单意图 含报销动作类型或票据
        if any(k in t for k in ("填", "提交", "上传", "创建", "新建", "开报销单", "怎么报",
                                "报一笔", "帮我报", "我要报销", "我要报", "我想报",
                                "报销单", "报销", "发票")):
            return "B"
        #续答含报销类型词按填单处理
        if any(k in t for k in _TYPE_WORDS):
            return "B"
        return "A"

    #路由辅助 上下文粘性
    @staticmethod
    def _has_strong_intent(text: str) -> bool:
        #强意图 要离开填单
        t = text or ""
        return any(k in t for k in ("进度", "到哪", "查一下", "我的报销单", "审批没",
                                    "到账", "现在什么状态", "查询", "查",
                                    "合规", "预审", "能不能报", "有没有风险", "超标", "能不能过"))

    @staticmethod
    def _is_explicit_question(text: str) -> bool:
        #政策问句应放行到 A
        t = text or ""
        return any(q in t for q in _A_QUESTION_MARKERS)

    @staticmethod
    def _is_policy_question(text: str) -> bool:
        #真正的制度问句 填单中也要放行到 A
        t = text or ""
        return any(q in t for q in _POLICY_QUESTION_WORDS)

    @classmethod
    def _has_fill_payload(cls, text: str) -> bool:
        #这句话里带了填单字段（部门/类型/事由/金额数字/吐槽识别结果）→ 是填单续答 不是制度咨询
        #注意：光有「金额」「发票」两个字不算 太泛 会把「金额多少合适」这类真咨询拉进填单
        t = text or ""
        if any(k in t for k in ("部门", "事由", "用途", "备注", "因为", "用于")):
            return True
        if any(k in t for k in _TYPE_WORDS):
            return True
        #金额数字（带单位）算实打实的填单内容
        if re.search(r"\d+(?:\.\d+)?\s*(?:元|块|钱)", t):
            return True
        #部门简称
        if any(k in t for k in ("研发", "财务", "市场", "行政", "人事", "销售", "采购")):
            return True
        #吐槽「识别结果没显示」也算 是在追问自己这单
        if cls._is_complaint_about_display(t):
            return True
        return False

    @classmethod
    def _is_complaint_about_display(cls, text: str) -> bool:
        #「金额不是有显示吗」这类：吐槽识别结果 不该整句走咨询也不该整句当事由
        t = text or ""
        if not any(v in t for v in _COMPLAINT_VERBS):
            return False
        return any(q in t for q in _A_QUESTION_MARKERS)

    @staticmethod
    def _recognized_replay(f: dict) -> str:
        #如实回显已识别到的要素 绝不说「读不到/不会自动读取」这种假话
        bits = []
        if f.get("amount"):
            bits.append(f"金额 ¥{f['amount']:g}")
        if f.get("date"):
            bits.append(f"日期 {f['date']}")
        if f.get("invoice_seller"):
            bits.append(f"销售方 {f['invoice_seller']}")
        if f.get("category"):
            bits.append(f"类型 {f['category']}")
        if f.get("city"):
            bits.append(f"城市 {f['city']}")
        if not bits:
            return ("🧾 这次没能从您上传的材料里读出金额。"
                    "麻烦您直接报个数字给我，我马上填上~")
        return "🧾 您上传的材料我识别到：" + "、".join(bits) + "，已经填进去了~"

    @staticmethod
    def _strip_complaint(text: str) -> str:
        #去掉「金额不是有显示吗」这类吐槽片段 别让它混进事由
        #整句都是吐槽时返回空串 → 交给下一轮继续追问事由
        t = text or ""
        if not any(v in t for v in _COMPLAINT_VERBS):
            return t
        parts = re.split(r"[\s，,。；;！!？?]+", t)
        kept = []
        for p in parts:
            if (p and any(v in p for v in _COMPLAINT_VERBS)
                    and any(q in p for q in ("吗", "呢", "？", "?"))):
                continue
            kept.append(p)
        return " ".join(kept)

    @classmethod
    def _progress_followup_keyword(cls, question: str) -> str:
        #续答关键词 单号或姓名
        t = (question or "").strip()
        #单号格式
        m = re.search(r"(BX-?\s*\d[\d-]*)", t, re.IGNORECASE)
        if m:
            return m.group(1).replace(" ", "").upper()
        core = re.sub(r"[的了吧呀哦呢]+$", "", t)
        core = re.sub(r"^(我是|我叫|我的名字是)", "", core)
        if not re.fullmatch(r"[一-龥]{2,4}", core):
            return ""
        if core in _SKIP_NAME or core in _D_NAME_BLOCKLIST:
            return ""
        if any(ch in _D_PRONOUN_CHARS for ch in core):
            return ""
        if any(ch in _NON_NAME_CHARS for ch in core):
            return ""
        return core

    @classmethod
    def _is_progress_name_followup(cls, question: str, history) -> bool:
        #上一轮要姓名 本轮回姓名或单号
        last_assistant = ""
        for msg in reversed(list(history or [])):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                last_assistant = str(msg.get("content", ""))
                break
        if "请提供姓名或报销单号" not in last_assistant:
            return False
        return bool(cls._progress_followup_keyword(question))

    @staticmethod
    def _is_fill_in_progress(draft) -> bool:
        #草稿是否仍在进行中
        if not isinstance(draft, dict):
            return False
        #正在等事由 下一句仍算填单续答
        if draft.get("await_reason"):
            return True
        missing_core = not (draft.get("applicant") and draft.get("department")
                           and draft.get("category") and draft.get("amount"))
        blocked = draft.get("has_invoice") == "无票"
        return bool(missing_core or blocked)

    #统一入口 按意图分发
    def route(self, question: str, history=None, base_draft=None, actor: Actor | None = None,
              image_b64: str = "", image_mime: str = "image/jpeg",
              invoice_fields: dict | None = None) -> dict:
        #按意图路由到对应分支 未登录不放行任何业务分支
        if (question or "").strip() in FILL_GUIDE_TRIGGERS:
            return self._handle_fill_guide()
        if actor is None:
            return {
                "intent": "A",
                "answer": "🔐 请先登录后再使用报销助手（登录身份由主服务器校验）。",
            }
        #已提交后想补票/改单：对话里做不到，直接引导人工
        if any(k in (question or "") for k in _AFTER_SUBMIT_KEYWORDS):
            return {
                "intent": "B",
                "record_id": None,
                "bill_no": "",
                "answer": _AFTER_SUBMIT_ANSWER,
                "draft": base_draft,
            }
        intent = self.classify_intent(question)
        #填单中继续保持 B 分支
        _sticky = self._is_fill_in_progress(base_draft) or (isinstance(base_draft, dict) and base_draft.get("await_confirm"))
        if intent != "B" and base_draft and _sticky:
            #草稿还在进行中时：
            #  强意图（进度/合规/查询）→ 放行离开填单
            #  真制度问句（标准/上限/流程…）→ 放行 A
            #  带了填单字段的追问（金额不是有显示吗 财务部 招待费…）→ 留 B 继续填
            if not self._has_strong_intent(question) and not self._is_policy_question(question):
                if not self._is_explicit_question(question) or self._has_fill_payload(question):
                    return self._handle_fill(question, base_draft=base_draft, actor=actor,
                                             image_b64=image_b64, image_mime=image_mime,
                                             invoice_fields=invoice_fields)
        #D 续答 继续走进度查询
        if intent == "A" and self._is_progress_name_followup(question, history):
            return self._handle_progress(question, actor=actor)
        if intent == "A":
            return self._handle_consult(question, history=history)
        if intent == "B":
            return self._handle_fill(question, base_draft=base_draft, actor=actor,
                                     image_b64=image_b64, image_mime=image_mime,
                                     invoice_fields=invoice_fields)
        if intent == "C":
            #合规预审仅对有审核权限的角色开放
            if not actor.can(PERM_INVOICE_REVIEW):
                return {
                    "intent": "C",
                    "answer": "🛡️ 合规预审仅对财务/管理员开放哦~ "
                              "您可以把单子提交上来等财务预审，有疑问也可以直接问我报销制度。",
                    "compliance": "", "risk_summary": "", "record_id": None,
                }
            return self._handle_compliance(question, base_draft=base_draft, actor=actor)
        return self._handle_progress(question, actor=actor)

    #A 咨询分支
    def _handle_consult(self, question: str, history=None) -> dict:
        #检索资料后交给 LLM 作答
        chunks = self.retriever.query(
            question, top_k=self.top_k, categories=self.default_category
        )
        ctx = format_context(chunks, compress=self.compressor, query=question)
        user = f"问题：{question}\n\n[资料]\n{ctx}"
        result = self.llm.chat(user, system=SYSTEM_CONSULT, history=history)
        return {
            "intent": "A",
            "answer": result.text,
            "chunks": chunks,
            "model": result.model,
        }

    @staticmethod
    def _correction_parts(text: str):
        #改口表达拆成 (被否定的旧值, 改口后的新值)
        #「不是办公用品是招待费」→ ("办公用品", "招待费")
        #「改成交通费」→ ("", "交通费")
        t = text or ""
        m = _CORRECT_NEG_RE.search(t)
        if m:
            return m.group("denied"), t[m.end():].strip(" 　，,。；;：:")
        m2 = _CORRECT_SET_RE.search(t)
        if m2:
            return "", m2.group("tail").strip(" 　，,。；;：:")
        return "", ""

    @classmethod
    def _correction_tail(cls, text: str) -> str:
        #改口后的新值片段
        return cls._correction_parts(text)[1]

    @classmethod
    def _pick_type(cls, text: str) -> str:
        #报销类型：先精确字面再模糊映射
        #改口优先（「不是办公用品是招待费」→ 招待费 而不是被否定的办公用品）
        t = text or ""
        _denied, _tail = cls._correction_parts(t)
        if _tail:
            for cat in _TYPE_ORDER:
                if cat in _tail:
                    return cat
        #被否定的旧值要从候选里排除 免得又挑回它
        _skip = _denied
        for cat in _TYPE_ORDER:
            if cat in t and cat not in _skip:
                return cat
        for cat, kws in CATEGORY_MAP:
            if any(k in t for k in kws) and not any(k in _skip for k in kws):
                return cat
        return ""

    @classmethod
    def _pick_department(cls, text: str) -> str:
        #部门抽取并归一为标准枚举 改口优先（「不是财务部是市场部」→ 市场部）
        t = text or ""
        _denied, _tail = cls._correction_parts(t)
        for scope, skip in ((_tail, _denied), (t, _denied)):
            if not scope:
                continue
            m = re.search(r"([一-龥]{1,6}部门)", scope)
            if not m:
                #"部门是/叫/为/：X" 平铺肯定句：吃掉可选的"是/为/叫/："判断词再取值
                m = re.search(r"部门\s*[是为叫:：]?\s*([一-龥]{1,6})", scope)
            if not m:
                m = re.search(r"([一-龥]{2,6}部)(?=[，,\s。的]|$)", scope)
            if m:
                _raw = m.group(1)
                #被否定的旧值不再生效
                if _raw and _raw in skip:
                    continue
                _d = normalize_department(_raw)
                if _d:
                    return _d
        return ""

    #本地规则抽取字段 三类分支共用
    @staticmethod
    def _extract_fields(text: str) -> dict:
        t = text or ""
        result = {
            "applicant": "", "department": "", "category": "", "amount": 0.0,
            "reason": "", "has_invoice": "", "city": "", "date": "", "special": None,
        }

        #金额 改口优先（「金额不是14.42是200」→ 200 而不是被否定的旧值）
        #三级匹配（2026-10-30 问题27）：带单位 > 金额词后 > 独立裸数字；
        #「A4纸5箱」这类粘在商品规格里的数字不再是金额（前面紧贴汉字/字母的不算）
        _denied_amt, _tail_amt = XiaoCaiAgent._correction_parts(t)
        _AMT_WITH_UNIT = r"(\d+(?:\.\d+)?)\s*(?:万元|块钱|元|块|钱|￥|¥|[rR][mM][bB])"
        _AMT_AFTER_KW = r"(?:金额|花了|花费|费用|合计)\s*[:：为]?\s*(\d+(?:\.\d+)?)"
        _AMT_BARE = r"(?<![一-龥A-Za-z0-9])(\d+(?:\.\d+)?)"

        def _bare_amount(scope: str):
            #裸数字候选：跳过日期片段（2026/10、10月）里的数字
            #（注意空串 in 判定恒 True 首尾要先判空）
            for _bm in re.finditer(_AMT_BARE, scope):
                _before = scope[_bm.start() - 1] if _bm.start() > 0 else ""
                _after = scope[_bm.end()] if _bm.end() < len(scope) else ""
                if (_after and _after in "/-.年月日") or (_before and _before in "/-."):
                    continue
                return _bm
            return None

        _m = None
        for _scope in (_tail_amt, t):
            if not _scope:
                continue
            _m = (re.search(_AMT_WITH_UNIT, _scope)
                  or re.search(_AMT_AFTER_KW, _scope)
                  or _bare_amount(_scope))
            if _m:
                break
        if _m:
            try:
                result["amount"] = float(_m.group(1))
            except ValueError:
                result["amount"] = 0.0

        #票据状态：口述只认「无票」（用户声明没有）；口述「有发票」不再计为已开票，
        #必须由真实 OCR 证据（发票号/销售方/支付凭证）才能置「有票」，否则一律「未提」
        if re.search(r"没发票|无发票|没票|丢了|没有发票|没开|未开|没开发票", t):
            result["has_invoice"] = "无票"
        else:
            result["has_invoice"] = "未提"

        #先判专项流程类别
        for name, kws in SPECIAL_CATEGORY_MAP:
            if any(k in t for k in kws):
                result["special"] = name
                break

        #报销类型 先精确字面再模糊映射
        if not result["special"]:
            result["category"] = XiaoCaiAgent._pick_type(t)

        #城市仅在白名单内匹配
        for c in CITY_KEYWORDS:
            if c in t:
                result["city"] = c
                break

        #部门抽取并归一为标准枚举 纠正表达优先（「不是财务部是市场部」→ 市场部）
        result["department"] = XiaoCaiAgent._pick_department(t)

        #申请人 只认明确句式 不再从自由文本兜底猜名字
        m = re.search(r"我是\s*([一-龥]{2,4})", t)
        if not m:
            m = re.search(r"([一-龥]{2,4})\s*[,，]?\s*(?:研发部|市场部|传媒部门|电脑部门|部门)", t)
        if not m:
            m = re.search(r"查一下\s*([一-龥]{2,3})", t) or re.search(r"查\s*([一-龥]{2,3})的?报销", t)
        if not m:
            #替帮代句式 用于识别并拦截代他人报账
            m = re.search(r"(?:替|帮|代)\s*([一-龥]{2,3})\s*(?:报|报销|提交|填|交)", t)
        if m and _valid_person_name(m.group(1)):
            result["applicant"] = m.group(1).strip()

        #事由 引导词之后取到标点为止 再按下一个字段名截断
        m = re.search(r"(?:事由|用途|用于|因为|备注)\s*[：:是为]?\s*([^，,。；;！!\n]{2,60})", t)
        if m:
            reason = m.group(1).strip(" 　，,。；;：:、")
            for kw in _REASON_STOP:
                i = reason.find(kw)
                if i >= 2:
                    reason = reason[:i]
            #事由里带「N元/N块」→ 金额部分归金额 事由截到它前面（2026-10-30 问题27）
            #「A4」「5箱」「20支」没有单位 保留在事由里不动
            _am = re.search(r"\d+(?:\.\d+)?\s*(?:元|块|钱|万)", reason)
            if _am:
                reason = reason[:_am.start()].rstrip(" 　，,。；;：:、花了用费掉共")
            reason = reason.strip(" 　，,。；;：:、")
            if 2 <= len(reason) <= 200:
                result["reason"] = reason
        if not result["reason"]:
            #动作词后的描述须含两个实字
            m = re.search(r"(?:出差|请客|采购|打车)([一-龥0-9，,。 ]{2,20})", t)
            if m:
                tail = m.group(1).strip(" ，,。")
                if len(re.sub(r"[费元\s，,。0-9]", "", tail)) >= 2:
                    result["reason"] = m.group(0).strip(" ，,。")

        #日期
        result["date"] = _normalize_date(t)
        return result

    #合规预审核心规则
    @staticmethod
    def _city_tier(city: str):
        #住宿分级上限 元每晚
        if not city:
            return None
        return CITY_TIER.get(city, 250)  #非白名单按三线 250

    def _compliance_check(self, f: dict):
        #产出合规结果与风险摘要
        risks: list = []
        cat = f.get("category", "")
        amount = f.get("amount", 0) or 0
        has_invoice = f.get("has_invoice", "")
        date_str = f.get("date", "")
        city = f.get("city", "")

        #无票阻断 未提仅提示
        if has_invoice == "无票":
            return ("阻断", "票据状态为「无票」，缺少合规发票，提交前无法放行")
        if has_invoice == "未提":
            risks.append("票据状态未提，提交前需确认合规发票")
        #支付凭证（无发票号）虽可提交，但需财务核对后定
        if f.get("invoice_source") == "voucher" and has_invoice == "有票":
            risks.append("支付凭证非发票，财务核对后定")

        #额度校验
        if cat == "招待费" and amount > 2000:
            risks.append("单次招待费超2000元，需总经理事前审批")
        if cat == "办公用品" and amount > 5000:
            risks.append("超5000元，走采购流程，不适用个人报销")
        if cat == "差旅费":
            tier = self._city_tier(city)
            if tier is not None:
                if amount > tier:
                    risks.append(f"{city}属住宿分级上限{tier}元/晚，超标需事前审批")
            else:
                if amount > 3000:
                    risks.append("差旅费金额较大，请确认住宿城市与晚数是否符合分级标准")

        #时限 距开票超 30 天
        if date_str:
            try:
                y, mo, d = (int(x) for x in date_str.split("/"))
                dt = datetime(y, mo, d)
                if (datetime.now() - dt).days > 30:
                    risks.append("距开票超30天，需附情况说明")
            except Exception:
                pass

        if not risks:
            return ("通过", "无")
        return ("风险", "；".join(risks))

    #识别确认提交意图
    @staticmethod
    def _is_confirm(text: str) -> bool:
        #否定词优先拦截 避免误触发
        t = text or ""
        if any(w in t for w in ("不提交", "不确认", "不要提交", "不用提交", "暂不", "先不", "别提交")):
            return False
        return any(k in t for k in ("确认提交", "确认", "确定提交", "确定", "提交吧",
                                    "确认吧", "帮我提交", "没问题", "好的提交",
                                    "可以提交", "ok", "OK", "提交"))

    #票据状态提醒 按状态说用户要做的动作 不重复寒暄
    @staticmethod
    def _invoice_reminder(has_invoice: str) -> str:
        if has_invoice == "有票":
            return "🧾 已收到发票，审核会快一些~"
        if has_invoice == "无票":
            return ("🧾 这单还没有发票。可以上传发票，或上传详细的支付凭证"
                    "（微信/支付宝「账单详情」页），\n"
                    "我先帮您把金额、商户、时间填好；图片会保存到飞书财务表。")
        return ("🧾 还没记票据状态：请上传发票或者上传详细的支付凭证"
                "（微信/支付宝「账单详情」页）\n"
                "先补信息。")

    #从一句补充里提事由：先剔除已被其它字段吃掉的词 避免整句塞进事由
    @staticmethod
    def _reason_from_answer(text: str, f: dict) -> str:
        ans = (text or "").strip()
        if not (2 <= len(ans) <= 200):
            return ""
        if XiaoCaiAgent._is_confirm(ans) or ans in _REASON_SMALL_TALK:
            return ""
        #整句就是别的字段 交给正常抽取 不当事由
        if any(ans.startswith(k) for k in _REASON_STOP):
            return ""
        leftover = ans
        #剔除本轮已识别的字段字面（只删第一次出现 避免误伤）
        for key in ("department", "category", "city"):
            v = str(f.get(key) or "").strip()
            if len(v) >= 2:
                leftover = leftover.replace(v, " ", 1)
        #剔除金额与单位（2026-10-30 问题27：只删「带单位」的数字；
        #裸数字可能是「A4纸」「5箱」「20支」的一部分 不能删）
        leftover = re.sub(r"\d+(?:\.\d+)?\s*(?:元|块|钱|万)", " ", leftover)
        #剔除票据词
        leftover = re.sub(r"有发票|没发票|无发票|有票|无票|未提", " ", leftover)
        leftover = re.sub(r"\s+", " ", leftover).strip(" ，,。；;、")
        #剩下的才可能是事由 太短说明整句都是别的字段 继续追问而不是硬塞
        return leftover if 2 <= len(leftover) <= 200 else ""

    #非必填的软提示
    @staticmethod
    def _soft_hints(f: dict) -> list:
        #返回可选的补充建议（事由是必填项 不在这里提示）
        hints = []
        #差旅费缺城市时提醒
        if f.get("category") == "差旅费" and not f.get("city"):
            hints.append("💡 补充消费「城市」可以加快审核速度~")
        return hints

    #填单追问与提交
    def _ask_missing(self, f: dict, missing: list, conflict_notes: list | None = None) -> str:
        #列出已填与待补字段
        lines = ["我帮您理了一下这单，还差几项就能提交~"]
        filled = []
        if f["applicant"]:
            filled.append(f"👤 申请人：{f['applicant']}")
        if f["department"]:
            filled.append(f"🏢 部门：{f['department']}")
        if f["category"]:
            filled.append(f"💼 报销类型：{f['category']}")
        if f["amount"]:
            filled.append(f"💰 金额：¥{f['amount']:g}")
        if f["has_invoice"]:
            filled.append(f"🧾 票据：{f['has_invoice']}")
        if f.get("reason"):
            filled.append(f"📝 事由：{f['reason']}")
        if filled:
            lines.append("已记录：" + " · ".join(filled))
        need = []
        if "申请人" in missing:
            need.append("👤 申请人（您怎么称呼呀？）")
        if "部门" in missing:
            need.append("🏢 部门（您属于哪个部门呀？）")
        if "报销类型" in missing:
            need.append("💼 报销类型（差旅费 / 招待费 / 办公用品 / 交通费？）")
        if "金额" in missing:
            need.append("💰 金额（花了多少呀？）")
        if "事由" in missing:
            need.append("📝 事由（只说用途就行，例如「通勤」「客户招待餐费」）")
        if "票据" in missing:
            need.append(self._invoice_reminder(f["has_invoice"] or "未提"))
        lines.append("还差：")
        lines.extend(need)
        #票据已并入「还差」清单时不再单独重复
        inv = f["has_invoice"] or "未提"
        if "票据" not in missing and inv in ("无票", "未提"):
            lines.append("")
            lines.append(self._invoice_reminder(inv))
        #顺带给出软提示
        hints = self._soft_hints(f)
        if hints:
            lines.append("")
            lines.extend(hints)
        if conflict_notes:
            lines.append("")
            lines.extend(conflict_notes)
        lines.append("")
        if f.get("await_reason") and "事由" in missing:
            lines.append("事由单独补一句就行，已有的信息不用重复~")
        else:
            lines.append("直接一句话补充就行，补齐后我请您核对。")
        return "\n".join(lines)

    #确认前回显摘要
    def _ask_confirm(self, f: dict, has_invoice: str, conflict_notes: list | None = None) -> str:
        #回显已填信息待用户确认
        lines = ["✨ 信息齐了，麻烦您核对一下~"]
        rows = [
            f"👤 申请人：{f['applicant']}",
            f"🏢 部门：{f['department']}",
            f"💼 报销类型：{f['category']}",
            f"💰 金额：¥{f['amount']:g}",
        ]
        if f.get("city"):
            rows.append(f"📍 城市：{f['city']}")
        if f.get("reason"):
            rows.append(f"📝 事由：{f['reason']}")
        src = f.get("invoice_source")
        inv_label = has_invoice + ("（发票）" if src == "invoice" else "（支付凭证）" if src == "voucher" else "")
        rows.append(f"🧾 票据：{inv_label}")
        lines.append("📋 当前已填信息：")
        lines.extend(rows)
        #金额锁定提示（2026-10-30）：金额只认发票识别值 提前说清免得用户白改
        if f.get("amount_locked"):
            lines.append(
                f"💰 金额按发票识别 ¥{(f.get('amount') or 0):g}，"
                "如与实际不符请重新上传清晰的发票")

        #提交前先算一次合规 有问题提前告知 别等提交后被打回
        compliance, risk_summary = self._compliance_check({**f, "has_invoice": has_invoice})
        blocked = compliance == "阻断"
        if blocked:
            lines.append("")
            lines.append(f"🚫 {risk_summary}")
        elif compliance != "通过":
            lines.append("")
            lines.append(f"⚠️ 风险提示：{risk_summary}")

        #事由缺失时提示补充（正常流程不可达 留作兜底）
        if not str(f.get("reason") or "").strip():
            lines.append("📝 备注：报销的主要事情或用途，请补充「事由」后我再帮您提交~")
        #票据引导：风险摘要已点明状态时只补动作 不重复说一遍
        if not blocked and has_invoice in ("无票", "未提"):
            if "票据状态" in risk_summary:
                lines.append("🧾 可直接上传发票或支付凭证补上（微信/支付宝「账单详情」页）。")
            else:
                lines.append(self._invoice_reminder(has_invoice))
        #城市等软提醒
        soft = self._soft_hints(f)
        if soft:
            lines.append("")
            lines.append("💡 还有这些可补充（非必填，补了更快通过）：")
            lines.extend(soft)
        lines.append("")
        if conflict_notes:
            lines.extend(conflict_notes)
            lines.append("")
        lines.append("✅ 补齐发票后回复我，我马上帮您提交~" if blocked
                     else "✅ 确认无误请回复「确认提交」；有误请直接说明哪一项修改为什么，准确识别无误。")
        return "\n".join(lines)

    #跨轮草稿合并
    @staticmethod
    def _merge_draft(base: dict, new: dict) -> dict:
        #非空值覆盖或补全空字段
        if not base:
            return new
        merged = dict(base)
        for k, v in new.items():
            if v in (None, "", 0.0, []):
                continue
            if k == "applicant" and base.get("applicant"):
                continue
            #未提不覆盖已有票据状态
            if k == "has_invoice" and v == "未提" and base.get("has_invoice") in ("有票", "无票"):
                continue
            #金额锁定为发票金额（2026-10-30）：文字金额不再覆盖已锁定的票据金额
            if k == "amount" and base.get("amount_locked") and v != base.get("amount"):
                continue
            merged[k] = v
        return merged

    #字段定向修改：解析「部门改成财务部」「金额修改为200」
    @classmethod
    def _parse_field_edits(cls, text: str):
        #→ [(草稿键, 字段名, 原始值, 是否可用, 归一值)]
        t = text or ""
        edits = []
        for m in _FIELD_EDIT_RE.finditer(t):
            field_word = m.group("field")
            key = _FIELD_KEY_MAP.get(field_word, "")
            if not key:
                continue
            raw = (m.group("value") or "").strip(" 　\"'「」『』“”‘’")
            if not raw:
                continue
            #疑问句不当修改指令（「类型是交通费吗」「事由是什么意思」「部门是哪个」）
            if raw.endswith(("吗", "呢")) or re.search(r"什么|怎么|多少|哪|几", raw):
                continue
            core = raw.rstrip(_FIELD_VALUE_TAILS) or raw
            core = re.sub(r"^[是为]", "", core).strip() or raw
            ok, val = cls._normalize_field_value(key, core)
            edits.append((key, _FIELD_LABELS.get(key, field_word), raw, ok, val))
        return edits

    @staticmethod
    def _normalize_field_value(key: str, val: str):
        #按字段各自口径归一 → (是否可用, 归一值)
        if key == "department":
            d = normalize_department(val)
            return bool(d), d
        if key == "category":
            if val in ALLOWED_TYPES:
                return True, val
            c = XiaoCaiAgent._pick_type(val)
            return bool(c), c
        if key == "amount":
            m = re.search(r"\d+(?:\.\d+)?", val)
            if not m:
                return False, 0.0
            try:
                return True, float(m.group(0))
            except ValueError:
                return False, 0.0
        if key == "city":
            if val in CITY_KEYWORDS:
                return True, val
            for c in CITY_KEYWORDS:
                if c in val:
                    return True, c
            return False, ""
        if key == "date":
            d = _normalize_date(val)
            return bool(d), d
        if key == "reason":
            return 2 <= len(val) <= 200, val
        return False, ""

    def _apply_field_edits(self, f: dict, text: str, notes: list,
                           base_draft=None, fresh_upload: bool = False,
                           ocr_keys=None) -> set:
        #把定向修改落到草稿 返回产生了「已改」回执的键（供旧回显去重）
        noted: set = set()
        for key, label, raw, ok, val in self._parse_field_edits(text):
            #两个锁死字段：申请人恒为登录身份 票据状态只认证据
            if key == "applicant":
                notes.append("👤 申请人以登录身份为准，改不了哦~")
                continue
            if key == "has_invoice":
                notes.append("🧾 票据状态以上传的票据为准，口述改不了~")
                continue
            #金额锁定为发票金额（2026-10-30）：识别到发票金额后文字/定向修改都改不动
            if key == "amount" and f.get("amount_locked"):
                _la = f.get("amount") or 0
                notes.append(f"💰 金额以发票识别为准（¥{_la:g}），改不了哦~"
                             "如发票金额识别有误，请重新上传清晰的发票")
                continue
            if not ok:
                notes.append(f"⚠️ 想把{label}改成「{raw}」，但这个值我没认出来，麻烦换个说法~")
                continue
            #本轮刚上传票据 → 票据覆盖到的字段以票为准（沿用「以票为准」口径）
            if fresh_upload and key in (ocr_keys or ()):
                notes.append(f"⚠️ 本轮刚上传票据，{label}以票据为准")
                continue
            old_val = (base_draft or {}).get(key)
            f[key] = val
            #只有真的改动了已有值才播报 首次填入/重申同值不播报
            if base_draft and old_val not in (None, "") and str(old_val) != str(val):
                shown = f"¥{val:g}" if key == "amount" else f"「{val}」"
                notes.append(f"✏️ 已把{label}改为 {shown}")
                noted.add(key)
        return noted

    #按钮口令的全参数引导
    def _handle_fill_guide(self) -> dict:
        #讲清全部所需参数
        opener = random.choice(_FILL_GUIDE_OPENERS)
        closer = random.choice(_FILL_GUIDE_CLOSERS)
        lines = [
            opener,
            "",
            "【必填 · 一项都不能少】",
            "👤 申请人 —— 您怎么称呼呀？",
            "🏢 部门 —— 您在哪个部门高就呀？",
            "💼 报销类型 —— 差旅费 / 招待费 / 办公用品 / 交通费，四选一",
            "💰 金额 —— 这次一共花了多少呀？",
            "📝 事由 —— 报销的主要事情或用途写在这里，财务小伙伴全靠它归类~",
            "",
            "【重点提醒 · 直接影响审核速度】",
            "🧾 票据状态 —— 有发票会加快审核进度；暂时没有发票，可以上传微信/支付宝"
            "的详细支付记录（「账单详情」页）先补信息",
            "",
            "【选填 · 补了更好】",
            "📍 城市 —— 出差住宿的话说一声城市，可以加快审核",
            "📅 发票日期 —— 像 2026/10/1 这样就行，不说我帮您按今天记",
            "",
            closer,
            "💡 也可以一句话搞定：「我是张三，研发部，出差住酒店 800 元，有发票」",
        ]
        return {
            "intent": "B",
            "answer": "\n".join(lines),
            "record_id": None,
            "bill_no": "",
            "draft": {},  #空草稿 承接后续补充
        }

    #填单主干 合并草稿后追问或提交
    def _handle_fill(self, question: str, base_draft=None, actor: Actor | None = None,
                     image_b64: str = "", image_mime: str = "image/jpeg",
                     invoice_fields: dict | None = None) -> dict:
        #抽取本轮字段（口述的票据状态只可能是 无票/未提，有票只来自证据）
        #吐槽句（「金额不是有显示吗」）先剥掉 免得被当成字段或事由
        _q = self._strip_complaint(question)
        f = self._extract_fields(_q)
        spoken_invoice = f.get("has_invoice") or ""  #本轮口述：无票 or 未提
        if base_draft:
            f = self._merge_draft(base_draft, f)
            #保留待确认标记
            if base_draft.get("await_confirm"):
                f["await_confirm"] = True
        #申请人恒等于登录身份；只有抽到像真名且不是本人时才拦截
        if actor is not None:
            spoken = str(f.get("applicant") or "").strip()
            if (spoken and spoken not in (actor.display_name, actor.username)
                    and _valid_person_name(spoken)):
                return {
                    "intent": "B",
                    "record_id": None,
                    "bill_no": "",
                    "answer": (f"🚫 报销单只能由本人提交：当前登录身份是 {actor.display_name}"
                               f"（{actor.employee_id or '无工号'}），不能替「{spoken}」报账。"
                               "请让本人登录后自己提交~"),
                    "draft": None,
                }
            f["applicant"] = actor.display_name

        #并入 OCR 结构化证据（金额/日期/类型/城市/票号/销售方/来源 直接覆盖，避免二次解析）
        #⚠️ 关键：只有「本轮确实是新上传」才允许票据值覆盖用户已填的字段。
        #前端会把暂存的票据随每一句话一起捎带 若无脑覆盖 用户改完的类型/金额会被弹回旧值。
        evidence = False
        fresh_upload = False
        _ocr_vals: dict = {}
        if invoice_fields:
            for _k in ("invoice_no", "invoice_seller"):
                _v = str(invoice_fields.get(_k) or "").strip()
                if _v:
                    f[_k] = _v[:64]
            #判断这是「新上传的一份票据」还是「同一份票据被重复携带」
            _uid = str(invoice_fields.get("upload_id") or "").strip()
            _sig = "|".join(str(invoice_fields.get(k) or "")
                            for k in ("invoice_no", "invoice_seller", "amount",
                                      "date", "category", "city", "source"))
            evidence = bool(
                str(invoice_fields.get("invoice_no") or "").strip()
                or str(invoice_fields.get("invoice_seller") or "").strip()
                or invoice_fields.get("source") == "voucher")
            if _uid:
                fresh_upload = _uid != str((base_draft or {}).get("ocr_upload_id") or "")
            else:
                #老客户端没带 upload_id → 用票据内容指纹比对
                fresh_upload = _sig != str((base_draft or {}).get("ocr_sig") or "")
            _amt = invoice_fields.get("amount")
            if _amt not in (None, "", 0):
                try:
                    _ocr_vals["amount"] = float(_amt)
                except (TypeError, ValueError):
                    pass
            for _k, _dst, _lim in (("date", "date", 24), ("category", "category", 16),
                                   ("city", "city", 16)):
                _v = str(invoice_fields.get(_k) or "").strip()
                if _v:
                    _ocr_vals[_dst] = _v[:_lim]
            for _k, _v in _ocr_vals.items():
                if _k == "amount" and evidence:
                    #金额锁定为发票金额（2026-10-30 用户拍板）：只要本轮有票据证据，
                    #文字金额一律让位；识别到发票金额即加锁，之后文字/定向修改都改不动
                    f[_k] = _v
                    f["amount_locked"] = True
                    continue
                #新上传 → 票据为准（问题2 以票为准）；重复携带 → 只补空 不覆盖用户的文字修改
                if fresh_upload or not f.get(_k):
                    f[_k] = _v
            if invoice_fields.get("source"):
                f["invoice_source"] = invoice_fields.get("source")
            if _uid:
                f["ocr_upload_id"] = _uid
            f["ocr_sig"] = _sig
        #票据状态判定（单一入口，方向唯一，无双向竞争）：
        #  证据在场        → 有票（实物/凭证最权威，能纠正口误）；
        #  已有票 + 口述无票 → 维持有票（必须有票才能提交，无票无意义，故不回退）；
        #  口述无票        → 无票；
        #  历史已是有票/无票 → 维持；其余 → 未提
        prev_invoice = (base_draft or {}).get("has_invoice")
        if evidence:
            f["has_invoice"] = "有票"
        elif spoken_invoice == "无票" and prev_invoice != "有票":
            f["has_invoice"] = "无票"
        elif prev_invoice in ("有票", "无票"):
            f["has_invoice"] = prev_invoice
        else:
            f["has_invoice"] = "未提"
        #冲突检测（仅当本轮是上传，避免历史提示复现）：
        #金额差 > 0.01 或类型不同 → 以发票为准并提示；事由保留用户原值并提醒核对
        conflict_notes: list = []
        #字段定向修改（「部门改成财务部」「金额修改为200」）：命中的字段按用户指定改
        _noted = self._apply_field_edits(
            f, _q, conflict_notes, base_draft=base_draft,
            fresh_upload=fresh_upload, ocr_keys=set(_ocr_vals))
        #口述「没发票」但单据里已经有票：不回退票据状态（必须有票才能提交），
        #但要把「按票据为准」明确说给用户听，不能默默吞掉用户的话
        if spoken_invoice == "无票" and prev_invoice == "有票":
            conflict_notes.append(
                "🧾 已收到您上传的票据，按「有票」计算（提交报销单需要票据）")
        #用户吐槽「金额不是有显示吗」：把识别到的内容如实回显 不说「读不到」这种假话
        if self._is_complaint_about_display(question):
            conflict_notes.append(self._recognized_replay(f))
        #用户用文字改了票据识别出来的值 → 明确告知「按您说的改了」
        #（票据状态不可改 但金额/类型/部门等都可以改 这是用户明确要求的）
        if evidence and base_draft and not fresh_upload:
            for _k, _label, _fmt in (("amount", "金额", "¥{v:g}"),
                                     ("category", "类型", "「{v}」"),
                                     ("city", "城市", "「{v}」")):
                if _k in _noted:  #定向修改已回执过 不重复播报
                    continue
                _ov = _ocr_vals.get(_k)
                _nv = f.get(_k)
                if _ov and _nv and str(_nv) != str(_ov):
                    conflict_notes.append(
                        f"✏️ 已按您说的把{_label}改为 " + _fmt.format(v=_nv))
        if evidence and base_draft and fresh_upload:
            _ba = base_draft.get("amount") or 0
            _oa = f.get("amount") or 0
            if _ba and _oa and abs(float(_oa) - float(_ba)) > 0.01:
                conflict_notes.append(
                    f"⚠️ 发票金额 ¥{_oa:g} 与您说的 ¥{_ba:g} 不一致，已按发票为准")
            _bc = (base_draft.get("category") or "").strip()
            _oc = (f.get("category") or "").strip()
            if _bc and _oc and _bc != _oc:
                conflict_notes.append(f"类型已按发票改为「{_oc}」")
            if str(base_draft.get("reason") or "").strip():
                conflict_notes.append(
                    "🔎 检测到新的报销信息，事由是否仍适用？如与发票不符，请直接文字补充修改")
        result = {"intent": "B", "record_id": None, "bill_no": "", "answer": "", "draft": f}

        #专项流程不直交 引导走专项
        if f.get("special"):
            result["draft"] = None  #清空草稿避免粘连
            result["answer"] = (
                f"🚦「{f['special']}」不在小财直交范围，需走专项流程提交~ "
                "（团建/通讯/培训/推广/加班餐/私车公用请按对应渠道办理，不要伪装成 4 类报销哦）"
            )
            return result

        #上一轮刚追问过事由 这一句里剔除已知字段后剩下的才当事由
        if (isinstance(base_draft, dict) and base_draft.get("await_reason")
                and not f.get("reason")):
            f["reason"] = self._reason_from_answer(self._strip_complaint(question), f)

        #必填项 申请人部门类型金额事由
        missing = []
        if not f.get("applicant"):
            missing.append("申请人")
        if not f.get("department"):
            missing.append("部门")
        if not f.get("category"):
            missing.append("报销类型")
        if not f.get("amount") or f["amount"] <= 0:
            missing.append("金额")
        if len(str(f.get("reason") or "").strip()) < 2:
            missing.append("事由")
        #只有「有票」才算票据齐了 无票/未提都要先把票补上
        if f.get("has_invoice") not in ("有票",):
            missing.append("票据")

        if missing:
            result["answer"] = self._ask_missing(f, missing, conflict_notes)
            #记下是否在等事由 供下一句直接承接
            result["draft"]["await_reason"] = "事由" in missing
            return result
        result["draft"]["await_reason"] = False

        #票据状态归一 未提不阻断
        has_invoice = f["has_invoice"] if f["has_invoice"] in ("有票", "无票") else "未提"

        #必填齐全 进入确认态 不直接写表
        if f.get("await_confirm") and self._is_confirm(question):
            answer, rid, bill_no = self._do_submit(
                f, has_invoice, actor=actor,
                image_b64=image_b64, image_mime=image_mime)
            result["answer"] = answer
            result["record_id"] = rid
            result["bill_no"] = bill_no
            #提交成功才清空草稿
            if rid:
                result["draft"] = None
            return result

        #未确认 回显核对等待确认提交
        result["draft"]["await_confirm"] = True
        result["answer"] = self._ask_confirm(f, has_invoice, conflict_notes)
        return result

    #主服务器唯一写入口
    def _submit_via_backend(self, f: dict, has_invoice: str, actor: Actor | None = None,
                            image_b64: str = "", image_mime: str = "image/jpeg"):
        #提交给主服务器 失败一律不写飞书
        from api import backend_client

        if actor is None:
            return ("🔐 登录状态已失效，请重新登录后再提交。", None, "")

        invoice_info = "；".join(
            x for x in (str(f.get("invoice_no") or "").strip(),
                        str(f.get("invoice_seller") or "").strip()) if x
        )
        #支付凭证（无发票号）单独标记，财务核对后定
        if f.get("invoice_source") == "voucher" and not str(f.get("invoice_no") or "").strip():
            invoice_info = ("支付凭证：" + invoice_info) if invoice_info else "支付凭证"
        try:
            data = backend_client.submit_with_token(
                actor.token, actor.sign_key,
                department=f.get("department", ""),
                rtype=f.get("category", ""),
                amount=f.get("amount", 0),
                reason=f.get("reason", ""),
                has_invoice=has_invoice,
                city=f.get("city", ""),
                date_str=f.get("date", ""),
                invoice_info=invoice_info,
            )
        except backend_client.BackendUnauthorized:
            return ("🔐 登录状态已失效，请重新登录后再提交。", None, "")
        except backend_client.BackendRejected as e:
            #业务规则拒绝 绝不绕过写入口
            return (f"⚠️ 这张单没有提交成功：{e}", None, "")
        except backend_client.BackendError as e:
            return (f"😥 主服务器暂时不可用，单据没有提交（{e}）。请稍后重试或联系管理员。", None, "")

        rid = data.get("record_id") or ""
        bill_no = data.get("bill_no") or ""
        if not rid:
            return (f"😥 主服务器没有返回单据编号（{data.get('message', '')}），请稍后在进度查询里确认。", None, "")

        #单据已落库 再把发票图片写进附件列 失败不阻断主流程
        attach_note = ""
        if image_b64:
            attach_note = self._attach_invoice(rid, image_b64, image_mime)

        parts = [f"👤 {f.get('applicant', '')}"]
        if f.get("department"):
            parts.append(f"🏢 {f['department']}")
        if f.get("category"):
            parts.append(f"💼 {f['category']}")
        parts.append(f"💰 ¥{float(f.get('amount') or 0):.0f}")
        if f.get("city"):
            parts.append(f"📍 {f['city']}")
        if f.get("reason"):
            parts.append(f"📝 {f['reason']}")

        answer_lines = [
            "✅ 报销单已成功提交啦！",
            "",
            f"📋 单号：{bill_no}" if bill_no else "📋 单号：已成功提交，稍后可在「进度查询」中输入姓名查看",
            " · ".join(parts),
            "📊 当前状态：待审核",
        ]
        hints = XiaoCaiAgent._soft_hints(f)
        if hints:
            answer_lines.append("")
            answer_lines.extend(hints)
        if attach_note:
            answer_lines.append(attach_note)
        answer_lines.append("财务小伙伴会尽快审核，结果出来会有通知~")
        return ("\n".join(answer_lines), rid, bill_no)

    #确认后真正写表
    def _do_submit(self, f: dict, has_invoice: str,
                   compliance: str | None = None, risk_summary: str | None = None,
                   actor: Actor | None = None,
                   image_b64: str = "", image_mime: str = "image/jpeg"):
        #预审不通过不提交 通过则走主服务器唯一写入口
        if compliance is None:
            compliance, risk_summary = self._compliance_check({**f, "has_invoice": has_invoice})
        if compliance == "阻断":
            return (f"🧾 {risk_summary}，补齐合规发票后我马上帮您提交~", None, "")
        #网关侧硬性要求：只有「有票」才允许提交（无票/未提一律拦住）
        if has_invoice != "有票":
            return ("🧾 请先补上发票，或上传微信/支付宝「账单详情」页的支付记录，"
                    "我才能帮您提交~", None, "")
        return self._submit_via_backend(f, has_invoice, actor=actor,
                                        image_b64=image_b64, image_mime=image_mime)

    #发票附件存档
    _MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

    def _attach_invoice(self, record_id: str, image_b64: str, image_mime: str = "image/jpeg") -> str:
        #上传发票附件并关联单据
        try:
            import base64 as _b64
            file_bytes = _b64.b64decode(image_b64, validate=True)
        except Exception as e:
            return f"📎 发票图片数据异常，未存档（{e}）；单据本身已提交成功"
        if not file_bytes:
            return "📎 发票图片为空，未存档；单据本身已提交成功"
        ext = self._MIME_EXT.get(image_mime, "jpg")
        try:
            token = self.feishu.upload_attachment(f"发票.{ext}", file_bytes, image_mime)
            names = self.feishu.field_names(self.table_id)
            if "发票附件" not in names:
                return ("📎 发票图片已上传，但当前表暂无「发票附件」列，未关联到单据"
                        "（可按 TABLE_SETUP 手册加列后重传）")
            self.feishu.update_record(self.table_id, record_id,
                                      {"发票附件": [{"file_token": token}]})
            return "📎 发票图片已存档到这张报销单，财务可直接点开核对~"
        except FeishuError as e:
            return f"📎 发票图片存档未成功（{e}）；单据本身已提交成功，可稍后补传"
        except Exception as e:  #附件异常不影响主流程
            return f"📎 发票图片存档未成功（{e}）；单据本身已提交成功，可稍后补传"

    #合规预审 只给结论不建单
    def _handle_compliance(self, question: str, base_draft=None, actor: Actor | None = None) -> dict:
        #抽取并合并草稿后预审
        f = self._extract_fields(question)
        if base_draft:
            f = self._merge_draft(base_draft, f)
        result = {"intent": "C", "compliance": "", "risk_summary": "", "record_id": None, "answer": ""}

        if f["special"]:
            result["answer"] = (
                f"🚦「{f['special']}」不在小财直交范围，须走专项流程，无法按 4 类预审哦~"
            )
            return result

        compliance, risk_summary = self._compliance_check(f)
        result["compliance"] = compliance
        result["risk_summary"] = risk_summary

        if compliance == "阻断":
            result["answer"] = f"🧾 {risk_summary}，补齐合规发票后我再帮您预审~"
            return result

        if compliance == "通过":
            head = "✅ 合规预审：通过，无风险点。"
        else:
            head = f"✅ 合规预审：存在风险。风险点：{risk_summary}"
        #预审不建单 建单只能由申请人本人走填单分支
        result["answer"] = (
            head + "\n\n💡 本预审只给结论、不创建单据；"
            "正式提交请由申请人本人到「填单」里确认提交。"
        )
        return result

    #进度查询 按申请人或单号
    def _d_extract_keyword(self, question: str) -> str:
        #进度查询词抽取
        q = question or ""
        bx = re.search(r"(BX-?\d[\d-]*)", q)
        if bx:
            return bx.group(1)

        def _valid(name: str) -> str:
            #姓名校验 过滤伪姓名
            name = (name or "").strip()
            if not re.fullmatch(r"[一-龥]{2,4}", name):
                return ""
            if name in _D_NAME_BLOCKLIST or name in _SKIP_NAME:
                return ""
            if any(ch in _D_PRONOUN_CHARS for ch in name):
                return ""
            if any(ch in _NON_NAME_CHARS for ch in name):
                return ""
            return name

        #姓名加查询词的组合句式
        for pat in (
            r"查(?:询|一下)?\s*([一-龥]{2,4}?)\s*的?\s*(?:进度|报销|单号|记录)",
            r"([一-龥]{2,4}?)\s*的\s*(?:进度|报销|记录|单号|结果)",
        ):
            m = re.search(pat, q)
            if m:
                name = _valid(m.group(1))
                if name:
                    return name

        #纯姓名 可带语气后缀
        bare = _valid(re.sub(r"[的了吧呀哦呢]+$", "", q.strip()))
        if bare:
            return bare

        #兜底取申请人或工号
        f = self._extract_fields(q)
        if f["applicant"] and f["applicant"] not in _D_NAME_BLOCKLIST:
            return f["applicant"]
        am = re.search(r"([A-Za-z0-9]{2,})", q)
        return am.group(1) if am else ""

    @staticmethod
    def _record_hit(keyword: str, row: dict, question: str) -> bool:
        #只比对单号与申请人 不做任意字段匹配
        if not keyword:
            return False
        no = str(row.get("单号") or "").strip()
        if no:
            k = keyword.upper().replace("-", "")
            n = no.upper().replace("-", "")
            if k in n or n in k:
                return True
        applicant = str(row.get("申请人") or "").strip()
        return bool(applicant) and (keyword in applicant or applicant in keyword)

    #把主服务器返回的行转成中文键视图 前端按中文键渲染
    @staticmethod
    def _csharp_row_view(row: dict) -> dict:
        row = row or {}
        attach = ""
        raw = row.get("raw")
        blob = raw.get("发票附件") if isinstance(raw, dict) else None
        if isinstance(blob, str) and blob.strip():
            try:
                parsed = json.loads(blob)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                attach = "、".join(
                    str(i.get("name") or "附件") if isinstance(i, dict) else str(i)
                    for i in parsed if i
                )
            else:
                attach = blob.strip()
        return {
            "单号": row.get("billNo") or "",
            "申请人": row.get("applicant") or "",
            "工号": row.get("employeeId") or "",
            "部门": row.get("department") or "",
            "报销类型": row.get("rtype") or "",
            "金额": row.get("amount"),
            "事由": row.get("reason") or "",
            "票据状态": row.get("hasInvoice") or "",
            "城市": row.get("city") or "",
            "发票日期": row.get("invoiceDate") or "",
            "记录提交时间": row.get("submittedAt") or "",
            "合规结果": row.get("compliance") or "",
            "风险摘要": row.get("riskSummary") or "",
            "状态": row.get("status") or "",
            "审核人": row.get("reviewer") or "",
            "审批意见": row.get("reviewOpinion") or "",
            "审批时间": row.get("reviewedAt") or "",
            "发票信息": row.get("invoiceInfo") or "",
            "发票附件": attach,
            "record_id": row.get("recordId") or "",
        }

    #展示格式化
    @staticmethod
    def _fmt_ts(v) -> str:
        #毫秒时间戳转日期时间串
        if v in (None, ""):
            return ""
        try:
            n = float(v)
        except (TypeError, ValueError):
            return str(v).strip()
        if n <= 0:
            return ""
        try:
            return datetime.fromtimestamp(n / 1000).strftime("%Y/%m/%d %H:%M")
        except (OverflowError, OSError, ValueError):
            return str(v)

    def _handle_progress(self, question: str, actor: Actor | None = None) -> dict:
        #查询范围由主服务器按令牌角色裁剪 这里只做关键词过滤与展示
        result = {"intent": "D", "records": [], "answer": ""}
        if actor is None:
            result["answer"] = "🔐 请先登录后再查询报销进度。"
            return result

        keyword = self._d_extract_keyword(question)
        #泛指的“所有人/全部”之类不算筛选条件
        if keyword in _D_GENERIC_KEYWORDS:
            keyword = ""
        #没有查看全部权限的角色 只查本人
        if not actor.can(PERM_INVOICE_READ_ALL):
            looks_like_bill = bool(re.match(r"^BX-?\d", keyword or "", re.I))
            if not looks_like_bill:
                keyword = actor.display_name or actor.username

        if not keyword and not actor.can(PERM_INVOICE_READ_ALL):
            result["answer"] = "⏳ 请提供姓名或报销单号（如 BX000013），我帮您查进度~"
            return result

        from api import backend_client
        try:
            data = backend_client.list_invoices(actor.token, actor.sign_key, limit=500)
        except backend_client.BackendUnauthorized:
            result["answer"] = "🔐 登录状态已失效，请重新登录后再查询。"
            return result
        except backend_client.BackendError as e:
            result["answer"] = f"⏳ 报销单查询失败（{e}），建议稍后重试或联系财务同学~"
            return result

        rows = [self._csharp_row_view(r) for r in (data.get("rows") or [])]
        hits = [r for r in rows if not keyword or self._record_hit(keyword, r, question)]
        #按提交时间倒序 最新的排最前 再截断
        hits.sort(key=lambda r: _ms_key(r.get("记录提交时间")), reverse=True)
        total = len(hits)
        matched = hits[:_D_LIST_LIMIT]
        result["records"] = matched
        if not matched:
            result["answer"] = "⏳ 没有找到相关的报销记录呢~ 请确认姓名或单号是否正确？"
            return result

        #明细交给前端卡片渲染 这里只给一句摘要 避免同一批单据重复展示两遍
        lines = [f"好的，帮您查到 {len(matched)} 条~ 明细在下方卡片里"]
        if total > len(matched):
            lines.append(f"（范围内共 {total} 条，这里显示最近 {len(matched)} 条；"
                         "查更早的单请提供单号）")
        if str(data.get("scope") or "") == "mine":
            lines.append("💡 以上只包含您本人的单据。")
        lines.append("💡 有驳回单建议联系审核人，待审核请耐心等待审批~")
        result["answer"] = "\n".join(lines)
        return result

    #便捷构造
    @classmethod
    def from_config(cls, compressor=None, top_k: int = 3, default_category="报销制度", with_feishu: bool = True):
        #按 .env 与知识库一键装配
        llm = LLMAdapter.from_env()
        retriever = Retriever.from_directory(knowledge_dir())
        feishu = None
        table_id = None
        if with_feishu and os.getenv("FEISHU_APP_ID"):
            feishu = FeishuBitableClient.from_env()
            table_id = os.getenv("FEISHU_TABLE_ID", "")
        return cls(
            llm,
            retriever,
            compressor=compressor,
            top_k=top_k,
            default_category=default_category,
            feishu=feishu,
            table_id=table_id,
        )
