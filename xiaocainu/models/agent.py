"""
M4 · 编排路由层（B/C/D 业务落地版）
================================
设计要点
--------
- XiaoCaiAgent 把 M1(LLM) + M2(检索) 组合，是「小财」对外统一入口。
- 分支路由：A 咨询(报销制度) / B 填单引导 / C 合规预审 / D 进度查询(飞书)。
- **本版重写 B/C/D 业务逻辑**，严格对齐：填单规则.md（抽取契约+枚举）+ 小财奴.md（追问/提交/查询话术）
  + 报销制度.md（合规预审阈值）+ 真实飞书表 tbl5Dz4SdKmmwgP5 schema。
- 抽取方式：本地正则+关键词（零 API 成本、确定性、可离线单测）。
  填单规则.md 的 LLM 抽取提示词保留为常量 FILL_EXTRACT_SYSTEM_PROMPT，将来切 LLM 模式一行切换。
- 全程离线可测：测试注入 MockProvider + 内存 retriever + MockFeishu，不碰任何付费 API。
"""
from __future__ import annotations

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
    build_reimbursement_record,
    filter_by_schema,
    public_view,
    ALLOWED_TYPES,
    DEPT_WHITELIST,
    CITY_WHITELIST,
    CITY_TIER,
    validate_enum_fields,
)
from .users import ROLE_EMPLOYEE, ROLE_FINANCE, ROLE_ADMIN

SYSTEM_CONSULT = (
    "你是小财，一个企业财务报销助手。只能依据下方[资料]回答用户问题；"
    "若资料中没有相关信息，如实说明「资料中未提及」，不要编造或猜测。"
    "回答简洁、用中文、面向普通员工。"
    "严禁输出「已记录/预审通过/提交到飞书/确认后帮您提交」等系统操作话术，"
    "也不要复述资料里的示例对话；你只负责解答报销制度疑问。"
)

# 填单规则.md 的 LLM 抽取提示词（备用：将来把 _extract_fields 切到 LLM 模式时使用，不默认启用）
FILL_EXTRACT_SYSTEM_PROMPT = (
    "你是报销信息的结构化抽取引擎，只输出一个 JSON 对象（字段：applicant/department/"
    "category/amount/reason/has_invoice/city/date），category 仅取差旅费/招待费/办公用品/"
    "交通费，has_invoice 仅取有票/无票/未提，date 格式 YYYY/M/D。"
)

# 意图识别：按关键词命中分支；均未命中 → 兜底 A 咨询
INTENT_RULES = [
    ("D", ["进度", "到哪", "查一下", "我的报销单", "审批没", "到账", "现在什么状态", "查询", "查"]),
    ("B", ["填", "提交", "上传", "创建", "新建", "开报销单", "怎么报", "报一笔",
           "帮我报", "我要报销", "我要报", "报销单"]),
    ("C", ["合规", "预审", "能不能报", "有没有风险", "超标", "能不能过"]),
]

# ---------------------------------------------------- 抽取用词典（本地规则）
# 城市白名单（差旅分级主数据；抽取与分级共用，来自 feishu.CITY_WHITELIST 权威枚举）
CITY_KEYWORDS = CITY_WHITELIST

# 部门口语化简称 → 企业标准枚举的「显式」映射（绝不靠子串猜测，避免误归到错误部门）。
# 仅收录明确可确定的简称；其余一律留空交由机器人追问，杜绝写出未定义部门值。
DEPT_ALIASES = {
    "研发": "研发部", "研发部": "研发部",
    "技术": "信息技术部", "信息技术": "信息技术部", "信息": "信息技术部", "it": "信息技术部",
    "人力": "人力资源部", "人力资源": "人力资源部", "hr": "人力资源部",
    "行政": "行政部", "财务": "财务部", "市场": "市场部", "销售": "销售部",
    "生产": "生产部", "采购": "采购部", "质管": "质量管理部", "质量": "质量管理部",
    "客服": "客户服务部", "客户": "客户服务部",
}


def normalize_department(raw: str) -> str:
    """部门抽取归一路由：把用户模糊输入映射到企业标准枚举（部门字典）。

    规则：精确命中字典 → 原样返回；否则取剥离「部门/部」后的核心词查 DEPT_ALIASES
    显式映射（如「研发部门/研发」→「研发部」、「技术部」→「信息技术部」）。
    不在显式映射中的（如「电脑部门」「管理部门」「火星部」）一律返空，
    交由机器人追问，绝不允许写出未定义的部门值。
    """
    if not raw:
        return ""
    if raw in DEPT_WHITELIST:
        return raw
    core = raw.replace("部门", "").replace("部", "").strip().lower()
    return DEPT_ALIASES.get(core, "")

# 报销类型模糊映射（命中即归为对应直交类）
# 顺序：招待费置顶——"出差+请客户吃饭"共存时按填单规则示例归为招待费。
CATEGORY_MAP = [
    ("招待费", ["招待", "请客", "客户吃饭", "吃饭", "用餐", "宴请", "业务招待"]),
    ("差旅费", ["差旅", "出差", "住酒店", "住宿", "高铁", "车票", "机票", "火车"]),
    ("办公用品", ["办公用品", "办公", "采购", "文具", "耗材", "低值"]),
    ("交通费", ["交通费", "打车", "滴滴", "加油", "油费", "过路费", "市内交通"]),
]

# 专项流程类别（bot 不直交，须引导走专项，严禁伪装塞入 4 类）
SPECIAL_CATEGORY_MAP = [
    ("团建/员工活动", ["团建", "员工活动", "活动费"]),
    ("通讯费", ["通讯费", "话费", "电话费"]),
    ("培训费", ["培训", "职业发展"]),
    ("市场推广费", ["推广", "宣传", "市场推广"]),
    ("加班餐补", ["加班餐", "误餐", "餐补"]),
    ("私车公用", ["私车公用", "公里补贴", "里程"]),
]

# 显式政策问句标记（用于路由：明显的咨询问句应放行到 A，不被填单上下文粘连）
_A_QUESTION_MARKERS = ("吗", "呢", "？", "?", "多少", "什么", "标准", "规定", "介绍",
                      "说明", "可以", "能否", "是否", "为什么", "有啥")

