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
    core = raw.replace("部门", "").replace("部", "").strip().lower()
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
            if not self._has_strong_intent(question) and not self._is_explicit_question(question):
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

    #本地规则抽取字段 三类分支共用
    @staticmethod
    def _extract_fields(text: str) -> dict:
        t = text or ""
        result = {
            "applicant": "", "department": "", "category": "", "amount": 0.0,
            "reason": "", "has_invoice": "", "city": "", "date": "", "special": None,
        }

        #金额
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|￥|¥|rmb|RMB)?", t)
        if m:
            try:
                result["amount"] = float(m.group(1))
            except ValueError:
                result["amount"] = 0.0

        #票据状态 先判无票
        if re.search(r"没发票|无发票|没票|丢了|没有发票|没开|未开|没开发票", t):
            result["has_invoice"] = "无票"
        elif re.search(r"有票|有发票|电子发票|已开|开票了|开了票|发票有|发票都留着|发票在|带着发票", t):
            result["has_invoice"] = "有票"
        else:
            result["has_invoice"] = "未提"

        #先判专项流程类别
        for name, kws in SPECIAL_CATEGORY_MAP:
            if any(k in t for k in kws):
                result["special"] = name
                break

        #报销类型 先精确字面再模糊映射
        if not result["special"]:
            for cat in ALLOWED_TYPES:
                if cat in t:
                    result["category"] = cat
                    break
            if not result["category"]:
                for cat, kws in CATEGORY_MAP:
                    if any(k in t for k in kws):
                        result["category"] = cat
                        break

        #城市仅在白名单内匹配
        for c in CITY_KEYWORDS:
            if c in t:
                result["city"] = c
                break

        #部门抽取并归一为标准枚举
        m = re.search(r"([一-龥]{1,6}部门)", t)
        if not m:
            m = re.search(r"部门([一-龥]{1,6})", t)
        if not m:
            m = re.search(r"([一-龥]{2,6}部)(?=[，,\s。的]|$)", t)
        if m:
            result["department"] = normalize_department(m.group(1))

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
                "先补信息。也可以直接回复「有发票」或「没发票」。")

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
        #剔除金额与单位
        leftover = re.sub(r"\d+(?:\.\d+)?\s*(?:元|块|钱|万)?", " ", leftover)
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
    def _ask_missing(self, f: dict, missing: list) -> str:
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
            filled.append(f"💰 金额：¥{f['amount']:.0f}")
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
        lines.append("")
        if f.get("await_reason") and "事由" in missing:
            lines.append("事由单独补一句就行，已有的信息不用重复~")
        else:
            lines.append("直接一句话补充就行，补齐后我请您核对。")
        return "\n".join(lines)

    #确认前回显摘要
    def _ask_confirm(self, f: dict, has_invoice: str) -> str:
        #回显已填信息待用户确认
        lines = ["✨ 信息齐了，麻烦您核对一下~"]
        rows = [
            f"👤 申请人：{f['applicant']}",
            f"🏢 部门：{f['department']}",
            f"💼 报销类型：{f['category']}",
            f"💰 金额：¥{f['amount']:.0f}",
        ]
        if f.get("city"):
            rows.append(f"📍 城市：{f['city']}")
        if f.get("reason"):
            rows.append(f"📝 事由：{f['reason']}")
        rows.append(f"🧾 票据：{has_invoice}")
        lines.append("📋 当前已填信息：")
        lines.extend(rows)

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
        lines.append("✅ 补齐发票后回复我，我马上帮您提交~" if blocked
                     else "✅ 确认无误请回复「确认提交」；有误请直接说要改哪一项，我帮您改~")
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
            merged[k] = v
        return merged

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
        #抽取本轮字段并合并历史草稿
        f = self._extract_fields(question)
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
        #并入 OCR 发票号与销售方
        if invoice_fields:
            for _k in ("invoice_no", "invoice_seller"):
                _v = str(invoice_fields.get(_k) or "").strip()
                if _v:
                    f[_k] = _v[:64]
        #只要识别出发票号就判为有票：图片证据优先于口述（含"没发票"也要纠正）
        if str(f.get("invoice_no") or "").strip():
            f["has_invoice"] = "有票"
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
            f["reason"] = self._reason_from_answer(question, f)

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
            result["answer"] = self._ask_missing(f, missing)
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
        result["answer"] = self._ask_confirm(f, has_invoice)
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
