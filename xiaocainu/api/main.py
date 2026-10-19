"""
M5 · API 网关（FastAPI）
===================
把 XiaoCaiAgent（M1-M4）包成 HTTP 服务，供 M6 前端 / 外部调用。

端点
----
- GET  /                 前端单页（frontend/index.html）
- GET  /finance           财务审核页（frontend/finance.html）
- GET  /health            健康检查（免认证）
- POST /login            {username,password}             登录（免认证，发 token）
- GET  /me               Authorization: Bearer <token>   当前登录用户信息
- POST /logout           Authorization: Bearer <token>   登出（jti 拉黑）
- POST /chat             {session_id?, text, image_b64?} 主对话入口（多轮记忆 + B 跨轮补全 + M9 发票图）
- POST /ocr              {session_id?, image_b64, filename?} M9 发票 OCR 识别（DeepSeek 视觉）
- POST /reimburse        {department,rtype,amount,...}   结构化填单直提（直连飞书多维表，申请人/工号以登录身份为准）
- POST /progress         {applicant?|bill_no?}           进度查询
- POST /finance/{meta,list,detail,review,update,delete}  财务审核页六端点：meta/list/detail 读直连飞书（只读），
                                                              review/update/delete 经主服务器写入口（角色限财务/管理员）

M8 认证墙：除 /health /（静态页）/login 外一律需 Bearer token（fail-closed——
认证未配置时受保护端点直接 401，不做匿名放行）。

设计
----
- FastAPI + pydantic：请求模型即「JSON 契约」，字段长度/类型约束构成**入口防注入第一道闸**
  （超长 payload、非法类型、越界数值在边界被拒）。
- M7 语义层守卫（security 包，纯 stdlib）：sanitize_text（净化）→ check_injection（prompt
  注入拦截）→ RateLimiter（按会话限流），在 dispatch 真正调用 Agent 之前构筑**第二道闸**，
  形成「契约层 → 语义层」双闸纵深防御。
- 安全：Agnes/飞书凭证只在服务端 from_env 读取，前端只连本网关，key 不落地浏览器；
  对话仅内存级 SessionStore（不落库、带 TTL），上下文不持久化（数据最小化）。
- dispatch() 与框架解耦：便于离线单测（直接传 mock agent/store，无需起 HTTP）。

运行
----
    cd xiaocainu
    pip install fastapi uvicorn httpx
    python -m api.main            # uvicorn 启动，默认 0.0.0.0:8000
    python -m api.main 127.0.0.1 9000
"""
from __future__ import annotations

import os
import sys
import base64
import time
from typing import Any, Dict, Tuple

from pydantic import BaseModel, Field, field_validator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (
    XiaoCaiAgent,
    LLMAdapter,
    MockProvider,
    FeishuBitableClient,
    Retriever,
)
from models.config import knowledge_dir, get_ocr_config
from models.feishu import (
    build_reimbursement_record, validate_enum_fields, filter_by_schema,
    FeishuError, ALLOWED_TYPES, DEPT_WHITELIST, _parse_date_to_ts,
)
from models.ocr import (
    InvoiceOCREngine, MockOcrEngine, OCRError, build_invoice_preview,
)
from models.users import ROLE_EMPLOYEE, ROLE_FINANCE, ROLE_ADMIN
from .session import SessionStore
from .auth_service import AuthService, build_auth
from . import backend_client
from security.guard import sanitize_text, check_injection, RateLimiter

from fastapi import FastAPI, HTTPException, Header, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response


# ---------------------------------------------------- 请求模型（JSON 契约 + 入口防注入约束）
class ChatRequest(BaseModel):
    session_id: str = Field(default="default", max_length=64)
    text: str = Field(..., min_length=1, max_length=2000)  # 长度闸：防超长 payload / 注入体
    # M9 可选发票图（base64 裸串或 data URL；8MB 原始 ≈ 10.7M base64 字符，上限留余量）
    image_b64: str = Field(default="", max_length=12_000_000)
    image_mime: str = Field(default="image/jpeg", max_length=32)
    # M9 可选 OCR 发票字段（/ocr 识别后由前端随对话捎回，并入 B 草稿）
    invoice_no: str = Field(default="", max_length=64)
    invoice_seller: str = Field(default="", max_length=64)


class OcrRequest(BaseModel):
    """M9 发票 OCR 请求：图片以 base64 JSON 传输（复用认证墙/限流，不引入 multipart）。"""
    session_id: str = Field(default="default", max_length=64)
    image_b64: str = Field(..., min_length=8, max_length=12_000_000)
    filename: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=64)


class ReimburseRequest(BaseModel):
    applicant: str = Field(default="", max_length=32)
    department: str = Field(default="", max_length=32)
    rtype: str = Field(default="", max_length=16)
    amount: float = Field(default=0, ge=0, le=10_000_000)
    reason: str = Field(default="", max_length=200)
    has_invoice: str = Field(default="", max_length=8)
    city: str = Field(default="", max_length=16)
    date_str: str = Field(default="", max_length=24)
    compliance: str = Field(default="通过", max_length=16)
    risk_summary: str = Field(default="", max_length=200)
    status: str = Field(default="待审核", max_length=16)
    # M9 发票信息（号码/销售方），随单透传主服务器（此前调用处漏传，已补）
    invoice_info: str = Field(default="", max_length=128)


