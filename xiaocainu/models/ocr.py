#发票 OCR 引擎 抽取结构化字段
from __future__ import annotations

import base64
import json
import re
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable

from .config import get_ocr_config
from .feishu import ALLOWED_TYPES, CITY_WHITELIST

logger = logging.getLogger("xiaocai.ocr")

#需要识别的日期格式
_DATE_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y%m%d")

#抽取提示词 只输出 JSON
_EXTRACT_PROMPT = (
    "你是企业财务发票识别引擎。请识别这张图片中的发票/收据/消费凭证关键字段，"
    "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"amount": 数字, 发票含税总金额(单位元), 无法识别用 null, '
    '"invoice_date": "开票日期 YYYY/M/D", 无法识别用空串, '
    '"invoice_no": "发票号码", 无则空串, '
    '"seller": "销售方名称", 无则空串, '
    '"rtype": "根据消费内容从 差旅费/招待费/办公用品/交通费 中选最贴近的一个", 无法判断则空串, '
    '"city": "消费发生城市", 无则空串}\n'
    "若图片不是发票/收据/消费凭证，只输出 {\"error\": \"not_invoice\"}。"
)

#输出字段白名单
_OUTPUT_KEYS = ("amount", "invoice_date", "invoice_no", "seller", "rtype", "city")


#OCR 业务错误基类
class OCRError(Exception):
    """OCR 业务错误。"""


@dataclass
class OCRResult:
    #归一化 OCR 结果
    fields: Dict[str, object]
    warnings: List[str] = field(default_factory=list)
    usage: Dict = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    latency_ms: int = 0

    @property
    def invoice_info(self) -> str:
        #拼成主表发票信息文本
        parts = [str(self.fields.get("invoice_no") or "").strip(),
                 str(self.fields.get("seller") or "").strip()]
        return "；".join(p for p in parts if p)


@runtime_checkable
class OCREngine(Protocol):
    #OCR 引擎接口
    name: str

    def extract(self, *, image_b64: str, content_type: str = "image/jpeg",
                filename: str = "") -> OCRResult:
        #抽取发票字段
        ...


def extract_json(text: str) -> dict:
    #从文本里抠出 JSON
    if not text:
        raise OCRError("OCR 返回为空")
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise OCRError(f"OCR 返回中未找到 JSON：{text[:120]}")
    try:
        obj = json.loads(s[start:end + 1])
    except ValueError as e:
        raise OCRError(f"OCR JSON 解析失败: {e}") from e
    if not isinstance(obj, dict):
        raise OCRError("OCR 返回 JSON 不是对象")
    return obj


def normalize_invoice_date(raw: object) -> str:
    #日期统一成 YYYY/M/D
    s = str(raw or "").strip()
    if not s:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            from datetime import datetime
            dt = datetime.strptime(s, fmt)
            return f"{dt.year}/{dt.month}/{dt.day}"
        except ValueError:
            continue
    return ""


def sanitize_fields(parsed: dict) -> tuple[Dict[str, object], List[str]]:
    #不合格枚举置空并告警
    warnings: List[str] = []
    out: Dict[str, object] = {k: "" for k in _OUTPUT_KEYS}
    out["amount"] = None

    raw_amount = parsed.get("amount")
    if raw_amount is not None:
        try:
            amt = float(raw_amount)
            if 0 <= amt <= 1e7:
                out["amount"] = amt
            else:
                warnings.append(f"金额越界({raw_amount})已置空，请人工确认")
        except (TypeError, ValueError):
            warnings.append(f"金额无法解析({raw_amount})已置空，请人工确认")

    out["invoice_date"] = normalize_invoice_date(parsed.get("invoice_date"))
    if parsed.get("invoice_date") and not out["invoice_date"]:
        warnings.append(f"开票日期无法解析({parsed.get('invoice_date')})已置空")

    for key, allowed in (("rtype", ALLOWED_TYPES), ("city", CITY_WHITELIST)):
        val = str(parsed.get(key) or "").strip()
        if val and val in allowed:
            out[key] = val
        elif val:
            warnings.append(f"{key}={val} 不在企业标准枚举内已置空")

    out["invoice_no"] = str(parsed.get("invoice_no") or "").strip()[:64]
    out["seller"] = str(parsed.get("seller") or "").strip()[:64]
    return out, warnings


