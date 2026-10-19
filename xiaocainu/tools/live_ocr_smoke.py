"""M9 · 发票 OCR 真实联调冒烟（确定性版）
=========================================================
流程（单次真实模型调用，无任何试探请求）：
  1) 魔数嗅探图片真实 MIME（JPEG/PNG/WebP），全链路一致传递
  2) DeepSeek 视觉 OCR 识别发票 → 结构化字段（1 次真实调用，usage 回显）
  3) OCR 字段直接组装确定性草稿（不走 preview 文本重解析）：
     - 报销类型取 OCR rtype ∈ 白名单，否则用 --rtype 兜底
     - 金额/发票日期/城市/发票号码/销售方直取
  4) XiaoCaiAgent「确认提交」→ 写主表（含 发票信息）→ 上传发票附件
  5) get_record 回读验证「发票附件」列已落库

用法：
  python tools/live_ocr_smoke.py 路径/发票.png --applicant 张三 --department 研发部 [--rtype 办公用品]
前置：
  .env 已填真实 DEEPSEEK_API_KEY（deepseek-flash）+ 飞书凭证；主表已加「发票信息」「发票附件」两列。
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from models.env import load_env
from models.config import get_ocr_config
from models.feishu import FeishuBitableClient, FeishuError
from models.ocr import InvoiceOCREngine, OCRError
from models.retriever import Retriever
from models.llm import LLMAdapter, MockProvider
from models.agent import XiaoCaiAgent

load_env()


def sniff_mime(raw: bytes) -> str:
    """魔数嗅探：JPEG / PNG / WebP；未知默认 jpg（DeepSeek 文档支持三者）。"""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


_MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_INVOICE_TYPES = ("差旅费", "招待费", "办公用品", "交通费")


def main() -> None:
    ap = argparse.ArgumentParser(description="M9 发票 OCR 冒烟（确定性提交版）")
    ap.add_argument("image", help="发票照片路径（jpg/png/webp，≤8MB）")
    ap.add_argument("--applicant", default="张三")
    ap.add_argument("--department", default="研发部")
    ap.add_argument("--rtype", default="办公用品",
                    help="OCR 未识别出 4 类白名单类型时的兜底报销类型")
    ap.add_argument("--city", default="")
    ap.add_argument("--fields", default="",
                    help="JSON 字符串：复用已有 OCR 识别结果跳过识别步骤（省钱，不发起模型调用）"
                         '，如 --fields \'{"amount":100.0,"invoice_date":"2023/12/4",'
                         '"invoice_no":"2311","seller":"某公司","rtype":"办公用品","city":"北京"}\'')
    args = ap.parse_args()

    cfg = get_ocr_config()
    key = cfg["api_key"]
    if not key or "你的密钥" in key or len(key) < 20:
        print("❌ .env 未配置真实 DEEPSEEK_API_KEY（当前为占位/缺失），拒绝发起任何调用。")
        return
    if not os.path.exists(args.image):
        print(f"❌ 找不到发票图片：{args.image}")
        return
    with open(args.image, "rb") as f:
        raw = f.read()
    if len(raw) > cfg["max_image_mb"] * 1024 * 1024:
        print(f"❌ 图片超过 {cfg['max_image_mb']}MB，请压缩后再试。")
        return

    mime = sniff_mime(raw)
    ext = _MIME_EXT[mime]
    image_b64 = base64.b64encode(raw).decode()

    # ---- 1) DeepSeek 视觉 OCR（唯一一次真实模型调用；--fields 复用时跳过）----
    if args.fields:
        import json
        f = json.loads(args.fields)
        print(f"🔎 [1/4] 复用已有 OCR 结果（跳过识别，0 次模型调用）：{f}")
    else:
        print(f"🔎 [1/4] OCR 识别 {os.path.basename(args.image)}"
              f"（{len(raw)} 字节，真实 MIME={mime}，模型 {cfg['model']}，detail={cfg['detail']}）...")
        engine = InvoiceOCREngine(base_url=cfg["base_url"], api_key=key,
                                  model=cfg["model"], detail=cfg["detail"])
        try:
            result = engine.extract(image_b64=image_b64, content_type=mime,
                                    filename=os.path.basename(args.image))
        except OCRError as e:
            print(f"❌ OCR 失败：{e}")
            return
        f = result.fields
        print(f"   字段：{f}")
        if result.warnings:
            print(f"   ⚠️ 警告：{result.warnings}")
        print(f"   usage：{result.usage}（本次识别计费依据）")

    # ---- 2) 确定性组装草稿（不走 preview 文本重解析）----
    rtype = f.get("rtype") or ""
    if rtype not in _INVOICE_TYPES:
        print(f"   ℹ️ OCR 类型「{rtype or '(空)'}」不在 4 类白名单，用 --rtype 兜底：{args.rtype}"
              f"（仅为冒烟演示归类，可后续调整）")
        rtype = args.rtype
    draft = {
        "applicant": args.applicant,
        "department": args.department,
        "category": rtype,
        "amount": f.get("amount"),
        "reason": f.get("reason") or "发票 OCR 冒烟测试",
        "has_invoice": "有票",
        "invoice_no": str(f.get("invoice_no") or "")[:64],
        "invoice_seller": str(f.get("seller") or "")[:64],
        "await_confirm": True,
    }
    if args.city:
        draft["city"] = args.city
    if f.get("invoice_date"):
        draft["date"] = f["invoice_date"]
    missing = [k for k in ("applicant", "department", "category", "amount")
               if not draft.get(k)]
    if missing:
        print(f"❌ 草稿缺必填项：{missing}，当前草稿：{draft}")
        return

    feishu = FeishuBitableClient.from_env()
    table_id = os.getenv("FEISHU_TABLE_ID", "")
    if not table_id:
        print("❌ .env 缺 FEISHU_TABLE_ID")
        return
    llm = LLMAdapter(MockProvider(canned="ok"))
    agent = XiaoCaiAgent(llm, Retriever([]), feishu=feishu, table_id=table_id)

    # ---- 3) 确认提交（写表 + 附件上传）----
    print(f"\n📤 [2/3] 确认提交（草稿：类型={rtype} 金额={draft['amount']} "
          f"号码={draft['invoice_no'] or '(空)'} 日期={draft.get('date', '(空)')}）...")
    r2 = agent.route("确认提交", base_draft=draft,
                     image_b64=image_b64, image_mime=mime)
    print((r2.get("answer") or "").replace("。\n", "。\n   "))
    rid = r2.get("record_id")
    if not rid:
        print("❌ 提交未成功（见上方回答）")
        return

    # ---- 4) 回读验证「发票附件」列 ----
    print(f"\n🔁 [3/4] 回读记录验证附件落库（record_id={rid}）...")
    try:
        rec = feishu.get_record(table_id, rid)
        attach = (rec.get("fields") or {}).get("发票附件")
        if attach:
            name = attach[0].get("name", "?") if isinstance(attach, list) else "?"
            print(f"   ✅ 发票附件已落库：{name}（{len(attach)} 个文件）")
        else:
            print("   ⚠️ 回读未见「发票附件」字段（可能上传降级，见上方 attach_note）")
        inv_info = (rec.get("fields") or {}).get("发票信息")
        print(f"   发票信息列：{inv_info or '(空)'}")
    except FeishuError as e:
        print(f"   ⚠️ 回读失败（不影响提交结果）：{e}")

    print(f"\n✅ 冒烟通过：单号={r2.get('bill_no') or '(AutoNumber 稍后可见)'} record_id={rid}")
    print("   请到飞书主表点开该行「发票附件」核对原图；「发票信息」列应有 号码；销售方。")


if __name__ == "__main__":
    main()