class ProgressRequest(BaseModel):
    applicant: str = Field(default="", max_length=32)
    bill_no: str = Field(default="", max_length=32)


# ---------------------------------------------------- 财务审核页请求模型（M10）
class FinanceListRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


class FinanceDetailRequest(BaseModel):
    record_id: str = Field(..., min_length=1, max_length=64)


class FinanceReviewRequest(BaseModel):
    record_id: str = Field(..., min_length=1, max_length=64)
    status: str = Field(..., min_length=2, max_length=16)
    opinion: str = Field(default="", max_length=300)


class FinanceUpdateRequest(BaseModel):
    record_id: str = Field(..., min_length=1, max_length=64)
    fields: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("fields")
    @classmethod
    def _fields_not_empty(cls, v):
        """入口防注入闸：空字典拒收；字段名限长 32、字符串值限长 200、仅允许标量。"""
        if not v:
            raise ValueError("fields 不能为空")
        for k, val in v.items():
            if len(k) > 32:
                raise ValueError(f"字段名过长：{k[:16]}…")
            if isinstance(val, str) and len(val) > 200:
                raise ValueError(f"字段值过长：{k}")
            if not isinstance(val, (str, int, float)):
                raise ValueError(f"字段值类型不支持：{k}")
        return v


class FinanceDeleteRequest(BaseModel):
    record_id: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(..., min_length=2, max_length=200)


# ---------------------------------------------------- 装配（与 run_live 同策略：openai 缺失降级 mock）
def build_agent() -> XiaoCaiAgent:
    try:
        llm = LLMAdapter.from_env()
    except ImportError:
        print("[M5] 未安装 openai，A 分支将用模拟回复；B/C/D 飞书功能正常。")
        llm = LLMAdapter(MockProvider())
    retriever = Retriever.from_directory(knowledge_dir())
    feishu = None
    table_id = ""
    if os.getenv("FEISHU_APP_ID"):
        feishu = FeishuBitableClient.from_env()
        table_id = os.getenv("FEISHU_TABLE_ID", "")
        print(f"[M5] 飞书已接入：table_id={table_id or '(未设置)'}")
    else:
        print("[M5] 未检测到 FEISHU_APP_ID，B/C/D 走降级（不真发）。")
    return XiaoCaiAgent(llm, retriever, feishu=feishu, table_id=table_id)


# 进程级单例（仅启动时装配一次）
AGENT = build_agent()
STORE = SessionStore()
# M7 限流单例：按会话滑动窗口（与进程同生命周期，内存级不落库）
CHAT_LIMITER = RateLimiter(limit=30, window=60)
# M9 OCR 限流：每会话独立窗口（视觉调用更贵，窗口更紧）
OCR_LIMITER = RateLimiter(limit=10, window=60)
# M8 认证单例：未配置用户表时为 None（受保护端点一律 401，fail-closed）
AUTH = build_auth()


# ---------------------------------------------------- M9 发票 OCR 装配与图片入口校验
_OCR_CFG = get_ocr_config()
OCR_MAX_BYTES = int(_OCR_CFG["max_image_mb"]) * 1024 * 1024
_OCR_MAX_MB = int(_OCR_CFG["max_image_mb"])
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def build_ocr_engine():
    """按 .env 装配 DeepSeek 视觉引擎；未配置 key → None（/ocr 诚实返回 503，不静默 mock）。

    占位符防护：形如「你的密钥_勿提交」的模板值视为未配置——绝不拿占位 key 发真实调用。
    """
    key = _OCR_CFG["api_key"]
    if not key or "你的密钥" in key or len(key) < 20:
        return None
    try:
        return InvoiceOCREngine(
            base_url=_OCR_CFG["base_url"], api_key=key,
            model=_OCR_CFG["model"], detail=_OCR_CFG["detail"],
        )
    except ImportError:
        print("[M9] 未安装 openai，OCR 引擎不可用（/ocr 将返回 503）。")
        return None


OCR_ENGINE = build_ocr_engine()

# 图片魔数白名单（按真实内容判定，不信任扩展名/声明 MIME）
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # 需追加校验 RIFF....WEBP
)


def _strip_data_url(b64: str) -> str:
    """'data:image/jpeg;base64,xxxx' → 'xxxx'；裸 base64 原样返回。"""
    if b64.startswith("data:"):
        return b64.partition(",")[2].strip()
    return b64.strip()


def _decode_image(image_b64: str, max_bytes: int) -> tuple[bytes, str] | tuple[None, None]:
    """解码并校验图片：返回 (bytes, 真实 MIME)；非法返回 (None, None)。

    防线：base64 合法性 → 字节上限 → 魔数白名单（伪装图片在此被拒）。
    """
    try:
        raw = base64.b64decode(_strip_data_url(image_b64), validate=True)
    except Exception:
        return None, None
    if not raw or len(raw) > max_bytes:
        return None, None
    for magic, mime in _IMAGE_MAGIC:
        if raw.startswith(magic):
            if mime == "image/webp" and raw[8:12] != b"WEBP":
                continue
            return raw, mime
    return None, None