# 填单引导口令（前端「📝 填单引导」按钮发送）：直接输出全部参数的友好引导，不进入抽取流程
FILL_GUIDE_TRIGGERS = {"填单引导", "填单指南"}

# 引导话术素材（开场/收尾随机切换，让每次点按钮的措辞有多样性；要点内容固定）
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
# 报销类型词（无疑问词时视为填单续答，避免「办公费 20块」被兜底成咨询）
_TYPE_WORDS = ("差旅费", "招待费", "办公费", "交通费", "办公用品", "报销")
# 兜底姓名抽取时的非姓名排除词（常见口语/代词）
_SKIP_NAME = {"好的", "谢谢", "没有", "麻烦", "请问", "帮我", "这个", "那个",
              "我们", "你们", "他们", "她们", "自己", "大家", "没事", "在哪", "这里"}
# 兜底姓名抽取时，token 中若含这些「非人名」字符则直接排除（业务/代词/虚词/疑问/数字等）
_NON_NAME_CHARS = set(
    "票费电发开报填审请查购采差出住餐吃车机油路话训推广活动团私司单金额类型部门"
    "理由事因为用于您我他她们这那没了的是不也都还再就才已有和与否如果比较标准规定"
    "介绍说明说么怎帮想要去回在哪能可该应或等及与"
)

# 中文数字（日期/月份/日用）
_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
    "八": 8, "九": 9, "十": 10, "〇": 0,
}

# D 进度查询：不是人名的「伪姓名」词与代词字符（防止把「进度/我的」当姓名去查）
_D_NAME_BLOCKLIST = {"进度", "报销", "记录", "单号", "状态", "结果", "详情", "信息"}
_D_PRONOUN_CHARS = set("我你他她它谁咱")


def _cn_num_token(tok: str):
    """中文/阿拉伯数字(≤31) → int；无法解析返回 None。支持 十/十一/十二/二十/二十三。"""
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
    """中文/阿拉伯日期 → 东八区当年默认 'YYYY/M/D'；无法解析返回 ''。"""
    if not text:
        return ""
    # 阿拉伯：2026/9/10, 2026-9-10, 2026.9.10
    m = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", text)
    if m:
        return f"{m.group(1)}/{int(m.group(2))}/{int(m.group(3))}"
    # 中文带年：2026年9月10号
    m = re.search(r"(\d{4})年\s*([0-9一-龥]{1,3})月\s*([0-9一-龥]{1,3})[日号]?", text)
    if m:
        mo = _cn_num_token(m.group(2))
        da = _cn_num_token(m.group(3))
        if mo and da:
            return f"{m.group(1)}/{mo}/{da}"
    # 中文不带年：9月10号 / 三月二号
    m = re.search(r"([0-9一-龥]{1,3})月\s*([0-9一-龥]{1,3})[日号]", text)
    if m:
        mo = _cn_num_token(m.group(1))
        da = _cn_num_token(m.group(2))
        if mo and da:
            return f"{datetime.now().year}/{mo}/{da}"
    return ""


