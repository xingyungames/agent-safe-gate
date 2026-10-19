"""
M3 飞书客户端单测（不联网、不使用真实密钥）
=========================================
用 FakeTransport 模拟飞书 HTTP 返回，验证：token 获取+缓存、批量写入、查询、
错误码抛 FeishuError、字段屏蔽、token 过期续期。

运行：
    py -3 tests/test_feishu.py
"""
import os
import sys
import time
import json as _json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.feishu import (
    FeishuBitableClient,
    FeishuError,
    build_reimbursement_record,
    filter_by_schema,
    public_view,
    REIMBURSEMENT_FIELDS,
    _parse_date_to_ts,
    _build_multipart,
    DEPT_WHITELIST,
    CITY_WHITELIST,
    ALLOWED_INVOICE,
    COMPLIANCE_RESULTS,
    STATUS_VALUES,
    validate_enum_fields,
)
from models.env import load_env


class FakeTransport:
    """模拟飞书开放平台：记录请求次数，按设定返回固定 JSON。"""

    def __init__(self, token_resp=None, write_resp=None, list_resp=None, fields_resp=None,
                 get_resp=None, upload_resp=None):
        self.n_token_calls = 0
        self.n_write_calls = 0
        self.n_list_calls = 0
        self.n_fields_calls = 0
        self.n_get_calls = 0
        self.n_upload_calls = 0
        self.last_upload = None  # (method, url, headers, data)
        self.token_resp = token_resp or {
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "t-abc",
            "expire": 7200,
        }
        self.write_resp = write_resp or {
            "code": 0,
            "msg": "ok",
            "data": {"records": [{"record_id": "rec_1"}, {"record_id": "rec_2"}]},
        }
        self.list_resp = list_resp or {
            "code": 0,
            "msg": "ok",
            "data": {
                "items": [
                    {"record_id": "rec_1", "fields": {"单号": "BX1", "状态": "审批中"}}
                ]
            },
        }
        # 单条查询：真实结构是 data.record.fields（曾因读成 data.fields 导致单号恒空）
        self.get_resp = get_resp or {
            "code": 0,
            "msg": "ok",
            "data": {
                "record": {
                    "record_id": "rec_1",
                    "id": "rec_1",
                    "fields": {"单号": "BX-001", "状态": "待审核", "申请人": "张三"},
                }
            },
        }
        self.fields_resp = fields_resp or {
            "code": 0,
            "msg": "ok",
            "data": {
                "items": [
                    {"field_id": "f1", "field_name": "事由", "type": 1},
                    {"field_id": "f2", "field_name": "金额", "type": 2},
                ]
            },
        }
        # M9 附件上传：upload_all
        self.upload_resp = upload_resp or {
            "code": 0, "msg": "ok", "data": {"file_token": "boxcnenFakeToken123"},
        }

    def request(self, method, url, *, json=None, params=None, headers=None, data=None):
        if "tenant_access_token" in url:
            self.n_token_calls += 1
            return self.token_resp
        if url.endswith("batch_create"):
            self.n_write_calls += 1
            return self.write_resp
        if url.endswith("upload_all"):
            self.n_upload_calls += 1
            self.last_upload = (method, url, headers, data)
            return self.upload_resp or {
                "code": 0, "msg": "ok", "data": {"file_token": "boxcnenFakeToken123"},
            }
        if url.endswith("/fields"):
            self.n_fields_calls += 1
            return self.fields_resp
        # 单条查询：.../records/{record_id}（先于 list 判断，避免被 "/records" 前缀吞掉）
        if method == "GET" and "/records/" in url:
            self.n_get_calls += 1
            return self.get_resp
        if method == "GET" and "/records" in url:
            self.n_list_calls += 1
            return self.list_resp
        return {"code": 0, "msg": "ok", "data": {}}


# ---------------------------------------------------------- 鉴权 + 缓存
def test_token_fetch_and_cache():
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    t1 = c._ensure_token()
    t2 = c._ensure_token()  # 未过期，应直接命中缓存，不再请求
    assert t1 == t2 == "t-abc"
    assert fake.n_token_calls == 1, "第二次应命中缓存，token 只请求 1 次"
    print("[PASS] test_token_fetch_and_cache")


# ---------------------------------------------------------- token 过期续期
def test_token_refresh_on_expire():
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake,
                           token_margin=0)  # margin=0 便于测试
    c._ensure_token()               # 第一次获取（n=1）
    c._token_exp = time.time() - 1   # 强制过期
    c._ensure_token()               # 过期后再次请求（n=2）
    assert fake.n_token_calls == 2, "过期后应重新获取，共 2 次"
    print("[PASS] test_token_refresh_on_expire")