app = FastAPI(title="小财奴 API 网关", version="1.3.0")

# 前端单页绝对路径（frontend/index.html），用于 GET / 直接托管
_FRONTEND_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "index.html"
)
# 财务审核页绝对路径（frontend/finance.html），用于 GET /finance 直接托管
_FINANCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "finance.html"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ---------------------------------------------------- 前端单页托管（M6）
@app.get("/")
def index():
    """托管 frontend/index.html；文件缺失时降级提示，不影响 API。"""
    if os.path.exists(_FRONTEND_PATH):
        return FileResponse(_FRONTEND_PATH, media_type="text/html")
    return HTMLResponse("<h2>小财奴 API 网关</h2><p>前端文件 frontend/index.html 未找到。</p>")


# ---------------------------------------------------- 财务审核页托管（M10）
@app.get("/finance")
def finance_page():
    """托管 frontend/finance.html；文件缺失时降级提示，不影响 API。

    页面免认证（同 /）：数据端点 POST /finance/* 全在认证墙后，
    角色不符由前端按 /me 结果进入拒绝视图（员工误入有引导回聊天页）。
    """
    if os.path.exists(_FINANCE_PATH):
        return FileResponse(_FINANCE_PATH, media_type="text/html")
    return HTMLResponse("<h2>小财奴 · 财务审核页</h2><p>前端文件 frontend/finance.html 未找到。</p>")


# ---------------------------------------------------- 路由分发（与框架解耦，便于测试）
# 免认证端点（M8 认证墙白名单；GET / 静态页不走 dispatch）
_PUBLIC_ENDPOINTS = {("GET", "/health"), ("POST", "/login")}
_AUTH_USER_KEYS = ("username", "name", "role", "employee_id")


def _bearer(token: str) -> str:
    """'Bearer xxx' → 'xxx'；其他原样返回。"""
    t = token or ""
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


# ---------------------------------------------------- 财务审核页助手（M10：直连飞书多维表格）
_FINANCE_ROLES = (ROLE_FINANCE, ROLE_ADMIN)

# 飞书真实列名 -> 前端 snake_case（record_id 是操作句柄前端不渲染；
# 单号为 AutoNumber 只读、发票附件为附件结构，二者均不过网关）
_FINANCE_ROW_KEYS = {
    "bill_no": "单号", "applicant": "申请人", "employee_id": "工号",
    "department": "部门", "rtype": "报销类型", "amount": "金额",
    "reason": "事由", "has_invoice": "票据状态", "city": "城市",
    "invoice_date": "发票日期", "submitted_at": "记录提交时间",
    "compliance": "合规结果", "risk_summary": "风险摘要", "status": "状态",
    "reviewer": "审核人", "review_opinion": "审批意见",
    "reviewed_at": "审批时间", "invoice_info": "发票信息",
    # 附件列是飞书附件结构 [{file_token,name,size,tmp_url,...}]；
    # tmp_url 是用户身份临时链接（应用身份访问报 99991661）且会过期，不下发，
    # 前端只用 file_token 走 /finance/attachment 中转下载。
    "invoice_files": "发票附件",
}
# 附件投影白名单：file_token/name/size 供前端展示；
# url 是飞书签发的临时下载票据（含 extra 鉴权），**只供服务端中转下载**，
# 前端 JS 不会直接用（它只传 file_token 回网关），但需随详情下发。
_FINANCE_FILE_KEYS = ("file_token", "name", "size", "type", "url")

# 财务可改字段白名单（中文列名）：服务端盖章字段与 AutoNumber 一律不可改
_FINANCE_EDITABLE = (
    "部门", "报销类型", "金额", "事由", "票据状态", "城市",
    "发票日期", "合规结果", "风险摘要",
)

# 审核动作允许写入的状态（走主服务器状态机后放宽：审核中/已通过/已驳回；待审核不可由审核动作写入）
_FINANCE_REVIEW_STATUS = ("审核中", "已通过", "已驳回")

# 审核前允许的状态（终态不可再审）
_FINANCE_REVIEWABLE_FROM = ("待审核", "审核中")


def _format_amount(amt: float) -> str:
    """金额归一成干净字符串：整数不带小数，小数去尾随零（避免 1800.0 / 1e3 等）。

    主服务器 /api/invoice/update 的 fields 是 Dictionary<string,string>，金额字段要字符串，
    且自行做类型转换——这里只负责"干净字符串"形态。
    """
    if amt == int(amt):
        return str(int(amt))
    return ("%.2f" % amt).rstrip("0").rstrip(".")