def build_invoice_preview(fields: Dict[str, object]) -> str:
    #字段拼成一句草稿话术
    parts: List[str] = ["我上传了发票，有发票"]
    if fields.get("amount") is not None:
        parts.append(f"金额{fields['amount']:g}元")
    if fields.get("invoice_date"):
        parts.append(f"开票日期{fields['invoice_date']}")
    if fields.get("city"):
        parts.append(f"城市{fields['city']}")
    if fields.get("rtype"):
        parts.append(f"类型{fields['rtype']}")
    if fields.get("invoice_no"):
        parts.append(f"发票号码{fields['invoice_no']}")
    if fields.get("seller"):
        parts.append(f"销售方{fields['seller']}")
    return "，".join(parts)


class InvoiceOCREngine:
    #DeepSeek 视觉接口 真实引擎

    name = "deepseek-ocr"

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        detail: str = "high",
        timeout: int = 60,
        client=None,
    ) -> None:
        #读配置并建 client
        cfg = get_ocr_config()
        self.model = model or cfg["model"]
        self.detail = detail or cfg["detail"]
        self.name = cfg["name"]
        if client is not None:  #测试注入假 client
            self._client = client
        else:
            from openai import OpenAI  #延迟导入
            self._client = OpenAI(
                base_url=base_url or cfg["base_url"],
                api_key=api_key or cfg["api_key"],
                timeout=timeout,
            )

    @staticmethod
    def build_messages(image_b64: str, content_type: str = "image/jpeg") -> list:
        #拼多模态消息
        return [{
            "role": "user",
            "content": [
                {"type": "text", "text": _EXTRACT_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:{content_type};base64,{image_b64}",
                    "detail": "high",
                }},
            ],
        }]

    def extract(self, *, image_b64: str, content_type: str = "image/jpeg",
                filename: str = "") -> OCRResult:
        #校验入参是合法 base64
        try:
            base64.b64decode(image_b64, validate=True)
        except Exception as e:
            raise OCRError(f"image_b64 不是合法 base64: {e}") from e

        t0 = time.time()
        try:
            #关闭思考模式 抽取无需推理
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=self.build_messages(image_b64, content_type),
                temperature=0,
                max_tokens=600,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as e:
            raise OCRError(f"OCR 引擎调用失败: {e}") from e
        latency = int((time.time() - t0) * 1000)

        text = ""
        finish_reason = ""
        has_reasoning = False
        try:
            choice = resp.choices[0]
            text = choice.message.content or ""
            finish_reason = str(getattr(choice, "finish_reason", "") or "")
            has_reasoning = bool(getattr(choice.message, "reasoning_content", ""))
        except Exception as e:
            raise OCRError(f"OCR 响应结构异常: {e}") from e
        if not text.strip():
            raise OCRError(
                f"OCR 返回为空（finish_reason={finish_reason}，推理内容={has_reasoning}）——"
                "thinking 已强制关闭；若复现请检查模型名或调大 max_tokens"
            )

        parsed = extract_json(text)
        if str(parsed.get("error", "")).strip() == "not_invoice":
            raise OCRError(
                "没识别出报销要素，请上传相关发票，或更清晰的支付凭证截图"
                "（微信/支付宝「账单详情」页）")

        fields, warnings = sanitize_fields(parsed)
        usage = {}
        if getattr(resp, "usage", None):
            try:
                usage = resp.usage.model_dump()
            except Exception:
                usage = {}
        logger.info("发票OCR完成 model=%s latency=%sms warnings=%s",
                    self.model, latency, warnings)
        return OCRResult(fields=fields, warnings=warnings, usage=usage,
                         model=self.model, provider=self.name, latency_ms=latency)


class MockOcrEngine:
    #测试用假 OCR 零网络零成本

    name = "mock-ocr"

    def __init__(self, canned: Optional[Dict[str, object]] = None) -> None:
        #默认假发票字段
        self.canned = canned if canned is not None else {
            "amount": 800.0,
            "invoice_date": "2026/10/1",
            "invoice_no": "24312000000012345678",
            "seller": "北京示例酒店有限公司",
            "rtype": "差旅费",
            "city": "北京",
        }
        self.calls: List[Dict] = []

    def extract(self, *, image_b64: str, content_type: str = "image/jpeg",
                filename: str = "") -> OCRResult:
        #记录调用并返回固定字段
        self.calls.append({"image_b64_len": len(image_b64),
                           "content_type": content_type, "filename": filename})
        fields, warnings = sanitize_fields(dict(self.canned))
        return OCRResult(fields=fields, warnings=warnings, model="mock-ocr-model",
                         provider=self.name, latency_ms=5)
