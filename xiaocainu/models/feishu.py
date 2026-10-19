"""
M3 · 飞书多维表格（Bitable）客户端
=================================
设计要点
--------
- 支撑小财 B/C/D 分支：B 填单写草稿、C 合规预审回写、D 进度查询。
- 纯标准库 urllib 实现 HTTP（不引入 requests，贴合项目「少依赖/可扫描」原则）。
- 传输层抽象为 FeishuTransport 接口：真实用 UrllibTransport；测试注入 FakeTransport，
  离线、零网络、零密钥可测。
- token-swap-first：任何 API 调用前先确保 tenant_access_token，并缓存+提前 60s 续期。
- 安全：app_secret 仅存实例、不写日志；异常信息不含密钥；内部字段(record_id 等)不进聊天层。

接口（线上用法）
----------------
    client = FeishuBitableClient.from_env()          # 读 .env：FEISHU_APP_ID/SECRET/APP_TOKEN
    ids = client.create_records("tblXXXX", [fields]) # 批量写，返回 record_id 列表
    recs = client.list_records("tblXXXX", page_size=20)  # 查询，返回 [{record_id, fields}]

测试（离线）
--------
    fake = FakeTransport(...)                          # 模拟飞书返回
    client = FeishuBitableClient("app_id","app_secret", transport=fake)
"""
from __future__ import annotations

import os
import re
import time
import json as _json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Protocol, Tuple, runtime_checkable


# ---------------------------------------------------- 异常（信息安全：不含密钥）
class FeishuError(Exception):
    """飞书接口错误。构造时只带 code/msg，绝不拼入 app_secret。"""


# ---------------------------------------------------- 传输层接口（可插拔）
@runtime_checkable
class FeishuTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        data: Optional[bytes] = None,
    ) -> dict:
        ...


class UrllibTransport:
    """真实传输层：基于标准库 urllib，无第三方依赖。"""

    def __init__(self, timeout: int = 10, verbose: bool = False) -> None:
        self.timeout = timeout
        self.verbose = verbose

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        data: Optional[bytes] = None,
    ) -> dict:
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        if json is not None:
            data = _json.dumps(json).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        # JSON body 且调用方未显式指定 Content-Type 时补默认值（multipart 等场景不覆盖）
        if json is not None and "content-type" not in {k.lower() for k in (headers or {})}:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # 飞书错误也可能落在 HTTP 层；尽量解析 body 拿到业务 code/msg
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                raise FeishuError(f"飞书 HTTP 错误 {e.code}") from e
        if self.verbose:
            # 调试开关：打印飞书原始回传（含 code/msg/时间戳），便于自测排查
            print(f"[Feishu raw] {method} {url}\n  {raw}")
        try:
            return _json.loads(raw)
        except Exception as exc:  # 兜底：网络/格式异常统一为 FeishuError
            raise FeishuError(f"飞书响应解析失败: {exc}") from exc


# ---------------------------------------------------- multipart 编码（M9 附件上传，纯 stdlib）
def _build_multipart(
    fields: Dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    boundary: str,
) -> bytes:
    """构造 multipart/form-data 报文（RFC 2046）：文本字段 + 单个文件字段。"""
    parts: list = []
    for k, v in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                f"{v}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts)