def _finance_row(row: dict) -> dict:
    """飞书记录 → 网关业务投影：仅 18 个业务字段 + record_id 操作句柄。

    兼容两种入参形态：
      1) {"record_id": ..., "fields": {中文字段}}（list_records 返回）
      2) {中文字段}（已取出的 fields 本体）
    未在白名单内的键（raw 等内部结构）一律剔除，绝不过网关。
    """
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else row
    record_id = row.get("record_id") or ""
    out = {"record_id": record_id}
    for k, src in _FINANCE_ROW_KEYS.items():
        v = fields.get(src)
        if k == "invoice_files":
            # 附件结构只留白名单键；tmp_url/url 为用户身份临时链接，不下发
            out[k] = ([{kk: it.get(kk) for kk in _FINANCE_FILE_KEYS if it.get(kk)}
                       for it in v if isinstance(it, dict)] if isinstance(v, list) else [])
            continue
        out[k] = "" if v is None else v
    return out


def _finance_feishu_call(fn):
    """财务页直连飞书：统一错误口径。

    fn 可返回裸 payload（成功）或 (code, payload) 元组（提前判否，如 404/400）；
    飞书异常一律上抛成人类文案，绝不静默吞错。
    """
    try:
        out = fn()
    except FeishuError as e:
        return 502, {"error": f"飞书多维表格操作失败：{e}"}
    except Exception as e:
        return 500, {"error": f"财务操作异常：{type(e).__name__}: {e}"}
    if isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], int):
        return out[0], out[1]
    return 200, out


def _finance_feishu(agent):
    """取飞书客户端与主表 id；未接入飞书时返回 None（调用方据此 fail-closed）。"""
    feishu = getattr(agent, "feishu", None)
    table_id = getattr(agent, "table_id", "") or ""
    if not feishu or not table_id:
        return None, ""
    return feishu, table_id


def _finance_enum_from_schema(feishu, table_id: str, key: str, fallback: list) -> list:
    """现场探测表内单选字段的真实选项；探测失败回落内置常量（页面不白屏）。"""
    try:
        for f in feishu.list_fields(table_id):
            if f.get("field_name") == key:
                opts = [o.get("name") for o in ((f.get("property") or {}).get("options") or [])]
                got = [o for o in opts if o]
                if got:
                    return got
    except Exception as e:
        print(f"[finance] 枚举探测失败 {key}，回落内置常量：{type(e).__name__}")
    return list(fallback)


def _finance_meta(agent, auth_ctx: dict) -> dict:
    """meta 投影：能力位本地判定 + 枚举取自飞书表真实选项（供前端动态渲染）。"""
    feishu, table_id = _finance_feishu(agent)
    role = auth_ctx.get("role")
    can_edit = role in _FINANCE_ROLES

    departments, types, states, cities, compliance, statuses = [], [], [], [], [], []
    if feishu:
        departments = _finance_enum_from_schema(feishu, table_id, "部门", list(DEPT_WHITELIST))
        types = _finance_enum_from_schema(feishu, table_id, "报销类型", sorted(ALLOWED_TYPES))
        states = _finance_enum_from_schema(feishu, table_id, "票据状态", ["有票", "无票", "未提"])
        cities = _finance_enum_from_schema(feishu, table_id, "城市", [])
        compliance = _finance_enum_from_schema(feishu, table_id, "合规结果", ["通过", "风险", "驳回"])
        statuses = _finance_enum_from_schema(feishu, table_id, "状态",
                                            ["待审核", "审核中", "已通过", "已驳回"])

    return {
        "can_see_all": can_edit,
        "can_review": can_edit,
        "can_update": can_edit,
        "can_delete": can_edit,   # 财务亦可删（用户 2026-10-19 拍板）
        "can_rollback": False,    # 回溯由安全审计侧按动作日志实现，本页不做
        "departments": departments,
        "types": types,
        "invoice_states": states,
        "compliance_results": compliance,
        "statuses": statuses,
        "cities": cities,
        "finance_editable_fields": list(_FINANCE_EDITABLE),
        "review_statuses": list(_FINANCE_REVIEW_STATUS),
        "rules": [
            "审核：待审核/审核中 可改为 已通过 或 已驳回，终态不可再审",
            "修改：仅可改部门/报销类型/金额/事由/票据状态/城市/发票日期/合规结果/风险摘要",
            "删除：物理删除不可恢复，操作前请核对单号与申请人",
        ],
        "table_url": os.getenv("FEISHU_TABLE_URL", ""),
    }


def _finance_role_gate(auth_ctx: dict):
    """财务页角色门：仅财务/管理员。返回 (code, payload) 或 None。"""
    if auth_ctx.get("role") not in _FINANCE_ROLES:
        return 403, {"error": "只有财务或管理员可以使用财务审核功能"}
    return None


