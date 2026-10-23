#飞书客户端离线单测 不联网
import os
import sys
import time
import json as _json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.feishu import (
    FeishuBitableClient,
    FeishuError,
    REIMBURSEMENT_FIELDS,
    _build_multipart,
    DEPT_WHITELIST,
    CITY_WHITELIST,
    ALLOWED_INVOICE,
    COMPLIANCE_RESULTS,
    STATUS_VALUES,
)
from models.env import load_env


class FakeTransport:
    #模拟飞书开放平台

    def __init__(self, token_resp=None, write_resp=None, list_resp=None, fields_resp=None,
                 get_resp=None, upload_resp=None):
        #初始化各类请求计数
        self.n_token_calls = 0
        self.n_write_calls = 0
        self.n_list_calls = 0
        self.n_fields_calls = 0
        self.n_get_calls = 0
        self.n_upload_calls = 0
        self.last_upload = None  #(方法 地址 头 数据)
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
        #单条查询字段在 record 内
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
        #附件上传走 upload_all
        self.upload_resp = upload_resp or {
            "code": 0, "msg": "ok", "data": {"file_token": "boxcnenFakeToken123"},
        }

    def request(self, method, url, *, json=None, params=None, headers=None, data=None):
        #按地址分派假响应
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
        #单条查询要先于列表判断
        if method == "GET" and "/records/" in url:
            self.n_get_calls += 1
            return self.get_resp
        if method == "GET" and "/records" in url:
            self.n_list_calls += 1
            return self.list_resp
        return {"code": 0, "msg": "ok", "data": {}}


def test_token_fetch_and_cache():
    #令牌缓存命中不重复请求
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    t1 = c._ensure_token()
    t2 = c._ensure_token()  #未过期直接命中缓存
    assert t1 == t2 == "t-abc"
    assert fake.n_token_calls == 1, "第二次应命中缓存，token 只请求 1 次"
    print("[PASS] test_token_fetch_and_cache")


def test_token_refresh_on_expire():
    #令牌过期自动刷新
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake,
                           token_margin=0)  #间隔设为 0 便于测试
    c._ensure_token()               #第一次获取
    c._token_exp = time.time() - 1   #强制过期
    c._ensure_token()               #过期后重新获取
    assert fake.n_token_calls == 2, "过期后应重新获取，共 2 次"
    print("[PASS] test_token_refresh_on_expire")


def test_create_records_returns_ids():
    #批量写记录返回 id 列表
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    recs = [{"报销类型": "差旅费", "金额": 500}]
    ids = c.create_records("tblX", recs)
    assert ids == ["rec_1", "rec_2"], f"应返回 record_id 列表, got {ids}"
    assert fake.n_write_calls == 1
    #写请求带上鉴权头
    print("[PASS] test_create_records_returns_ids")


def test_list_records_returns_items():
    #列表查询返回条目
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    items = c.list_records("tblX", page_size=10)
    assert items and items[0]["record_id"] == "rec_1"
    assert items[0]["fields"]["状态"] == "审批中"
    print("[PASS] test_list_records_returns_items")


def test_get_record_parses_nested_record():
    #单条查询解析嵌套字段
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    rec = c.get_record("tblX", "rec_1")
    assert rec["record_id"] == "rec_1"
    assert rec["fields"].get("单号") == "BX-001", f"应解析 data.record.fields, got {rec['fields']}"
    assert fake.n_get_calls == 1
    print("[PASS] test_get_record_parses_nested_record")


def test_error_code_raises_feishu_error():
    #错误码抛 FeishuError
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








def test_load_env_explicit_path():
    #按指定路径加载 env 文件
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
        f.write("XC_TEST_K=hello\n# 这是注释\nXC_TEST_Q='quoted'\nXC_TEST_EXISTING=keep\n")
        path = f.name
    os.environ.pop("XC_TEST_K", None)
    os.environ.pop("XC_TEST_Q", None)
    os.environ["XC_TEST_EXISTING"] = "orig"  #已存在的变量保留
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


def test_list_fields():
    #探测表字段列表
    fake = FakeTransport()
    c = FeishuBitableClient("app_id", "app_secret", "app_token", transport=fake)
    fields = c.list_fields("tblX")
    assert len(fields) == 2 and fields[0]["field_name"] == "事由"
    assert fake.n_fields_calls == 1
    print("[PASS] test_list_fields")


def test_field_names_caching():
    #字段名缓存与强制重探
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






def test_enum_constants_complete():
    #枚举常量数量与取值
    assert len(DEPT_WHITELIST) == 11, f"部门应 11 个, got {len(DEPT_WHITELIST)}"
    assert len(CITY_WHITELIST) == 37, f"城市应 37 个, got {len(CITY_WHITELIST)}"
    assert set(ALLOWED_INVOICE) == {"有票", "无票", "未提"}
    assert set(COMPLIANCE_RESULTS) == {"通过", "风险", "驳回", "超2000需总经理审批"}
    assert set(STATUS_VALUES) == {"待审核", "审核中", "已通过", "已驳回"}
    print("[PASS] test_enum_constants_complete")






def test_reimbursement_fields_include_invoice_columns():
    #发票新列已登记（字段适配改由主服务器负责，本层只保留字段清单）
    assert "发票信息" in REIMBURSEMENT_FIELDS and "发票附件" in REIMBURSEMENT_FIELDS
    print("[PASS] test_reimbursement_fields_include_invoice_columns")


def test_build_multipart_format():
    #multipart 报文结构
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
    #上传附件带鉴权头
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
    #空内容超限错误码都报错
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
    #真实 urllib 传输层请求
    import urllib.request as ur

    class FakeResp:
        #假 HTTP 响应对象
        def __init__(self, body: str):
            #保存响应体
            self._b = body.encode("utf-8")

        def __enter__(self):
            #进入上下文
            return self

        def __exit__(self, *a):
            #退出上下文
            return False

        def read(self):
            #读出响应体
            return self._b

    captured = {}

    def fake_urlopen(req, timeout=None):
        #替换 urlopen 抓取请求
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
    #请求体必须是合法 JSON
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
    test_load_env_explicit_path()
    test_list_fields()
    test_field_names_caching()
    test_enum_constants_complete()
    test_reimbursement_fields_include_invoice_columns()
    test_build_multipart_format()
    test_upload_attachment_offline()
    test_upload_attachment_guards()
    test_urllib_transport_request()
    print("\n全部 M3 飞书单测通过")