# ---------------------------------------------------- 客户端
class FeishuBitableClient:
    """飞书 Bitable 客户端：鉴权 + 写入 + 查询。凭证只存实例，不外泄。"""

    BASE = "https://open.feishu.cn/open-apis"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        app_token: str = "",
        transport: Optional[FeishuTransport] = None,
        token_margin: int = 60,
        verbose: bool = False,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret  # 仅存实例，禁止 print/日志输出
        self.app_token = app_token  # 多维表格 base id
        self.transport = transport or UrllibTransport(verbose=verbose)
        self.token_margin = token_margin
        self.verbose = verbose
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._fields_cache: Dict[str, set] = {}  # table_id -> 字段名集合（进程内缓存）

    @classmethod
    def from_env(cls) -> "FeishuBitableClient":
        return cls(
            os.getenv("FEISHU_APP_ID", ""),
            os.getenv("FEISHU_APP_SECRET", ""),
            os.getenv("FEISHU_APP_TOKEN", ""),
        )

    # -------------------------------------------------- token-swap-first 鉴权
    def _ensure_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp:
            return self._token
        resp = self.transport.request(
            "POST",
            f"{self.BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        if resp.get("code", -1) != 0:
            # 注意：msg 来自服务端，本身不含本地密钥；不额外拼接任何私密字段
            raise FeishuError(f"获取飞书令牌失败 code={resp.get('code')} msg={resp.get('msg')}")
        self._token = resp.get("tenant_access_token")
        expire = resp.get("expire", 7200)
        self._token_exp = now + max(0, int(expire) - self.token_margin)
        return self._token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    def _require_app_token(self, app_token: Optional[str]) -> str:
        at = app_token or self.app_token
        if not at:
            raise FeishuError("缺少 app_token（多维表格 base id）")
        return at

    # -------------------------------------------------- 写入（B/C 分支）
    def create_records(
        self, table_id: str, records: List[dict], app_token: str = ""
    ) -> List[str]:
        """批量新增记录。records 为字段字典列表。返回 record_id 列表（屏蔽原始 resp）。"""
        at = self._require_app_token(app_token)
        payload = {"records": [{"fields": f} for f in records]}
        resp = self.transport.request(
            "POST",
            f"{self.BASE}/bitable/v1/apps/{at}/tables/{table_id}/records/batch_create",
            json=payload,
            headers=self._auth_headers(),
        )
        if resp.get("code", -1) != 0:
            raise FeishuError(f"飞书写入失败 code={resp.get('code')} msg={resp.get('msg')}")
        recs = resp.get("data", {}).get("records", [])
        ids = [r.get("record_id") for r in recs]
        if self.verbose:
            print(f"[Feishu create_records] 写入 {len(ids)} 条 -> {ids}")
        # 只回 record_id，屏蔽 feishu_resp 原始 JSON（安全/整洁）
        return ids

    # -------------------------------------------------- 更新（账号管理 / 审批回写）
    def update_record(
        self, table_id: str, record_id: str, fields: dict, app_token: str = ""
    ) -> str:
        """更新单条记录的部分字段（fields 只放要改的键）。返回 record_id。"""
        at = self._require_app_token(app_token)
        resp = self.transport.request(
            "PUT",
            f"{self.BASE}/bitable/v1/apps/{at}/tables/{table_id}/records/{record_id}",
            json={"fields": fields},
            headers=self._auth_headers(),
        )
        if resp.get("code", -1) != 0:
            raise FeishuError(f"飞书更新失败 code={resp.get('code')} msg={resp.get('msg')}")
        rid = resp.get("data", {}).get("record", {}).get("record_id") or record_id
        if self.verbose:
            print(f"[Feishu update_record] {rid} 更新字段 {list(fields.keys())}")
        return rid

    # -------------------------------------------------- 删除（财务审核页删单）
    def delete_record(
        self, table_id: str, record_id: str, app_token: str = ""
    ) -> bool:
        """删除单条记录。返回 True。

        飞书删除是物理删除、行不可恢复（单号为自动编号也不会复用）。
        调用方须在 UI 层做二次确认与理由留痕；版本回溯由安全审计侧按动作日志重建。
        """
        at = self._require_app_token(app_token)
        resp = self.transport.request(
            "DELETE",
            f"{self.BASE}/bitable/v1/apps/{at}/tables/{table_id}/records/{record_id}",
            headers=self._auth_headers(),
        )
        if resp.get("code", -1) != 0:
            raise FeishuError(f"飞书删除失败 code={resp.get('code')} msg={resp.get('msg')}")
        if self.verbose:
            print(f"[Feishu delete_record] 已删除 {record_id}")
        return True

    # -------------------------------------------------- 附件下载（发票预览）
    def download_media(self, file_token: str, url: str = "") -> Tuple[bytes, str]:
        """下载多维表附件。返回 (二进制, Content-Type)。

        url 必须传**查询记录时飞书返回的原始 url 字段**（内含 extra 表格鉴权票据）。
        实测（2026-10-19）：带 extra 的原始 url 用应用身份 tenant_access_token
        直接可下载（HTTP 200 image/png）；自己拼 URL 会丢 extra → HTTP 400。
        因此本方法不自行拼 URL——extra 由飞书签发，客户端无法构造。

        传空 url 时（理论上不可达）退回按 token 拼，仅作兼容；
        正常调用方应总是带上飞书返回的 url。
        """
        if not file_token or "/" in file_token or len(file_token) > 128:
            raise FeishuError("附件 file_token 非法")
        # 只接受飞书官方域名的 url，防 SSRF（file_token 非法也已拦一道）
        target = ""
        if url and url.startswith("https://open.feishu.cn/"):
            target = url
        elif url:
            raise FeishuError("附件下载地址非飞书官方域名，已拒绝")
        else:
            target = f"{self.BASE}/drive/v1/medias/{file_token}/download"

        req = urllib.request.Request(target, method="GET")
        for k, v in self._auth_headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.transport.timeout) as resp:
                data = resp.read()
                ctype = (resp.headers.get("Content-Type") or "application/octet-stream")
        except urllib.error.HTTPError as e:
            # 飞书的权限错误体是 JSON，取出可读原因；不回显凭证
            raw = ""
            try:
                raw = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            code = ""
            msg = ""
            try:
                d = _json.loads(raw)
                code = str(d.get("code", ""))
                msg = str(d.get("msg", ""))[:120]
            except Exception:
                pass
            hint = ""
            if code == "99991672":
                hint = "（应用身份缺少 drive:drive:readonly 权限）"
            elif not code:
                hint = "（地址缺少 extra 表格鉴权票据，请传飞书返回的原始 url）"
            raise FeishuError(f"附件下载失败 HTTP {e.code} code={code} {msg}{hint}")
        except urllib.error.URLError as e:
            raise FeishuError(f"附件下载网络错误：{e.reason}")
        if len(data) > 20 * 1024 * 1024:
            raise FeishuError("附件超过 20MB，拒绝中转")
        if self.verbose:
            print(f"[Feishu download_media] {file_token} -> {len(data)} 字节 {ctype}")
        return data, ctype

    # -------------------------------------------------- 附件上传（M9 发票存档）
    def upload_attachment(
        self,
        filename: str,
        file_bytes: bytes,
        content_type: str = "application/octet-stream",
        app_token: str = "",
    ) -> str:
        """上传附件到多维表格（drive media upload_all，stdlib 手工 multipart）。

        返回 file_token；写入记录附件列用 [{"file_token": token}]（配合 update_record）。
        飞书 upload_all 单文件上限 20MB；parent_type=bitable_file，parent_node=base app_token。
        """
        at = self._require_app_token(app_token)
        if not file_bytes:
            raise FeishuError("附件内容为空")
        if len(file_bytes) > 20 * 1024 * 1024:
            raise FeishuError(f"附件超过 20MB 上限（{len(file_bytes)} 字节）")
        boundary = "xiaocainu-m9-3a9f1c7d5e2b"
        body = _build_multipart(
            fields={
                "file_name": filename,
                "parent_type": "bitable_file",
                "parent_node": at,
                "size": str(len(file_bytes)),
            },
            file_field="file",
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
            boundary=boundary,
        )
        resp = self.transport.request(
            "POST",
            f"{self.BASE}/drive/v1/medias/upload_all",
            data=body,
            headers={
                **self._auth_headers(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        if resp.get("code", -1) != 0:
            raise FeishuError(f"飞书附件上传失败 code={resp.get('code')} msg={resp.get('msg')}")
        token = (resp.get("data") or {}).get("file_token", "")
        if not token:
            raise FeishuError("飞书附件上传响应缺少 file_token")
        if self.verbose:
            print(f"[Feishu upload_attachment] {filename} -> file_token={token}")
        return token

    # -------------------------------------------------- 查询（D 分支）
    def list_records(
        self,
        table_id: str,
        *,
        page_size: int = 20,
        filter_expr: str = "",
        app_token: str = "",
    ) -> List[dict]:
        """查询记录。返回 [{record_id, fields}]。"""
        at = self._require_app_token(app_token)
        params: Dict[str, object] = {"page_size": page_size}
        if filter_expr:
            params["filter"] = filter_expr
        resp = self.transport.request(
            "GET",
            f"{self.BASE}/bitable/v1/apps/{at}/tables/{table_id}/records",
            params=params,
            headers=self._auth_headers(),
        )
        if resp.get("code", -1) != 0:
            raise FeishuError(f"飞书查询失败 code={resp.get('code')} msg={resp.get('msg')}")
        items = resp.get("data", {}).get("items", [])
        return [{"record_id": it.get("record_id"), "fields": it.get("fields", {})} for it in items]

    # -------------------------------------------------- 字段探测（写前先知道表结构）
    def list_fields(self, table_id: str, *, app_token: str = "") -> List[dict]:
        """查询表格字段定义（名称/类型/选项）。写前探测可避免字段类型不匹配导致写入失败。"""
        at = self._require_app_token(app_token)
        resp = self.transport.request(
            "GET",
            f"{self.BASE}/bitable/v1/apps/{at}/tables/{table_id}/fields",
            headers=self._auth_headers(),
        )
        if resp.get("code", -1) != 0:
            raise FeishuError(f"飞书字段查询失败 code={resp.get('code')} msg={resp.get('msg')}")
        return resp.get("data", {}).get("items", [])

    def field_names(self, table_id: str, *, app_token: str = "", use_cache: bool = True) -> set:
        """表字段名集合（进程内缓存，一次探测后复用——写前过滤用，防未知字段整单写入失败）。

        use_cache=False 强制重探（切表/改表后调用）。探测失败抛 FeishuError 由调用方决定兜底。
        """
        cached = self._fields_cache.get(table_id)
        if use_cache and cached is not None:
            return cached
        items = self.list_fields(table_id, app_token=app_token)
        names = {it.get("field_name", "") for it in items} - {""}
        self._fields_cache[table_id] = names
        return names

    def get_record(self, table_id: str, record_id: str, app_token: str = "") -> dict:
        at = self._require_app_token(app_token)
        resp = self.transport.request(
            "GET",
            f"{self.BASE}/bitable/v1/apps/{at}/tables/{table_id}/records/{record_id}",
            headers=self._auth_headers(),
        )
        if resp.get("code", -1) != 0:
            raise FeishuError(f"飞书单条查询失败 code={resp.get('code')} msg={resp.get('msg')}")
        data = resp.get("data", {})
        # ⚠️ 真实结构：字段在 data.record.fields（不是 data.fields），
        # 曾因读错层级导致单号恒取不到。此处做兼容：优先 data.record，异常时退回 data 本身。
        record = data.get("record") if isinstance(data.get("record"), dict) else data
        return {
            "record_id": record.get("record_id") or record.get("id"),
            "fields": record.get("fields", {}),
        }


# ---------------------------------------------------- 报销单领域 schema（对齐真实飞书表 tbl5Dz4SdKmmwgP5）
# 真实表共 14 字段：无「工号」（D 查询改用 申请人），「单号」为 AutoNumber 只读（代码不写）。
# M8/P5 企业级升级表（V2）新增：工号 / 审批意见 / 审批时间 —— 写入前经 field_names+filter_by_schema
# 探测过滤，同一份代码兼容 14/17 字段两种表（.env 切换 FEISHU_TABLE_ID 即用）。
REIMBURSEMENT_FIELDS = [
    "事由", "单号", "合规结果", "审核人", "报销类型",
    "记录提交时间", "状态", "申请人", "票据状态", "部门", "金额", "风险摘要",
    "城市", "发票日期",
    "工号", "审批意见", "审批时间",
    "发票信息", "发票附件",  # M9 OCR：发票号码/销售方 文本 + 发票图片附件（旧表缺列由 filter_by_schema 剔除）
]
# bot 直交 4 类（与填单规则.md + 真实表「报销类型」选项前 4 项一致）
ALLOWED_TYPES = {"差旅费", "招待费", "办公用品", "交通费"}

# ---------------------------------------------------- 企业标准枚举（与 docs/TABLE_SETUP.md 枚举版一致）
# M8 白名单强校验 + 抽取层字段归一路由的权威来源；任何写库路径都必须经过 validate_enum_fields。
# 部门字典（主表与用户表共用，必须逐字一致）
DEPT_WHITELIST = (
    "研发部", "市场部", "财务部", "人力资源部", "行政部", "生产部", "销售部",
    "采购部", "信息技术部", "质量管理部", "客户服务部",
)
# 城市字典（差旅分级城市主数据；与 TABLE_SETUP.md 附表一致）
_CITY_T1 = ("北京", "上海", "广州", "深圳")  # 一线
_CITY_T2 = ("成都", "杭州", "武汉", "西安", "南京", "重庆", "苏州", "天津", "长沙",
            "郑州", "青岛", "宁波", "东莞", "无锡", "沈阳", "昆明", "佛山", "合肥", "济南")  # 新一线
_CITY_T3 = ("福州", "厦门", "哈尔滨", "太原", "南昌", "南宁", "贵阳", "石家庄", "兰州",
            "大连", "常州", "温州", "烟台", "泉州")  # 二线
CITY_WHITELIST = _CITY_T1 + _CITY_T2 + _CITY_T3
# 城市分级住宿上限（元/晚）：一线 500 / 新一线 350 / 二线 250
CITY_TIER = {c: 500 for c in _CITY_T1}
CITY_TIER.update({c: 350 for c in _CITY_T2})
CITY_TIER.update({c: 250 for c in _CITY_T3})
# 票据状态 / 合规结果 / 状态 枚举
ALLOWED_INVOICE = ("有票", "无票", "未提")
COMPLIANCE_RESULTS = ("通过", "风险", "驳回", "超2000需总经理审批")
STATUS_VALUES = ("待审核", "审核中", "已通过", "已驳回")


def validate_enum_fields(record: dict) -> list:
    """写前枚举强校验：部门/城市/报销类型/票据状态/合规结果/状态 必须为固定枚举值。

    返回 [(字段, 非法值), ...]；空列表 = 全部合法（空值不参与校验）。
    落库前最后一道闸门——杜绝任何非标准枚举值（如「火星部」「电脑部门」）写入飞书。
    """
    errors = []
    for field, allowed in (
        ("部门", DEPT_WHITELIST),
        ("城市", CITY_WHITELIST),
        ("报销类型", ALLOWED_TYPES),
        ("票据状态", ALLOWED_INVOICE),
        ("合规结果", COMPLIANCE_RESULTS),
        ("状态", STATUS_VALUES),
    ):
        v = record.get(field)
        if v and v not in allowed:
            errors.append((field, v))
    return errors

COMPANY_NAME = "示例科技有限公司"  # 占位：比赛按真实公司名替换


def _parse_date_to_ts(date_str):
    """日期(YYYY/M/D 或 YYYY-M-D 或 YYYY年M月D日 或 YYYYMMDD) → 东八区当天0点毫秒时间戳。
    与飞书「发票日期」列（毫秒时间戳）对齐；无法解析返回 None。"""
    if not date_str:
        return None
    s = str(date_str).strip()
    cn = timezone(timedelta(hours=8))  # 飞书按浏览者时区显示，固定东八区避免漂移
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y%m%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=cn)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def build_reimbursement_record(
    *,
    applicant: str = "",
    department: str = "",
    rtype: str = "",
    amount=0,
    reason: str = "",
    has_invoice: str = "",
    city: str = "",
    date_str: str = "",
    compliance: str = "",
    risk_summary: str = "",
    status: str = "待审核",
    employee_id: str = "",
) -> dict:
    """构造一张符合真实主表 schema 的报销单字段字典（12~13 字段，缺省空）。

    对齐代码执行mod.md 标准版 body：
    - 不写「单号」：飞书 AutoNumber 只读，由系统自动生成（代码取回展示）。
    - 「工号」仅在传入时写入（M8 登录身份自动带出；旧表无此字段，写入前由 schema 过滤兜底）。
    - 不写「审核人」：留待财务/负责人审核时填写。
    - 「发票日期」由 date_str 解析为东八区毫秒时间戳；缺失则兜底记录提交时间。
    """
    now_ts = int(time.time() * 1000)
    invoice_ts = _parse_date_to_ts(date_str) or now_ts
    try:
        amount_val = float(amount)
    except (TypeError, ValueError):
        amount_val = 0.0
    rec = {
        "申请人": applicant,
        "部门": department,
        "报销类型": rtype,
        "金额": amount_val,
        "事由": reason,
        "票据状态": has_invoice,
        "状态": status,
        "城市": city,
        "发票日期": invoice_ts,
        "记录提交时间": now_ts,
        "合规结果": compliance,
        "风险摘要": risk_summary,
    }
    if employee_id:
        rec["工号"] = employee_id
    return rec


def public_view(record: dict) -> dict:
    """屏蔽内部键，只保留业务字段给聊天层展示（不泄露 record_id 等内部字段）。"""
    fields = record.get("fields", {})
    return {k: v for k, v in fields.items() if k in REIMBURSEMENT_FIELDS}


def filter_by_schema(record: dict, available_fields) -> dict:
    """按真实表字段集合过滤记录（M8/P5）：旧 14 字段表无 工号/审批意见/审批时间 列时
    自动剔除对应键，避免「未知字段导致整单写入失败」。"""
    allow = set(available_fields or ())
    if not allow:
        return record
    return {k: v for k, v in record.items() if k in allow}