def dispatch(method: str, path: str, body: Dict[str, Any], agent: XiaoCaiAgent,
             store: SessionStore, auth: AuthService | None = None,
             token: str = "", client_ip: str = "",
             ocr_engine=None) -> Tuple[int, Dict[str, Any]]:
    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}

    # ---- M8 认证墙（fail-closed）：白名单外一律要 token；auth 未配置也 401，不留匿名旁路 ----
    auth_ctx = None
    if (method, path) not in _PUBLIC_ENDPOINTS:
        if auth is None:
            return 401, {"error": "auth required (server auth not configured)"}
        auth_ctx = auth.verify(_bearer(token))
        if auth_ctx is None:
            return 401, {"error": "未登录或登录已过期，请重新登录"}

    if method == "POST" and path == "/login":
        if auth is None:
            return 503, {"error": "auth not configured (set FEISHU_USER_TABLE_ID in .env)"}
        username = sanitize_text(body.get("username") or "", max_length=32)
        password = body.get("password") or ""  # 密码不做 sanitize（可能含特殊字符），仅限长由 pydantic 保证
        if not username or not password or len(password) > 64:
            return 400, {"error": "username and password required"}
        return auth.login(username, password, client_ip=client_ip)

    if method == "GET" and path == "/me":
        return 200, {"user": {k: auth_ctx.get(k, "") for k in _AUTH_USER_KEYS}}

    if method == "POST" and path == "/logout":
        auth.logout(_bearer(token))
        return 200, {"ok": True}

    if method == "POST" and path == "/chat":
        # 角色门：聊天填单是员工职能；财务/管理员走 /finance 审核台，
        # 不给自己填单（避免自审自批）。前端也会拦一道，这里是服务端终拦。
        if auth_ctx.get("role") != ROLE_EMPLOYEE:
            return 403, {"error": "财务/管理员请使用财务审核台（/finance），聊天填单仅限员工角色"}
        sid = (body.get("session_id") or "default")[:64]
        # ---- M8 会话绑定：sid 首次出现即绑定用户，同 sid 换用户 → 403（防串用/越权读草稿）----
        ok, err = auth.bind_sid(sid, auth_ctx.get("username", ""))
        if not ok:
            return 403, {"error": err}
        # ---- M7 边界防御（语义层第二道闸）----
        # 1) 净化：剥离控制字符/空字节、超长截断（防御纵深，与 pydantic 长度闸互补）
        text = sanitize_text(body.get("text") or "", max_length=2000)
        if not text:
            return 400, {"error": "text required"}
        # 2) 注入拦截：命中 prompt-injection 签名直接拒绝（不进入模型）
        is_inj, matched = check_injection(text)
        if is_inj:
            return 400, {"error": "blocked: potential prompt injection", "matched": matched}
        # 3) 限流：单会话滑动窗口，防刷/资源耗尽
        if not CHAT_LIMITER.allow(sid):
            return 429, {"error": "rate limit exceeded, please try later"}
        # ---- M9 可选发票图与 OCR 字段（有图时先过图片入口防线）----
        image_b64 = str(body.get("image_b64") or "")
        image_mime = str(body.get("image_mime") or "image/jpeg")
        if image_b64:
            raw, real_mime = _decode_image(image_b64, OCR_MAX_BYTES)
            if raw is None:
                return 400, {"error": "invalid image (bad base64 / too large / unsupported type)"}
            image_mime = real_mime
            image_b64 = base64.b64encode(raw).decode()  # 归一为无前缀裸 base64
        invoice_fields = None
        if body.get("invoice_no") or body.get("invoice_seller"):
            invoice_fields = {"invoice_no": str(body.get("invoice_no") or "")[:64],
                              "invoice_seller": str(body.get("invoice_seller") or "")[:64]}
        # ---- 业务路由 ----
        history = store.get_messages(sid)
        base_draft = store.get_draft(sid)
        # M8：把登录身份传给 agent（员工 C 预审拒绝 / B 自动带身份 / D 强制查自己）
        user_ctx = {k: auth_ctx.get(k, "") for k in _AUTH_USER_KEYS} if auth_ctx else None
        res = agent.route(text, history=history, base_draft=base_draft, user=user_ctx,
                          image_b64=image_b64, image_mime=image_mime,
                          invoice_fields=invoice_fields)
        answer = res.get("answer", "")
        store.append(sid, "user", text)
        store.append(sid, "assistant", answer)
        if res.get("intent") == "B":
            store.set_draft(sid, res.get("draft"))
        return 200, res

    if method == "POST" and path == "/ocr":
        # 发票 OCR 是填单前置动作，同 /chat 一并限员工（财务/管理员走审核台）
        if auth_ctx.get("role") != ROLE_EMPLOYEE:
            return 403, {"error": "财务/管理员请使用财务审核台（/finance），发票识别仅限员工角色"}
        # M9 发票 OCR：图片入口防线（魔数/大小）→ 限流 → 视觉引擎抽取结构化字段
        engine = ocr_engine if ocr_engine is not None else OCR_ENGINE
        if engine is None:
            return 503, {"error": "OCR not configured (set DEEPSEEK_API_KEY in .env)"}
        sid = (body.get("session_id") or "default")[:64]
        ok, err = auth.bind_sid(sid, auth_ctx.get("username", ""))
        if not ok:
            return 403, {"error": err}
        if not OCR_LIMITER.allow(f"ocr:{sid}"):
            return 429, {"error": "rate limit exceeded, please try later"}
        raw, mime = _decode_image(str(body.get("image_b64") or ""), OCR_MAX_BYTES)
        if raw is None:
            return 400, {"error": (f"invalid image (bad base64 / >{_OCR_MAX_MB}MB / "
                                   "unsupported type; jpeg/png/gif/webp only)")}
        filename = sanitize_text(body.get("filename") or "invoice", max_length=64) or "invoice"
        try:
            result = engine.extract(
                image_b64=base64.b64encode(raw).decode(),
                content_type=mime, filename=filename,
            )
        except OCRError as e:
            return 502, {"error": f"OCR failed: {e}"}
        fields = dict(result.fields)
        return 200, {
            "ok": True,
            "fields": fields,
            "warnings": result.warnings,
            "preview": build_invoice_preview(fields),
            "invoice_info": result.invoice_info,
            "mime": mime,
            "usage": result.usage,
            "model": result.model,
            "provider": result.provider,
            "latency_ms": result.latency_ms,
        }

    if method == "POST" and path == "/reimburse":
        # 同 /chat：结构化填单直提仅限员工，财务/管理员不给自己填单
        if auth_ctx.get("role") != ROLE_EMPLOYEE:
            return 403, {"error": "财务/管理员请使用财务审核台（/finance），填单仅限员工角色"}
        f = body
        # M8 权限覆写：申请人/工号以登录身份为准（员工不能替别人填单）；
        # 部门以提交者口述为准（body 值优先）
        username = auth_ctx.get("username", "")
        applicant = auth_ctx.get("name") or username or f.get("applicant", "")
        department = f.get("department") or ""
        # 直连飞书写入（审计与回溯由安全审计服务侧按动作日志实现）
        if not agent.feishu:
            return 503, {"error": "feishu not connected (set FEISHU_APP_ID in .env)"}
        try:
            rec = build_reimbursement_record(
                applicant=applicant,
                department=department,
                rtype=f.get("rtype", ""),
                amount=f.get("amount", 0),
                reason=f.get("reason", ""),
                has_invoice=f.get("has_invoice", ""),
                city=f.get("city", ""),
                date_str=f.get("date_str", ""),
                compliance=f.get("compliance", "通过"),
                risk_summary=f.get("risk_summary", ""),
                status=f.get("status", "待审核"),
                employee_id=auth_ctx.get("employee_id", ""),
            )
            rec = agent._fit_schema(rec)  # M8/P5：旧表无 工号 列时自动剔除，防整单写入失败
            ids = agent.feishu.create_records(agent.table_id, [rec])
            rid = ids[0] if ids else None
            bill_no = agent._fetch_bill_no(rid) if rid else ""
            return 200, {"record_id": rid, "bill_no": bill_no, "status": "待审核"}
        except Exception as e:  # 飞书错误（code/msg 不含密钥）透明返回
            return 500, {"error": str(e)}

    if method == "POST" and path == "/progress":
        applicant = (body.get("applicant") or "").strip()
        bill_no = (body.get("bill_no") or "").strip()
        # M8 角色权限：员工只能查自己的进度（忽略 body 中的 applicant/bill_no，防越权）
        if auth_ctx.get("role") == ROLE_EMPLOYEE:
            applicant = (auth_ctx.get("name") or auth_ctx.get("username") or "").strip()
            bill_no = ""
        if bill_no:
            q = f"查 {bill_no}"
        elif applicant:
            q = f"查一下{applicant}的报销"
        else:
            return 400, {"error": "applicant or bill_no required"}
        res = agent.route(q)
        return 200, res

    # -------------------------------------------------- M10 财务审核页（直连飞书多维表格）
    if method == "POST" and path == "/finance/meta":
        denied = _finance_role_gate(auth_ctx)
        if denied:
            return denied
        return 200, _finance_meta(agent, auth_ctx)

    if method == "POST" and path == "/finance/list":
        denied = _finance_role_gate(auth_ctx)
        if denied:
            return denied
        feishu, table_id = _finance_feishu(agent)
        if not feishu:
            return 503, {"error": "未接入飞书多维表格（缺少 FEISHU_APP_ID / FEISHU_TABLE_ID 配置）"}

        def _do_list():
            # 财务/管理员看全部单据；按提交时间倒序（新单在前）
            recs = feishu.list_records(table_id, page_size=500)
            rows = [_finance_row(r) for r in recs]
            rows.sort(key=lambda r: (r.get("submitted_at") or 0), reverse=True)
            return {"scope": "all", "count": len(rows), "rows": rows}

        return _finance_feishu_call(_do_list)

    if method == "POST" and path == "/finance/detail":
        denied = _finance_role_gate(auth_ctx)
        if denied:
            return denied
        feishu, table_id = _finance_feishu(agent)
        if not feishu:
            return 503, {"error": "未接入飞书多维表格（缺少 FEISHU_APP_ID / FEISHU_TABLE_ID 配置）"}
        record_id = (body.get("record_id") or "").strip()
        if not record_id:
            return 400, {"error": "缺少单据 record_id"}

        def _do_detail():
            rec = feishu.get_record(table_id, record_id)
            if not rec or not rec.get("record_id"):
                return 404, {"error": "单据不存在或已被删除"}
            return 200, {"row": _finance_row(rec)}

        return _finance_feishu_call(_do_detail)

    if method == "POST" and path == "/finance/review":
        denied = _finance_role_gate(auth_ctx)
        if denied:
            return denied
        record_id = (body.get("record_id") or "").strip()
        status = (body.get("status") or "").strip()
        opinion = (body.get("opinion") or "").strip()
        if not record_id:
            return 400, {"error": "缺少单据 record_id"}
        if status not in _FINANCE_REVIEW_STATUS:
            return 400, {"error": f"审核状态只能是 {' / '.join(_FINANCE_REVIEW_STATUS)}"}
        if len(opinion) < 2:
            return 400, {"error": "审批意见至少 2 个字"}

        # 经主服务器写入口：服务端做权限矩阵 / 状态机 / 盖章（审核人+审批时间）/
        # 整行快照 / 审计哈希链。网关不传审核人与审批时间。终态由主服务器状态机判定。
        try:
            data = backend_client.review_as(
                auth_ctx.get("username", ""),
                record_id=record_id, status=status, opinion=opinion)
        except backend_client.BackendRejected as e:
            return (e.status or 400), {"error": str(e)}
        except backend_client.BackendSessionMissing as e:
            return 503, {"error": str(e)}
        except backend_client.BackendError as e:
            return 502, {"error": f"主服务器暂时不可用：{e}"}
        return 200, {"message": data.get("message") or f"已审核：{status}"}

    if method == "POST" and path == "/finance/update":
        denied = _finance_role_gate(auth_ctx)
        if denied:
            return denied
        record_id = (body.get("record_id") or "").strip()
        patch = body.get("fields") or {}
        if not record_id:
            return 400, {"error": "缺少单据 record_id"}
        if not isinstance(patch, dict) or not patch:
            return 400, {"error": "没有要修改的字段"}

        fields = {}
        for name, value in patch.items():
            if name not in _FINANCE_EDITABLE:
                return 400, {"error": f"字段不允许修改：{name}"}
            fields[name] = value
        # 日期：网关先校验可解析，再归一成 YYYY-MM-DD 字符串发给主服务器
        # （主服务器自己转毫秒写飞书，网关不传毫秒）
        if "发票日期" in fields:
            ts = _parse_date_to_ts(fields["发票日期"])
            if ts is None:
                return 400, {"error": "发票日期格式无法解析（应为 YYYY-MM-DD 或 YYYY/M/D）"}
            fields["发票日期"] = time.strftime("%Y-%m-%d", time.localtime(ts / 1000))
        # 金额：校验为数字后归一成干净字符串（不要科学计数 / 千分位，主服务器要求 string）
        if "金额" in fields:
            try:
                amt = float(fields["金额"])
            except (TypeError, ValueError):
                return 400, {"error": "金额必须是数字"}
            fields["金额"] = _format_amount(amt)
        # 枚举校验（提前拦；主服务器为权威）
        errs = validate_enum_fields(fields)
        if errs:
            detail = "；".join(f"{f}：{v}（不在允许枚举内）" for f, v in errs)
            return 400, {"error": detail}
        # 表结构过滤（有飞书表时按真实列过滤；否则信任主服务器过滤）
        feishu, table_id = _finance_feishu(agent)
        if feishu and table_id:
            fields = filter_by_schema(fields, feishu.field_names(table_id))
        if not fields:
            return 400, {"error": "没有可写入的字段（可能被表结构过滤）"}

        # 经主服务器写入口（服务端做合规复算 / 盖章 / 快照 / 审计）
        try:
            data = backend_client.update_as(
                auth_ctx.get("username", ""),
                record_id=record_id, fields=fields)
        except backend_client.BackendRejected as e:
            return (e.status or 400), {"error": str(e)}
        except backend_client.BackendSessionMissing as e:
            return 503, {"error": str(e)}
        except backend_client.BackendError as e:
            return 502, {"error": f"主服务器暂时不可用：{e}"}
        return 200, {"message": data.get("message") or f"已修改 {len(fields)} 个字段"}

    if method == "POST" and path == "/finance/delete":
        # 角色门保留（财务与管理员均可触达该端点）；最终删除权由主服务器权限矩阵裁决
        #（invoice.delete 仅管理员及以上，财务会收到主服务器 403，文案原样回显）。
        denied = _finance_role_gate(auth_ctx)
        if denied:
            return denied
        record_id = (body.get("record_id") or "").strip()
        reason = (body.get("reason") or "").strip()
        if not record_id:
            return 400, {"error": "缺少单据 record_id"}
        if len(reason) < 2:
            return 400, {"error": "请填写删除理由（至少 2 个字）"}

        # 经主服务器写入口（物理删除由主服务器执行，附审计与快照）
        try:
            data = backend_client.delete_as(
                auth_ctx.get("username", ""),
                record_id=record_id, reason=reason)
        except backend_client.BackendRejected as e:
            return (e.status or 400), {"error": str(e)}
        except backend_client.BackendSessionMissing as e:
            return 503, {"error": str(e)}
        except backend_client.BackendError as e:
            return 502, {"error": f"主服务器暂时不可用：{e}"}
        return 200, {"message": data.get("message") or f"已删除单据 {record_id}（理由：{reason}）"}

    return 404, {"error": "not found", "path": path}


