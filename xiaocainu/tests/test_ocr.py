#发票 OCR 引擎单测 全离线
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.ocr import (
    MockOcrEngine, InvoiceOCREngine, OCRError, OCRResult,
    extract_json, normalize_invoice_date, sanitize_fields, build_invoice_preview,
)


def test_mock_extract_full():
    #Mock 引擎全链路字段
    eng = MockOcrEngine()
    r = eng.extract(image_b64="aGVsbG8=", content_type="image/jpeg", filename="f.jpg")
    assert isinstance(r, OCRResult)
    assert r.fields["amount"] == 800.0
    assert r.fields["invoice_date"] == "2026/10/1"
    assert r.fields["rtype"] == "差旅费"
    assert r.fields["city"] == "北京"
    assert r.fields["invoice_no"].startswith("24312")
    assert r.invoice_info == "24312000000012345678；北京示例酒店有限公司"
    assert eng.calls and eng.calls[0]["filename"] == "f.jpg"


def test_preview_sentence():
    #预览句子含金额与类型
    eng = MockOcrEngine()
    r = eng.extract(image_b64="aGVsbG8=")
    s = build_invoice_preview(r.fields)
    assert "有发票" in s and "金额800元" in s and "2026/10/1" in s and "差旅费" in s


def test_extract_json_strips_fence():
    #剥掉代码围栏再解析
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('识别结果如下：{"amount": 5} 请核对') == {"amount": 5}


def test_extract_json_bad_raises():
    #坏输入一律抛 OCRError
    for bad in ("", "完全没有花括号", '{"broken": ', '[]'):
        try:
            extract_json(bad)
            assert False, f"应抛 OCRError: {bad!r}"
        except OCRError:
            pass


def test_sanitize_bogus_enum_cleared_with_warning():
    #非法枚举清空并给警告
    fields, warnings = sanitize_fields({
        "amount": "abc", "rtype": "通讯补贴", "city": "火星",
        "invoice_date": "2026年10月1日", "invoice_no": " N1 ", "seller": "某公司",
    })
    assert fields["amount"] is None and fields["rtype"] == "" and fields["city"] == ""
    assert fields["invoice_date"] == "2026/10/1"
    assert fields["invoice_no"] == "N1" and fields["seller"] == "某公司"
    assert len(warnings) == 3  #金额 类型 城市 三条


def test_sanitize_amount_bounds():
    #金额上下界校验
    _, w1 = sanitize_fields({"amount": 2e7})
    _, w2 = sanitize_fields({"amount": -1})
    _, w3 = sanitize_fields({"amount": 356.8})
    assert len(w1) == 1 and len(w2) == 1 and w3 == []


def test_normalize_invoice_date():
    #日期统一成斜杠格式
    assert normalize_invoice_date("2026年10月1日") == "2026/10/1"
    assert normalize_invoice_date("2026-10-01") == "2026/10/1"
    assert normalize_invoice_date("") == ""
    assert normalize_invoice_date("不是日期") == ""


class _Msg:
    #假消息对象
    def __init__(self, content): self.content = content


class _Choice:
    #假选择项
    def __init__(self, content):
        #包一条消息
        self.message = _Msg(content)
        self.finish_reason = "stop"


class _Usage:
    #假用量统计
    def model_dump(self): return {"prompt_tokens": 10, "completion_tokens": 5}


class _Completions:
    #假 completions 资源
    def __init__(self, content): self._content = content; self.kw = None

    def create(self, **kw):
        #记录入参并返回假响应
        self.kw = kw
        class _R:
            #假响应体
            choices = [_Choice(self._content)]
            usage = _Usage()
        return _R()


class _FakeClient:
    #假 OpenAI 客户端
    def __init__(self, content): self.chat = type("C", (), {})(); self.chat.completions = _Completions(content)


GOOD = ('```json\n{"amount": 1860.5, "invoice_date": "2026-10-01", '
        '"invoice_no": "INV1", "seller": "某酒店", "rtype": "差旅费", "city": "北京"}\n```')


def _engine(content: str) -> tuple[InvoiceOCREngine, _Completions]:
    #造一个带假 client 的引擎
    fake = _FakeClient(content)
    eng = InvoiceOCREngine(client=fake, model="deepseek-flash")
    return eng, fake.chat.completions


def test_engine_good_path_offline():
    #真实引擎正常路径
    eng, comp = _engine(GOOD)
    r = eng.extract(image_b64="aGVsbG8=")
    assert r.fields["amount"] == 1860.5 and r.fields["rtype"] == "差旅费"
    assert r.usage["completion_tokens"] == 5 and r.model == "deepseek-flash"
    #请求报文结构校验
    msgs = comp.kw["messages"]
    content = msgs[0]["content"]
    assert content[0]["type"] == "text" and "JSON" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert comp.kw["temperature"] == 0
    #thinking 必须关闭
    assert comp.kw["extra_body"] == {"thinking": {"type": "disabled"}}
    assert comp.kw["max_tokens"] == 600


def test_engine_empty_content_raises_with_detail():
    #空返回报错并带诊断
    eng, _ = _engine("")
    try:
        eng.extract(image_b64="aGVsbG8=")
        assert False, "应抛 OCRError"
    except OCRError as e:
        assert "返回为空" in str(e) and "finish_reason=stop" in str(e)


def test_engine_not_invoice_raises():
    #非发票图片报错 提示要告诉用户下一步传什么
    eng, _ = _engine('{"error": "not_invoice"}')
    try:
        eng.extract(image_b64="aGVsbG8=")
        assert False, "应抛 OCRError"
    except OCRError as e:
        assert "没识别出报销要素" in str(e)
        assert "支付凭证" in str(e)


def test_engine_illegal_base64_raises():
    #非法 base64 报错
    eng, _ = _engine(GOOD)
    try:
        eng.extract(image_b64="!!非法!!")
        assert False, "应抛 OCRError"
    except OCRError as e:
        assert "base64" in str(e)


def test_engine_engine_error_wrapped():
    #引擎异常被包装
    class _BoomClient:
        #调用即抛错的假 client
        class chat:
            #假 chat 资源
            class completions:
                #假 completions 资源
                @staticmethod
                def create(**kw): raise RuntimeError("network down")
    eng = InvoiceOCREngine(client=_BoomClient())
    try:
        eng.extract(image_b64="aGVsbG8=")
        assert False, "应抛 OCRError"
    except OCRError as e:
        assert "OCR 引擎调用失败" in str(e)


if __name__ == "__main__":
    #逐个执行本文件用例
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n全部通过：{len(fns)} 用例")