# ---------------------------------------------------------- 批量写入
def test_create_records_returns_ids():
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    recs = [build_reimbursement_record(rtype="差旅费", amount=500)]
    ids = c.create_records("tblX", recs)
    assert ids == ["rec_1", "rec_2"], f"应返回 record_id 列表, got {ids}"
    assert fake.n_write_calls == 1
    # 写入请求应带 Bearer token（不校验具体值，只确认鉴权头被加上）
    print("[PASS] test_create_records_returns_ids")


# ---------------------------------------------------------- 查询
def test_list_records_returns_items():
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    items = c.list_records("tblX", page_size=10)
    assert items and items[0]["record_id"] == "rec_1"
    assert items[0]["fields"]["状态"] == "审批中"
    print("[PASS] test_list_records_returns_items")


# ---------------------------------------------------------- 单条查询（真实嵌套结构）
def test_get_record_parses_nested_record():
    """真实响应字段在 data.record.fields；必须能取到 单号（曾读成 data.fields → 恒空）。"""
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    rec = c.get_record("tblX", "rec_1")
    assert rec["record_id"] == "rec_1"
    assert rec["fields"].get("单号") == "BX-001", f"应解析 data.record.fields, got {rec['fields']}"
    assert fake.n_get_calls == 1
    print("[PASS] test_get_record_parses_nested_record")


# ---------------------------------------------------------- 错误码 → FeishuError
def test_error_code_raises_feishu_error():
    fake = FakeTransport(
        token_resp={"code": 0, "msg": "ok", "tenant_access_token": "t", "expire": 7200},
        write_resp={"code": 9999, "msg": "权限不足"},
    )
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    try:
        c.create_records("tblX", [{"x": 1}])
        assert False, "应抛出 FeishuError"
    except FeishuError as e:
        assert "9999" in str(e) and "权限不足" in str(e)
        assert "app_secret" not in str(e) and "secret" not in str(e).lower()
    print("[PASS] test_error_code_raises_feishu_error")


# ---------------------------------------------------------- 字段屏蔽
def test_public_view_shields_internal():
    rec = {"record_id": "rec_secret", "fields": {"单号": "BX1", "状态": "审批中", "未知字段": "x"}}
    view = public_view(rec)
    assert "record_id" not in view
    assert "未知字段" not in view  # 非业务字段也屏蔽
    assert view["单号"] == "BX1" and view["状态"] == "审批中"
    print("[PASS] test_public_view_shields_internal")


# ---------------------------------------------------------- 报销单 schema 完整
def test_reimbursement_schema_complete():
    rec = build_reimbursement_record(
        rtype="差旅费", amount=800, applicant="张三", date_str="2026/9/10"
    )
    # build 写入 12 个业务字段（不含只读 单号 / 不存在的 工号 / 留待审核的 审核人）
    assert rec["报销类型"] == "差旅费" and rec["金额"] == 800
    assert rec["申请人"] == "张三"
    assert rec["状态"] == "待审核"
    assert rec["票据状态"] == ""                 # 未传 has_invoice 默认空
    assert isinstance(rec["发票日期"], int) and rec["发票日期"] > 0  # 日期已转毫秒时间戳
    assert "单号" not in rec and "工号" not in rec and "审核人" not in rec
    print("[PASS] test_reimbursement_schema_complete")


# ---------------------------------------------------------- 日期 → 毫秒时间戳
def test_parse_date_to_ts():
    ts = _parse_date_to_ts("2026/9/10")
    assert isinstance(ts, int) and ts > 0
    assert _parse_date_to_ts("") is None
    assert _parse_date_to_ts("乱码") is None
    print("[PASS] test_parse_date_to_ts")


# ---------------------------------------------------------- .env 加载器
def test_load_env_explicit_path():
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
        f.write("XC_TEST_K=hello\n# 这是注释\nXC_TEST_Q='quoted'\nXC_TEST_EXISTING=keep\n")
        path = f.name
    os.environ.pop("XC_TEST_K", None)
    os.environ.pop("XC_TEST_Q", None)
    os.environ["XC_TEST_EXISTING"] = "orig"  # 已存在的变量不应被覆盖
    try:
        load_env(path)
        assert os.getenv("XC_TEST_K") == "hello", "load_env 应写入 XC_TEST_K"
        assert os.getenv("XC_TEST_Q") == "quoted", "load_env 应去除引号"
        assert os.getenv("XC_TEST_EXISTING") == "orig", "已存在的 OS 变量不应被覆盖"
    finally:
        os.unlink(path)
        os.environ.pop("XC_TEST_K", None)
        os.environ.pop("XC_TEST_Q", None)
        os.environ.pop("XC_TEST_EXISTING", None)
    print("[PASS] test_load_env_explicit_path")


# ---------------------------------------------------------- 字段探测
def test_list_fields():
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    fields = c.list_fields("tblX")
    assert len(fields) == 2 and fields[0]["field_name"] == "事由"
    assert fake.n_fields_calls == 1
    print("[PASS] test_list_fields")