# ---------------------------------------------------- HTTP 端点（FastAPI 包装 dispatch）
def _client_ip(request: Request) -> str:
    return request.client.host if (request and request.client) else ""


@app.get("/health")
def health():
    code, p = dispatch("GET", "/health", {}, AGENT, STORE)
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/login")
def login(req: LoginRequest, request: Request):
    code, p = dispatch("POST", "/login", req.model_dump(), AGENT, STORE,
                       auth=AUTH, client_ip=_client_ip(request))
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.get("/me")
def me(authorization: str = Header(default="")):
    code, p = dispatch("GET", "/me", {}, AGENT, STORE, auth=AUTH, token=authorization)
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/logout")
def logout(request: Request, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/logout", {}, AGENT, STORE,
                       auth=AUTH, token=authorization, client_ip=_client_ip(request))
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/chat")
def chat(req: ChatRequest, request: Request, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/chat", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization, client_ip=_client_ip(request))
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/ocr")
def ocr(req: OcrRequest, request: Request, authorization: str = Header(default="")):
    """M9 发票 OCR：DeepSeek 视觉模型抽取发票字段（认证墙 + 魔数校验 + 独立限流）。"""
    code, p = dispatch("POST", "/ocr", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization, client_ip=_client_ip(request))
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/reimburse")
def reimburse(req: ReimburseRequest, request: Request, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/reimburse", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization, client_ip=_client_ip(request))
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/progress")
def progress(req: ProgressRequest, request: Request, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/progress", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization, client_ip=_client_ip(request))
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


# ---------------------------------------------------- 财务审核页端点（M10，认证墙内）
@app.post("/finance/meta")
def finance_meta(authorization: str = Header(default="")):
    code, p = dispatch("POST", "/finance/meta", {}, AGENT, STORE,
                       auth=AUTH, token=authorization)
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/finance/list")
def finance_list(req: FinanceListRequest, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/finance/list", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization)
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/finance/detail")
def finance_detail(req: FinanceDetailRequest, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/finance/detail", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization)
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/finance/review")
def finance_review(req: FinanceReviewRequest, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/finance/review", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization)
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/finance/update")
def finance_update(req: FinanceUpdateRequest, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/finance/update", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization)
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.post("/finance/delete")
def finance_delete(req: FinanceDeleteRequest, authorization: str = Header(default="")):
    code, p = dispatch("POST", "/finance/delete", req.model_dump(), AGENT, STORE,
                       auth=AUTH, token=authorization)
    if code != 200:
        raise HTTPException(status_code=code, detail=p)
    return p


@app.get("/finance/attachment")
def finance_attachment(record_id: str = Query(..., min_length=1, max_length=64),
                      file_token: str = Query(..., min_length=1, max_length=128),
                      authorization: str = Header(default="")):
    """中转下载发票附件。

    不让浏览器直连飞书的原因：记录里的下载票据是**用户身份**临时链接，
    应用身份直接访问报 99991661；且票据会过期。这里用服务端 tenant_access_token
    取流再转发，链接长期有效，凭证也不出服务端。

    必须同时传 record_id 与 file_token：服务端据此回查该单据的附件，
    取飞书签发的原始 url（含 extra 表格鉴权票据）——客户端无法自行构造。
    """
    # 认证墙 + 角色门（与财务页其余端点同一口径）
    if AUTH is None:
        raise HTTPException(status_code=401, detail={"error": "auth not configured"})
    claims = AUTH.verify(_bearer(authorization))
    if claims is None:
        raise HTTPException(status_code=401, detail={"error": "未登录或登录已过期，请重新登录"})
    if claims.get("role") not in _FINANCE_ROLES:
        raise HTTPException(status_code=403, detail={"error": "只有财务或管理员可以查看发票附件"})
    feishu, table_id = _finance_feishu(AGENT)
    if not feishu:
        raise HTTPException(status_code=503, detail={"error": "未接入飞书多维表格"})

    try:
        rec = feishu.get_record(table_id, record_id)
        files = (rec.get("fields") or {}).get("发票附件") or []
        hit = next((f for f in files if isinstance(f, dict)
                    and f.get("file_token") == file_token), None)
        if hit is None:
            raise HTTPException(status_code=404, detail={"error": "该单据下找不到此附件"})
        data, ctype = feishu.download_media(file_token, url=hit.get("url") or "")
    except FeishuError as e:
        raise HTTPException(status_code=502, detail={"error": str(e)})

    # 内联展示：图片直接看，其余类型强制下载（类型由飞书判定，不接受前端指定）
    disp = "inline" if str(ctype).startswith("image/") else "attachment"
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f'{disp}; filename="invoice"',
                             "Cache-Control": "private, max-age=600"})


# 注：/finance/rollback 已随主服务器适配层一并移除——版本回溯由安全审计服务侧
# 按动作日志（删单+加单代码）实现，本网关只负责四类直接操作。


# ---------------------------------------------------- 启动（uvicorn）
def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    print(f"[M5] API 网关启动 http://{host}:{port}  (Ctrl+C 停止)")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="小财奴 M5 API 网关 (FastAPI)")
    ap.add_argument("host", nargs="?", default="0.0.0.0")
    ap.add_argument("port", nargs="?", type=int, default=8000)
    args = ap.parse_args()
    run(args.host, args.port)