class XiaoCaiAgent:
    """小财智能体：检索 + 生成 + 分支路由的统一编排。

    参数
    ----
    llm        : LLMAdapter（M1）
    retriever  : Retriever（M2；若需 rerank，构造时传入 reranker=...）
    compressor : 可选 compress(query,text)->str（M2 可插拔压缩），None 关闭
    top_k      : 检索返回条数（默认 3）
    default_category : 默认检索来源类别（如 "报销制度" 限定 A 分支）
    """

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
        self.llm = llm
        self.retriever = retriever
        self.compressor = compressor
        self.top_k = top_k
        self.default_category = default_category
        self.feishu = feishu          # M3 飞书客户端（可插拔，未接入时 B/C/D 降级）
        self.table_id = table_id      # 报销单主表 table_id

    # ---------------------------------------------------------- 意图识别
    def classify_intent(self, text: str) -> str:
        t = text or ""
        # D 进度查询（最高优先）
        if any(k in t for k in ("进度", "到哪", "查一下", "我的报销单", "审批没",
                                "到账", "现在什么状态", "查询", "查")):
            return "D"
        # C 合规预审
        if any(k in t for k in ("合规", "预审", "能不能报", "有没有风险", "超标", "能不能过")):
            return "C"
        # A 咨询：明显的提问句式优先于 B（避免把"XX报销多少需要审批"判成填单）
        # 注意：不放"怎么"，否则"怎么提交报销/怎么报"会被误判为咨询，实际属 B 填单引导。
        if any(q in t for q in _A_QUESTION_MARKERS):
            return "A"
        # B 填单引导：含报销类动作/类型/票据 即视为提交意图
        if any(k in t for k in ("填", "提交", "上传", "创建", "新建", "开报销单", "怎么报",
                                "报一笔", "帮我报", "我要报销", "我要报", "我想报",
                                "报销单", "报销", "发票")):
            return "B"
        # B 续答：含 4 类报销类型词（无疑问词）→ 视为填单，避免「办公费 20块」被兜底成咨询
        if any(k in t for k in _TYPE_WORDS):
            return "B"
        return "A"

    # ---------------------------------------------------------- 路由辅助（上下文粘性）
    @staticmethod
    def _has_strong_intent(text: str) -> bool:
        """明确要离开填单的强意图：D 进度 / C 合规。"""
        t = text or ""
        return any(k in t for k in ("进度", "到哪", "查一下", "我的报销单", "审批没",
                                    "到账", "现在什么状态", "查询", "查",
                                    "合规", "预审", "能不能报", "有没有风险", "超标", "能不能过"))

    @staticmethod
    def _is_explicit_question(text: str) -> bool:
        """明显政策问句（有疑问标记）→ 应放行到 A，不被填单粘连。"""
        t = text or ""
        return any(q in t for q in _A_QUESTION_MARKERS)

    @classmethod
    def _progress_followup_keyword(cls, question: str) -> str:
        """D 续答的关键词识别：BX 单号（可带「单号」前缀）或纯姓名（可带后缀/「我是XX」句式）。"""
        t = (question or "").strip()
        # 单号：BX-000098 / BX000098 / 单号 BX-000098
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
        """D 续答识别：上一轮机器人刚要求「提供姓名或报销单号」，用户回的是姓名或单号。

        兼容「小新的」「张三吧」「我是张三」以及「BX-000098」——否则纯单号会被
        兜底成 A 咨询，造成「查询进度→回单号→答非所问」的混淆。
        """
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
        """草稿仍在进行中：必填项（申请人/部门/类型/金额）未齐，或票据为「无票」阻断态。"""
        if not isinstance(draft, dict):
            return False
        missing_core = not (draft.get("applicant") and draft.get("department")
                           and draft.get("category") and draft.get("amount"))
        blocked = draft.get("has_invoice") == "无票"
        return bool(missing_core or blocked)

    # ---------------------------------------------------------- 统一入口
    def route(self, question: str, history=None, base_draft=None, user: dict | None = None,
              image_b64: str = "", image_mime: str = "image/jpeg",
              invoice_fields: dict | None = None) -> dict:
        """统一路由。

        history    : 多轮对话历史（A 分支喂给 LLM，实现 chat 记忆）
        base_draft : 上一轮已抽取的 B/C 草稿，跨轮合并补全字段
        user       : 登录用户上下文 {username,name,role,department,employee_id}；
                     None=匿名（旧行为，离线测试/未登录兼容）。传入时启用角色权限：
                     员工 C 预审拒绝、B 自动带身份、D 强制查自己。
        image_b64  : M9 可选，用户上传的发票图片（base64，不带 data: 前缀）；
                     仅在「确认提交」真正写库成功后用于上传飞书附件，draft 中绝不保存。
        image_mime : 发票图片 MIME（image/jpeg|png|webp）。
        invoice_fields : M9 可选，OCR 识别的 {invoice_no, invoice_seller}，并入草稿写「发票信息」列。
        """
        # 填单引导口令（前端按钮）：直接输出全部参数的友好引导，不进入抽取流程
        if (question or "").strip() in FILL_GUIDE_TRIGGERS:
            return self._handle_fill_guide()
        intent = self.classify_intent(question)
        # 上下文粘性：填单进行中（存在未完草稿 / 待确认态），且本轮非明确 D/C 意图、也非明显政策问句，
        # 则继续留在 B 填单——避免「办公费 20块」「有电子发票」「确认」等续答被误判成咨询。
        _sticky = self._is_fill_in_progress(base_draft) or (isinstance(base_draft, dict) and base_draft.get("await_confirm"))
        if intent != "B" and base_draft and _sticky:
            if not self._has_strong_intent(question) and not self._is_explicit_question(question):
                return self._handle_fill(question, base_draft=base_draft, user=user,
                                         image_b64=image_b64, image_mime=image_mime,
                                         invoice_fields=invoice_fields)
        # D 续答：机器人刚要求提供姓名/单号，用户只回一个纯姓名 → 继续走进度查询（不误判成咨询）
        if intent == "A" and self._is_progress_name_followup(question, history):
            return self._handle_progress(question, user=user)
        if intent == "A":
            return self._handle_consult(question, history=history)
        if intent == "B":
            return self._handle_fill(question, base_draft=base_draft, user=user,
                                     image_b64=image_b64, image_mime=image_mime,
                                     invoice_fields=invoice_fields)
        if intent == "C":
            # M8 角色闸：合规预审仅财务/管理员（服务端强制，前端隐藏按钮只是 UX）
            if user and user.get("role") not in (ROLE_FINANCE, ROLE_ADMIN):
                return {
                    "intent": "C",
                    "answer": "🛡️ 合规预审仅对财务/管理员开放哦~ "
                              "您可以把单子提交上来等财务预审，有疑问也可以直接问我报销制度。",
                    "compliance": "", "risk_summary": "", "record_id": None,
                }
            return self._handle_compliance(question, base_draft=base_draft, user=user)
        return self._handle_progress(question, user=user)

    # ---------------------------------------------------------- A 咨询分支
    def _handle_consult(self, question: str, history=None) -> dict:
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

    # ---------------------------------------------------------- 轻量字段抽取（B/C/D 共用，本地规则）
    @staticmethod
    def _extract_fields(text: str) -> dict:
        t = text or ""
        result = {
            "applicant": "", "department": "", "category": "", "amount": 0.0,
            "reason": "", "has_invoice": "", "city": "", "date": "", "special": None,
        }

        # 金额
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|￥|¥|rmb|RMB)?", t)
        if m:
            try:
                result["amount"] = float(m.group(1))
            except ValueError:
                result["amount"] = 0.0

        # 票据状态：无票优先判定（含「没开发票」等否定式，避免被「开票」误判为有票）
        if re.search(r"没发票|无发票|没票|丢了|没有发票|没开|未开|没开发票", t):
            result["has_invoice"] = "无票"
        elif re.search(r"有票|有发票|电子发票|已开|开票了|开了票|发票有|发票都留着|发票在|带着发票", t):
            result["has_invoice"] = "有票"
        else:
            result["has_invoice"] = "未提"

        # 专项流程类别（优先判定，避免伪装成 4 类）
        for name, kws in SPECIAL_CATEGORY_MAP:
            if any(k in t for k in kws):
                result["special"] = name
                break

        # 报销类型：先精确字面匹配 4 类（最高优先），再模糊映射
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

        # 城市：仅在白名单内匹配（差旅分级主数据），非白名单城市一律不抽取
        for c in CITY_KEYWORDS:
            if c in t:
                result["city"] = c
                break

        # 部门：XX部门 / 部门XX / XX部 → 归一路由到企业标准枚举（无法确定则留空）
        m = re.search(r"([一-龥]{1,6}部门)", t)
        if not m:
            m = re.search(r"部门([一-龥]{1,6})", t)
        if not m:
            m = re.search(r"([一-龥]{2,6}部)(?=[，,\s。的]|$)", t)
        if m:
            result["department"] = normalize_department(m.group(1))

        # 申请人：我是XXX / XXX，部门 / 查询中的姓名（查一下XXX、XXX报销） / 兜底 token
        m = re.search(r"我是\s*([一-龥]{2,4})", t)
        if not m:
            m = re.search(r"([一-龥]{2,4})\s*[,，]?\s*(?:研发部|市场部|传媒部门|电脑部门|部门)", t)
        if not m:
            m = re.search(r"查一下\s*([一-龥]{2,3})", t) or re.search(r"查\s*([一-龥]{2,3})的?报销", t)
        if m:
            result["applicant"] = m.group(1)
        else:
            # 兜底姓名抽取：仅采纳「2~3 个汉字且不含业务/代词/虚词字符」的 token，
            # 避免把「有电子发票」「办公费」等噪声截成名字（如误把"有电子发"当姓名）。
            for tk in re.findall(r"[一-龥]{2,3}", t):
                if tk in _SKIP_NAME:
                    continue
                if any(ch in _NON_NAME_CHARS for ch in tk):
                    continue
                result["applicant"] = tk
                break

        # 事由：事由/用于/因为 之后；或类别动作后的描述
        m = re.search(r"(?:事由|用于|因为)[：: ]?([一-龥0-9，,。]{2,30})", t)
        if m:
            result["reason"] = m.group(1).strip(" ，,。")
        else:
            # 动作词后的描述段；但须含≥2个非"费/元/数字/标点"字，避免把
            # 「出差费200元」这类"类型+金额"误当事由（那会污染事由字段并屏蔽事由提示）
            m = re.search(r"(?:出差|请客|采购|打车)([一-龥0-9，,。 ]{2,20})", t)
            if m:
                tail = m.group(1).strip(" ，,。")
                if len(re.sub(r"[费元\s，,。0-9]", "", tail)) >= 2:
                    result["reason"] = m.group(0).strip(" ，,。")

        # 日期
        result["date"] = _normalize_date(t)
        return result

    # ---------------------------------------------------------- 合规预审核心（按报销制度.md 六）
    @staticmethod
    def _city_tier(city: str):
        """差旅住宿分级上限（元/晚）：一线 500 / 新一线 350 / 二线 250；空城市返回 None。"""
        if not city:
            return None
        return CITY_TIER.get(city, 250)  # 不在白名单（理论上不会）→ 三线及以下 250

    def _compliance_check(self, f: dict):
        """按《报销制度》六 产出 (合规结果, 风险摘要)。仅 通过 / 风险 / 阻断 三态。

        - 阻断：无票（硬性不合规），提交前无法放行。
        - 风险：超额/超时/票实不符等需关注项，仍允许提交但须写摘要。
        - 通过：无风险点。
        """
        risks: list = []
        cat = f.get("category", "")
        amount = f.get("amount", 0) or 0
        has_invoice = f.get("has_invoice", "")
        date_str = f.get("date", "")
        city = f.get("city", "")

        # 票据：无票 → 阻断；未提 → 仅提示（提交前需确认）
        if has_invoice == "无票":
            return ("阻断", "票据状态为「无票」，缺少合规发票，提交前无法放行")
        if has_invoice == "未提":
            risks.append("票据状态未提，提交前需确认合规发票")

        # 额度
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

        # 时限：距开票 > 30 天
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

    # ---------------------------------------------------------- 取回可读单号（AutoNumber）
    def _fit_schema(self, rec: dict) -> dict:
        """按真实表字段集过滤记录（M8/P5）：旧 14 字段表无 工号/审批意见/审批时间 列时自动剔除，
        防止「未知字段导致整单写入失败」。Mock/离线客户端无 field_names 则原样返回；
        探测失败也原样返回（让飞书报真实错误，不静默吞）。"""
        fn = getattr(self.feishu, "field_names", None)
        if fn is None:
            return rec
        try:
            names = fn(self.table_id)
        except FeishuError:
            return rec
        return filter_by_schema(rec, names)

    def _fetch_bill_no(self, rid: str) -> str:
        """取回飞书 AutoNumber 单号（写表后立即可见，但可能有极短的最终一致性延迟）。

        - rid 不存在/无飞书 → 返回 ""（由调用方做诚实兜底，绝不显示「系统生成中」误导）。
        - 首查若为空，短延迟重试一次，提升拿到真实单号的概率。
        """
        if not (self.feishu and rid):
            return ""
        for attempt in range(2):
            try:
                rec = self.feishu.get_record(self.table_id, rid)
                no = rec.get("fields", {}).get("单号", "")
                if no:
                    return no
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.4)  # AutoNumber 最终一致性：首查空则短延迟重试一次
        return ""

    # ---------------------------------------------------------- 确认提交意图识别
    @staticmethod
    def _is_confirm(text: str) -> bool:
        """待确认态下，识别用户「确认提交」意图（明确否定词优先拦截，避免误触发）。"""
        t = text or ""
        if any(w in t for w in ("不提交", "不确认", "不要提交", "不用提交", "暂不", "先不", "别提交")):
            return False
        return any(k in t for k in ("确认提交", "确认", "确定提交", "确定", "提交吧",
                                    "确认吧", "帮我提交", "没问题", "好的提交",
                                    "可以提交", "ok", "OK", "提交"))

    # ---------------------------------------------------------- 票据状态：主要提醒（礼貌）
    @staticmethod
    def _invoice_reminder(has_invoice: str) -> str:
        """票据状态是「主要提醒参数」（区别于城市/日期的软提醒）：礼貌告知票据对审核进度的影响。"""
        if has_invoice == "有票":
            return "🧾 您已带发票，这会加快审核进度，财务小伙伴能更快处理~"
        if has_invoice == "无票":
            return "🧾 暂未带发票，无发票需要更多核验时间，建议您尽快补开发票哦~"
        # 未提：温和引导补充票据状态
        return "🧾 温馨提示：有发票会加快审核进度，无发票需要更多核验时间，建议补充一下票据状态~"

    # ---------------------------------------------------------- 软提示（非必填，仅建议）
    @staticmethod
    def _soft_hints(f: dict) -> list:
        """补充建议：事由 / 差旅城市 补齐可提升通过率、加快审核（非强制，只提醒）。"""
        hints = []
        if not f.get("reason"):
            hints.append("💡 补充具体「事由」可以提高审核通过率哦~")
        # 差旅费（住宿/住酒店已被抽取归一为 差旅费）等外出类 → 提醒补城市
        if f.get("category") == "差旅费" and not f.get("city"):
            hints.append("💡 补充消费「城市」可以加快审核速度~")
        return hints

    # ---------------------------------------------------------- B 填单引导：追问 / 提交
    def _ask_missing(self, f: dict, missing: list) -> str:
        lines = ["您好呀~ 我是小财奴，帮您梳理了一下报销信息 📝"]
        filled = []
        if f["applicant"]:
            filled.append(f"👤 申请人：{f['applicant']}")
        if f["department"]:
            filled.append(f"🏢 部门：{f['department']}")
        if f["category"]:
            filled.append(f"💼 报销类型：{f['category']}")
        if f["amount"]:
            filled.append(f"💰 金额：¥{f['amount']:.0f}")
        if f["has_invoice"] and f["has_invoice"] != "未提":
            filled.append(f"🧾 票据：{f['has_invoice']}")
        if filled:
            lines.append("已记录：" + " · ".join(filled))
        need = []
        if "申请人" in missing:
            need.append("👤 申请人（您怎么称呼呀？）")
        if "部门" in missing:
            need.append("🏢 部门（您属于哪个部门呀？）")
        if "报销类型" in missing:
            need.append("💼 报销类型（差旅/招待/办公用品/交通费？）")
        if "金额" in missing:
            need.append("💰 金额（花了多少呀？）")
        lines.append("✏️ 还需要补齐这几项哦：")
        lines.extend(need)
        # 票据状态：主要提醒（区别于软提醒，礼貌告知对审核进度的影响）
        lines.append("")
        lines.append(self._invoice_reminder(f["has_invoice"] or "未提"))
        # 软提示：即使必填齐全，也顺手提醒补齐 事由/城市（非强制，仅建议）
        hints = self._soft_hints(f)
        if hints:
            lines.append("")
            lines.append("另外，补充以下信息能更快通过审核：")
            lines.extend(hints)
        lines.append("补齐后我会请您核对信息，确认无误再上传飞书~")
        return "\n".join(lines)

    # ---------------------------------------------------------- B 填单引导：确认前回显
    def _ask_confirm(self, f: dict, has_invoice: str) -> str:
        """必填齐全后的「确认前」摘要：回显当前已有参数 + 主要备注经事由 + 票据主要提醒 + 软提醒。"""
        lines = ["✨ 信息已收集齐全，麻烦您核对一下~"]
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
        # 事由备注提示：主要事情/用途都通过「事由」告诉我
        lines.append("📝 备注：报销的主要事情或用途，请通过「事由」补充告诉我，方便财务审核归类~")
        # 票据状态：主要提醒（礼貌，区别于软提醒）
        lines.append(self._invoice_reminder(has_invoice))
        # 软提醒：城市/日期等（非必填，礼貌提醒即可）
        soft = [h for h in self._soft_hints(f) if "事由" not in h]
        if soft:
            lines.append("")
            lines.append("💡 还有这些可补充（非必填，补了更快通过）：")
            lines.extend(soft)
        lines.append("")
        lines.append("✅ 请确认信息，回复「确认提交」即可开始上传飞书~")
        return "\n".join(lines)

    # ---------------------------------------------------------- B 填单引导：无票软确认（C 方案）
    def _ask_no_invoice_confirm(self, f: dict) -> str:
        """无票用户坚持提交时的「软确认」：给二选一，不硬拦截。

        - 回复「有发票了」→ 票据状态更新为有票，回到正常确认摘要；
        - 回复「仍要提交」→ 放行写表，合规结果标记「驳回」并注明用户坚持。
        """
        lines = [
            "🧾 温馨提示：这一单您说的是「无票」哦~",
            "按《报销制度》，没有合规发票的报销，财务是无法正常审核通过的。",
            "",
            "您可以选择：",
            "🧾 回复「有发票了」—— 补上票据状态，我帮您正常提交；",
            "🚀 回复「仍要提交」—— 我按您的决定提交给财务，"
            "并标注「无票提交，财务将驳回」。",
            "",
            "（也可以继续补充或修改其他报销信息~）",
        ]
        return "\n".join(lines)

    # ---------------------------------------------------------- 跨轮草稿合并（B/C 多轮补全用）
    @staticmethod
    def _merge_draft(base: dict, new: dict) -> dict:
        """base=上一轮已抽取字段，new=本轮新抽取；非空值覆盖/补全空字段。

        特别保护：申请人(姓名)一旦已确认真值，后续轮次不被可疑 token 覆盖
        （防止「有电子发票」把正确的「张三」冲掉）。
        """
        if not base:
            return new
        merged = dict(base)
        for k, v in new.items():
            if v in (None, "", 0.0, []):
                continue
            if k == "applicant" and base.get("applicant"):
                continue
            # 票据状态：「未提」是「无信息」哨兵，不应覆盖已提供的 有票/无票
            if k == "has_invoice" and v == "未提" and base.get("has_invoice") in ("有票", "无票"):
                continue
            merged[k] = v
        return merged

    # ---------------------------------------------------------- B 填单引导：按钮口令 → 全参数友好引导
    def _handle_fill_guide(self) -> dict:
        """「填单引导」按钮：用 emoji 图标 + 文字把全部需要的参数讲清楚（禁止 JSON），
        语气友好、措辞有多样性，并留一个空草稿承接用户接下来的逐项补充。"""
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
            "",
            "【重点提醒 · 直接影响审核速度】",
            "🧾 票据状态 —— 有发票会加快审核进度；无发票需要更多核验时间哦",
            "📝 事由 —— 报销的主要事情或用途写在这里，财务小伙伴全靠它归类~",
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
            "draft": {},  # 空草稿：接下来用户说什么都会并入填单流程
        }

    def _handle_fill(self, question: str, base_draft=None, user: dict | None = None,
                     image_b64: str = "", image_mime: str = "image/jpeg",
                     invoice_fields: dict | None = None) -> dict:
        f = self._extract_fields(question)
        if base_draft:
            f = self._merge_draft(base_draft, f)
            # 保留上一轮的「待确认」标记（合并不会覆盖该标记）
            if base_draft.get("await_confirm"):
                f["await_confirm"] = True
        # M8 身份覆写（修订）：姓名一律以账号为准（防替他人填单）；
        # 部门以用户口述为准（说了哪个部门就写哪个），未说才回退账号资料部门
        if user:
            if user.get("name"):
                f["applicant"] = user["name"]
            if not f.get("department") and user.get("department"):
                f["department"] = user["department"]
        # M9 OCR 发票字段并入草稿（发票号码/销售方 → 主表「发票信息」列；金额/日期等
        # 已由前端把 OCR 一句话喂回 _extract_fields 自然合并，无需在此重复）
        if invoice_fields:
            for _k in ("invoice_no", "invoice_seller"):
                _v = str(invoice_fields.get(_k) or "").strip()
                if _v:
                    f[_k] = _v[:64]
        # 票据已补为有票 → 清除无票软确认标记（合规层不再阻断，无需再走坚持流程）
        if f.get("has_invoice") == "有票":
            f.pop("no_invoice_warned", None)
        result = {"intent": "B", "record_id": None, "bill_no": "", "answer": "", "draft": f}

        # 专项流程：不直交，引导走专项，禁止伪装提交
        # （.get 防御：外部直传的草稿可能不含 special 等键，缺键视为无专项）
        if f.get("special"):
            result["draft"] = None  # 终态：清空草稿，避免上下文粘连
            result["answer"] = (
                f"🚦「{f['special']}」不在小财直交范围，需走专项流程提交~ "
                "（团建/通讯/培训/推广/加班餐/私车公用请按对应渠道办理，不要伪装成 4 类报销哦）"
            )
            return result

        # 必填（提交必须具备）：申请人 / 部门 / 报销类型(4类) / 金额
        # 票据状态按填单规则允许「未提」(不强制)；仅「无票」由合规层阻断
        missing = []
        if not f.get("applicant"):
            missing.append("申请人")
        if not f.get("department"):
            missing.append("部门")
        if not f.get("category"):
            missing.append("报销类型")
        if not f.get("amount") or f["amount"] <= 0:
            missing.append("金额")

        if missing:
            result["answer"] = self._ask_missing(f, missing)
            return result

        # 归一票据状态：未提/空 → 未提（合规层提示确认，不阻断）；仅「无票」触发阻断
        has_invoice = f["has_invoice"] if f["has_invoice"] in ("有票", "无票") else "未提"

        # 必填齐全：进入「确认前核对」态（不直接写表）
        if f.get("await_confirm") and self._is_confirm(question):
            # C 方案：无票不硬拦截——先软确认一次（二选一）；用户坚持「仍要提交」则放行并标记
            compliance, risk_summary = self._compliance_check({**f, "has_invoice": has_invoice})
            if compliance == "阻断" and has_invoice == "无票":
                if not f.get("no_invoice_warned"):
                    # 第一次坚持提交：软确认，不写表，记住已提醒
                    result["draft"]["await_confirm"] = True
                    result["draft"]["no_invoice_warned"] = True
                    result["answer"] = self._ask_no_invoice_confirm(f)
                    return result
                # 已软确认过且用户仍坚持 → 放行写表，合规结果标「驳回」（真实表单选选项）
                answer, rid, bill_no = self._do_submit(
                    f, has_invoice,
                    compliance="驳回",
                    risk_summary="无票提交（用户确认坚持），财务将驳回",
                    employee_id=(user or {}).get("employee_id", ""),
                    image_b64=image_b64, image_mime=image_mime,
                    username=(user or {}).get("username", ""),
                )
            else:
                answer, rid, bill_no = self._do_submit(
                    f, has_invoice, employee_id=(user or {}).get("employee_id", ""),
                    image_b64=image_b64, image_mime=image_mime,
                    username=(user or {}).get("username", ""))
            result["answer"] = answer
            result["record_id"] = rid
            result["bill_no"] = bill_no
            # 仅真正提交成功才清空草稿；被阻断则保留待确认态，便于补正后重确认
            if rid:
                result["draft"] = None
            return result

        # 未确认（首次齐全 / 用户继续补充信息）→ 回显核对，等待「确认提交」
        result["draft"]["await_confirm"] = True
        result["answer"] = self._ask_confirm(f, has_invoice)
        return result

    # ---------------------------------------------------------- B 填单引导：真正写表（确认后调用）
    def _do_submit(self, f: dict, has_invoice: str,
                   compliance: str | None = None, risk_summary: str | None = None,
                   employee_id: str = "", image_b64: str = "", image_mime: str = "image/jpeg",
                   username: str = ""):
        """合规预审 + 写库 + 成功回显。返回 (answer, record_id, bill_no)。

        compliance/risk_summary 可由调用方覆盖（C 方案：无票坚持提交时由外部传入
        「驳回」标记，绕过内部硬阻断）；缺省 None 时按《报销制度》内部计算。
        employee_id：M8 登录身份自动带出，写入「工号」（表无此列时由 schema 过滤兜底）。
        image_b64/image_mime：M9 可选发票图片，写库成功后上传为「发票附件」列附件；
        上传失败仅提示、不阻断提交（单据已生成，附件可补传）。
        username：M8 登录名，仅用于回显溯源（写入申请人以登录身份为准）。
        """
        if compliance is None:
            compliance, risk_summary = self._compliance_check({**f, "has_invoice": has_invoice})
        if compliance == "阻断":
            return (f"🧾 {risk_summary}，补齐合规发票后我马上帮您提交~", None, "")

        rec = build_reimbursement_record(
            applicant=f["applicant"], department=f["department"], rtype=f["category"],
            amount=f["amount"], reason=f.get("reason", ""), has_invoice=has_invoice,
            city=f.get("city", ""), date_str=f.get("date", ""), compliance=compliance, risk_summary=risk_summary,
            status="待审核", employee_id=employee_id,
        )
        # M9 发票信息（发票号码/销售方）：有值才写，旧表无此列由 _fit_schema 剔除
        invoice_info = "；".join(
            x for x in (str(f.get("invoice_no") or "").strip(),
                        str(f.get("invoice_seller") or "").strip()) if x
        )
        if invoice_info:
            rec["发票信息"] = invoice_info
        rec = self._fit_schema(rec)  # M8/P5：旧表无 工号 列时自动剔除
        # 写前枚举强校验：任何非企业标准枚举值一律拒写，保护飞书数据不被脏值污染
        enum_errors = validate_enum_fields(rec)
        if enum_errors:
            bad = "；".join(f"{fld}={val!r}" for fld, val in enum_errors)
            return (f"⚠️ 以下字段值不在企业标准枚举内，已拒绝写入以保护数据：{bad}。"
                    f"请按规范填写后重试~", None, "")
        if self.feishu and self.table_id:
            try:
                ids = self.feishu.create_records(self.table_id, [rec])
                rid = ids[0] if ids else None
                bill_no = self._fetch_bill_no(rid) if rid else ""
                # M9 发票图片存档：上传附件并关联到刚生成的单据（失败不阻断，仅提示）
                attach_note = ""
                if image_b64 and rid:
                    attach_note = self._attach_invoice(rid, image_b64, image_mime)
                # 单号展示：优先真实单号；若已写入成功但 AutoNumber 暂未可取，诚实兜底（绝不显示「系统生成中」）
                if bill_no:
                    bill_line = f"📋 单号：{bill_no}"
                else:
                    bill_line = "📋 单号：已成功提交，稍后可在「进度查询」中输入姓名查看"
                # 已提交参数回显：让用户确认填写字段确实生效
                parts = [f"👤 {f['applicant']}"]
                if f.get("department"):
                    parts.append(f"🏢 {f['department']}")
                if f.get("category"):
                    parts.append(f"💼 {f['category']}")
                parts.append(f"💰 ¥{f['amount']:.0f}")
                if f.get("city"):
                    parts.append(f"📍 {f['city']}")
                if f.get("reason"):
                    parts.append(f"📝 {f['reason']}")
                answer_lines = [
                    "✅ 报销单已成功提交到飞书表格啦！",
                    "",
                    bill_line,
                    " · ".join(parts),
                    "📊 当前状态：待审核",
                ]
                if compliance != "通过":
                    answer_lines.append(f"📌 合规结果：{compliance} / {risk_summary}")
                if attach_note:
                    answer_lines.append(attach_note)
                # 软提示：即便已提交，也提醒可补充 事由/城市 以便更快通过/审核
                hints = self._soft_hints(f)
                if hints:
                    answer_lines.append("")
                    answer_lines.extend(hints)
                answer_lines.append(
                    "财务小伙伴会尽快审核，结果出来会有通知~ 您也可以随时问我"
                    "「我的报销进度怎么样了」来查看状态哦 💡"
                )
                return ("\n".join(answer_lines), rid, bill_no)
            except FeishuError as e:
                return (f"😥 抱歉，提交没能成功（{e}），需要我帮您重新提交吗？", None, "")
        return (
            f"📋 填单引导：信息齐全（申请人 {f['applicant']}，{f['department']}，"
            f"{f['category']} ¥{f['amount']:.0f}），飞书未接入，未实际写入。",
            None,
            "",
        )

    # ---------------------------------------------------------- M9 发票附件存档
    _MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

    def _attach_invoice(self, record_id: str, image_b64: str, image_mime: str = "image/jpeg") -> str:
        """发票图片上传飞书并关联单据「发票附件」列；返回用户提示语（永不抛异常）。

        降级策略：表无「发票附件」列 / 飞书权限不足 / 图片数据异常 → 仅提示，
        绝不影响已成功的单据提交（主流程优先）。
        """
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
        except Exception as e:  # 兜底：附件链路任何异常都不影响主流程
            return f"📎 发票图片存档未成功（{e}）；单据本身已提交成功，可稍后补传"

    # ---------------------------------------------------------- C 合规预审：规则 + 回写
    def _handle_compliance(self, question: str, base_draft=None, user: dict | None = None) -> dict:
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

        # 信息齐全且可提交 → 回写（状态 待审核）；信息不全 → 仅建议不写表
        ready = bool(f["applicant"] and f["category"] and f["amount"] > 0 and f["has_invoice"] == "有票")
        if ready and self.feishu and self.table_id:
            try:
                rec = build_reimbursement_record(
                    applicant=f["applicant"], department=f["department"], rtype=f["category"],
                    amount=f["amount"], reason=f["reason"], has_invoice=f["has_invoice"],
                    city=f["city"], date_str=f["date"], compliance=compliance, risk_summary=risk_summary,
                    status="待审核",
                )
                rec = self._fit_schema(rec)  # M8/P5：旧表无 工号 列时自动剔除
                # 写前枚举强校验：非标准枚举值拒写
                enum_errors = validate_enum_fields(rec)
                if enum_errors:
                    bad = "；".join(f"{fld}={val!r}" for fld, val in enum_errors)
                    result["answer"] = f"⚠️ 预审通过，但字段值不在企业标准枚举内，已拒绝回写飞书：{bad}。"
                    return result
                ids = self.feishu.create_records(self.table_id, [rec])
                result["record_id"] = ids[0] if ids else None
            except FeishuError as e:
                result["answer"] = f"⚠️ 预审完成（{compliance}），但飞书回写失败：{e}。"

        if not result["answer"]:
            if compliance == "通过":
                result["answer"] = "✅ 合规预审：通过，无风险点。"
            else:
                result["answer"] = f"✅ 合规预审：存在风险。风险点：{risk_summary}"
        return result

    # ---------------------------------------------------------- D 进度查询：查飞书（按申请人/单号）
    def _d_extract_keyword(self, question: str) -> str:
        """D 分支专用查询词抽取：BX 单号 → 「查询XX的进度」里的姓名 → 纯姓名 → 通用兜底。

        修复：「查询小新的进度 / 查询张三的进度」这类带姓名的完整问句，
        旧逻辑抽不出姓名（还会把「进度」错当姓名），导致机器人反问是谁。
        """
        q = question or ""
        bx = re.search(r"(BX-?\d[\d-]*)", q)
        if bx:
            return bx.group(1)

        def _valid(name: str) -> str:
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

        # 「查询XX的进度 / 查一下XX的报销 / XX的结果」等：姓名 + 查询名词组合
        for pat in (
            r"查(?:询|一下)?\s*([一-龥]{2,4}?)\s*的?\s*(?:进度|报销|单号|记录)",
            r"([一-龥]{2,4}?)\s*的\s*(?:进度|报销|记录|单号|结果)",
        ):
            m = re.search(pat, q)
            if m:
                name = _valid(m.group(1))
                if name:
                    return name

        # 纯姓名（可带「的/吧/呀」等领属/语气后缀，如「小新的」「张三吧」）
        bare = _valid(re.sub(r"[的了吧呀哦呢]+$", "", q.strip()))
        if bare:
            return bare

        # 兜底：通用抽取的 applicant（排除伪姓名）/ 字母数字（工号类）
        f = self._extract_fields(q)
        if f["applicant"] and f["applicant"] not in _D_NAME_BLOCKLIST:
            return f["applicant"]
        am = re.search(r"([A-Za-z0-9]{2,})", q)
        return am.group(1) if am else ""

    @staticmethod
    def _record_hit(keyword: str, fields: dict, question: str) -> bool:
        """D 记录命中判定（精确优先，防误伤）。

        修复：旧逻辑「任意字段值是问句子串就算命中」过松——查 BX-000098 时，
        BX-000097 的金额 8 使 "8" in 问句 成立，导致跨单号误命中。
        现改为：① 单号归一（忽略连字符）后互相包含；② 姓名与申请人双向包含；
        ③ 兜底仅接受「长度≥2 且非纯数字」的字段值出现在问句中。
        """
        if not keyword:
            return False
        no = str(fields.get("单号", "") or "").strip()
        if no:
            k = keyword.upper().replace("-", "")
            n = no.upper().replace("-", "")
            if k in n or n in k:
                return True
        applicant = str(fields.get("申请人", "") or "").strip()
        if applicant and (keyword in applicant or applicant in keyword):
            return True
        for v in fields.values():
            s = str(v).strip()
            if len(s) >= 2 and not s.isdigit() and s in question:
                return True
        return False

    # ---------------------------------------------------------- D 展示格式化
    @staticmethod
    def _fmt_ts(v) -> str:
        """飞书日期字段（毫秒时间戳）→ 'YYYY/MM/DD HH:MM'；非时间值原样返回字符串。"""
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

    @staticmethod
    def _fmt_attach(v) -> str:
        """附件列（[{name,file_token},...]）→ 附件名顿号串联；空返回空串。"""
        if isinstance(v, list) and v:
            names = []
            for it in v:
                if isinstance(it, dict):
                    names.append(str(it.get("name") or "附件"))
                else:
                    names.append(str(it))
            return "、".join(n for n in names if n)
        if isinstance(v, str) and v.strip():
            return v.strip()
        return ""

    def _handle_progress(self, question: str, user: dict | None = None) -> dict:
        keyword = self._d_extract_keyword(question)
        hit_question = question
        # M8 角色权限：员工只能查自己的进度（忽略问句里的任何姓名/单号，防越权查询）。
        # 注意：兜底命中也要把问句换成本人关键词——否则问句里的他人姓名仍会经
        # 「字段值出现在问句中」的兜底路径命中他人记录（实测泄漏点）。
        if user and user.get("role") == ROLE_EMPLOYEE:
            keyword = user.get("name") or user.get("username") or keyword
            hit_question = keyword
        result = {"intent": "D", "records": [], "answer": ""}

        if not keyword:
            result["answer"] = "⏳ 请提供姓名或报销单号（如 BX000013），我帮您查进度~"
            return result

        if self.feishu and self.table_id:
            try:
                recs = self.feishu.list_records(self.table_id, page_size=50)
                matched = [
                    public_view(r) for r in recs
                    if self._record_hit(keyword, r.get("fields", {}), hit_question)
                ]
                result["records"] = matched
                if matched:
                    lines = ["好的，帮您查到啦~"]
                    for i, m in enumerate(matched, 1):
                        st = str(m.get("状态") or "").strip()
                        emoji = {"已通过": "✅", "待审核": "⏳", "审核中": "🔍", "已驳回": "❌"}.get(st, "📊")
                        amount = m.get("金额")
                        amount_s = f"¥{amount}" if amount not in (None, "") else "无"
                        # 全字段展示：审核人/审批意见/审批时间/发票信息/附件等一个不落；空值一律「无」
                        lines += [
                            f"{i}. 📋 单号 {m.get('单号') or '无'}",
                            f"   👤 申请人：{m.get('申请人') or '无'} · 部门：{m.get('部门') or '无'} · 工号：{m.get('工号') or '无'}",
                            f"   💼 报销类型：{m.get('报销类型') or '无'} · 金额：{amount_s}",
                            f"   📝 事由：{m.get('事由') or '无'}",
                            f"   🏙️ 城市：{m.get('城市') or '无'} · 发票日期：{self._fmt_ts(m.get('发票日期')) or '无'}",
                            f"   🕐 提交时间：{self._fmt_ts(m.get('记录提交时间')) or '无'}",
                            f"   🧾 票据状态：{m.get('票据状态') or '无'} · 发票信息：{m.get('发票信息') or '无'} · 发票附件：{self._fmt_attach(m.get('发票附件')) or '无'}",
                            f"   📊 状态：{emoji} {st or '无'}",
                            f"   📌 合规结果：{m.get('合规结果') or '无'} · 风险摘要：{m.get('风险摘要') or '无'}",
                            f"   👮‍♂️ 审核人：{m.get('审核人') or '无'} · 审批意见：{m.get('审批意见') or '无'} · 审批时间：{self._fmt_ts(m.get('审批时间')) or '无'}",
                        ]
                    lines.append("💡 有驳回单建议联系审核人，待审核请耐心等待审批~")
                    result["answer"] = "\n".join(lines)
                else:
                    result["answer"] = "⏳ 没有找到相关的报销记录呢~ 请确认姓名或单号是否正确？"
            except FeishuError as e:
                result["answer"] = f"⏳ 飞书查询失败（{e}），建议稍后重试或联系财务同学~"
        else:
            result["answer"] = "⏳ 进度查询需要接入飞书（M3），当前未接入。"
        return result

    # ---------------------------------------------------------- 便捷构造
    @classmethod
    def from_config(cls, compressor=None, top_k: int = 3, default_category="报销制度", with_feishu: bool = True):
        """从 .env + knowledge/ 一键装配（生产用）。

        注意：reranker 若启用，需在 Retriever.from_directory(reranker=...) 处注入，
        本方法保持简洁，默认不启用 rerank（比赛现场可加）。
        Feishu 仅在配置了 FEISHU_APP_ID 时自动接入（缺省则 B/C/D 降级，不报错）。
        """
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