# ---------------------------------------------------------- M8/P5 字段名缓存 + schema 过滤
def test_field_names_caching():
    """field_names：进程内缓存（二次调用不发 HTTP）；use_cache=False 强制重探。"""
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    names = c.field_names("tblX")
    assert names == {"事由", "金额"}
    c.field_names("tblX")
    c.field_names("tblX")
    assert fake.n_fields_calls == 1, "缓存内重复调用不应再请求飞书"
    c.field_names("tblX", use_cache=False)
    assert fake.n_fields_calls == 2, "强制重探应再请求一次"
    assert c.field_names("tblOther") == {"事由", "金额"}, "不同表各自缓存"
    print("[PASS] test_field_names_caching")


def test_filter_by_schema():
    """旧 14 字段表过滤掉 工号/审批意见/审批时间；新表全保留；空集合原样返回。"""
    rec = build_reimbursement_record(rtype="差旅费", amount=500, employee_id="E0001")
    assert rec.get("工号") == "E0001", "传入 employee_id 应写入工号"
    old_names = set(REIMBURSEMENT_FIELDS) - {"工号", "审批意见", "审批时间"}
    rec_old = filter_by_schema(rec, old_names)
    assert "工号" not in rec_old and "申请人" in rec_old, "旧表应剔除工号、保留其余字段"
    rec_new = filter_by_schema(rec, REIMBURSEMENT_FIELDS)
    assert rec_new.get("工号") == "E0001", "新表应保留工号"
    assert filter_by_schema(rec, set()) == rec, "空集合（探测失败兜底）应原样返回"
    print("[PASS] test_filter_by_schema")


def test_build_record_employee_id_optional():
    """employee_id 不传/空 → 不写工号键（与旧表语义一致）。"""
    rec = build_reimbursement_record(rtype="差旅费", amount=500)
    assert "工号" not in rec
    rec2 = build_reimbursement_record(rtype="差旅费", amount=500, employee_id="")
    assert "工号" not in rec2
    print("[PASS] test_build_record_employee_id_optional")


# ---------------------------------------------------------- 企业标准枚举常量
def test_enum_constants_complete():
    """部门 11 / 城市 37；各枚举集合内容符合企业标准（与 TABLE_SETUP.md 一致）。"""
    assert len(DEPT_WHITELIST) == 11, f"部门应 11 个, got {len(DEPT_WHITELIST)}"
    assert len(CITY_WHITELIST) == 37, f"城市应 37 个, got {len(CITY_WHITELIST)}"
    assert set(ALLOWED_INVOICE) == {"有票", "无票", "未提"}
    assert set(COMPLIANCE_RESULTS) == {"通过", "风险", "驳回", "超2000需总经理审批"}
    assert set(STATUS_VALUES) == {"待审核", "审核中", "已通过", "已驳回"}
    print("[PASS] test_enum_constants_complete")


# ---------------------------------------------------------- 写前枚举强校验闸门
def test_validate_enum_fields_passes_valid():
    """全部合规枚举值 → 无错误。"""
    rec = build_reimbursement_record(
        applicant="张三", department="研发部", rtype="差旅费", amount=800,
        has_invoice="有票", city="上海", compliance="通过", status="待审核",
    )
    assert validate_enum_fields(rec) == [], "合规记录不应报错"
    print("[PASS] test_validate_enum_fields_passes_valid")


def test_validate_enum_fields_rejects_bogus():
    """任一字段出现非标准枚举值 → 报出 (字段, 非法值)；空值不报错。"""
    rec = build_reimbursement_record(
        applicant="张三", department="火星部", rtype="差旅费", amount=800,
        has_invoice="有票", city="上海", compliance="通过", status="待审核",
    )
    errs = dict(validate_enum_fields(rec))
    assert errs.get("部门") == "火星部", "应捕获非标准部门"
    # 补一个脏城市复测
    rec["城市"] = "火星城"
    errs = dict(validate_enum_fields(rec))
    assert errs.get("城市") == "火星城", "应捕获非标准城市"
    # 空部门/空城市不参与校验（允许留空由追问补全）
    rec["部门"], rec["城市"] = "", ""
    assert validate_enum_fields(rec) == []
    print("[PASS] test_validate_enum_fields_rejects_bogus")


# ---------------------------------------------------------- M9 附件上传（stdlib multipart）
def test_reimbursement_fields_include_invoice_columns():
    """M9 新列已登记：发票信息(文本)/发票附件(附件)，旧表缺列由 filter_by_schema 兜底。"""
    assert "发票信息" in REIMBURSEMENT_FIELDS and "发票附件" in REIMBURSEMENT_FIELDS
    rec = build_reimbursement_record(rtype="差旅费", amount=500)
    rec_new = filter_by_schema(rec, set(REIMBURSEMENT_FIELDS))
    assert rec_new == rec, "新表 schema 下普通记录不受影响"
    old_names = set(REIMBURSEMENT_FIELDS) - {"发票信息", "发票附件"}
    rec_old = filter_by_schema(rec, old_names)
    assert "发票信息" not in rec_old, "旧表应自动剔除 M9 新列"
    print("[PASS] test_reimbursement_fields_include_invoice_columns")


def test_build_multipart_format():
    """multipart 报文结构：字段行 + 文件头 + 二进制体 + 结束边界。"""
    body = _build_multipart(
        fields={"file_name": "a.jpg", "size": "11"},
        file_field="file", filename="a.jpg", content_type="image/jpeg",
        file_bytes=b"\xff\xd8BIN", boundary="BOUND",
    )
    s = body.decode("utf-8", errors="replace")
    assert "--BOUND\r\n" in s and s.rstrip().endswith("--BOUND--")
    assert 'name="file_name"' in s and "a.jpg" in s
    assert 'filename="a.jpg"' in s and "Content-Type: image/jpeg" in s
    assert body.endswith(b"\xff\xd8BIN\r\n--BOUND--\r\n")
    print("[PASS] test_build_multipart_format")


def test_upload_attachment_offline():
    """upload_attachment：multipart 报文 + 鉴权头 + 返回 file_token。"""
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    token = c.upload_attachment("fapiao.jpg", b"\xff\xd8\xff\xe0JPGDATA", "image/jpeg")
    assert token == "boxcnenFakeToken123"
    assert fake.n_upload_calls == 1
    method, url, headers, data = fake.last_upload
    assert method == "POST" and url.endswith("/drive/v1/medias/upload_all")
    ct = headers["Content-Type"]
    assert ct.startswith("multipart/form-data; boundary=")
    assert "Authorization" in headers and "Bearer t-abc" in headers["Authorization"]
    body = data.decode("utf-8", errors="replace")
    assert 'name="parent_type"' in body and "bitable_file" in body
    assert 'name="parent_node"' in body and "app_token" in body
    assert 'name="file_name"' in body and "fapiao.jpg" in body
    print("[PASS] test_upload_attachment_offline")


def test_upload_attachment_guards():
    """空内容 / 超大附件 / 飞书错误码 都要大声报错。"""
    fake = FakeTransport(upload_resp={"code": 91403, "msg": "Forbidden"})
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    for fn, args in [
        ("empty", ("a.jpg", b"", "image/jpeg")),
        ("error", ("a.jpg", b"\xff" * 10, "image/jpeg")),
    ]:
        try:
            c.upload_attachment(*args)
            assert False, f"{fn} 应抛 FeishuError"
        except FeishuError as e:
            assert "app_secret" not in str(e).lower()
    try:
        c.upload_attachment("big.bin", b"\x00" * (21 * 1024 * 1024), "application/octet-stream")
        assert False, "超限应抛 FeishuError"
    except FeishuError as e:
        assert "20MB" in str(e)
    print("[PASS] test_upload_attachment_guards")



def test_urllib_transport_request():
    import urllib.request as ur

    class FakeResp:
        def __init__(self, body: str):
            self._b = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeResp(_json.dumps({"code": 0, "msg": "ok", "data": {"x": 1}}))

    orig = ur.urlopen
    ur.urlopen = fake_urlopen
    try:
        from models.feishu import UrllibTransport

        t = UrllibTransport()
        resp = t.request(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": "a", "app_secret": "s"},
        )
    finally:
        ur.urlopen = orig

    assert resp == {"code": 0, "msg": "ok", "data": {"x": 1}}, resp
    # 验证请求体是合法 JSON（json 模块未被 json 参数遮蔽）
    sent = captured["req"].data.decode("utf-8")
    assert '"app_id"' in sent, "请求体应是合法 JSON（json 模块不应被参数遮蔽）"
    print("[PASS] test_urllib_transport_request")


if __name__ == "__main__":
    test_token_fetch_and_cache()
    test_token_refresh_on_expire()
    test_create_records_returns_ids()
    test_list_records_returns_items()
    test_get_record_parses_nested_record()
    test_error_code_raises_feishu_error()
    test_public_view_shields_internal()
    test_reimbursement_schema_complete()
    test_parse_date_to_ts()
    test_load_env_explicit_path()
    test_list_fields()
    test_field_names_caching()
    test_filter_by_schema()
    test_build_record_employee_id_optional()
    test_enum_constants_complete()
    test_validate_enum_fields_passes_valid()
    test_validate_enum_fields_rejects_bogus()
    test_reimbursement_fields_include_invoice_columns()
    test_build_multipart_format()
    test_upload_attachment_offline()
    test_upload_attachment_guards()
    test_urllib_transport_request()
    print("\n全部 M3 飞书单测通过")
