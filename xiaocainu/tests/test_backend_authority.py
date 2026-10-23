#账号权威链路单测 主服务器为唯一身份源 纯离线
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api import auth_service, backend_client
from models.actor import PERM_INVOICE_READ_ALL, PERM_INVOICE_REVIEW
from mock_backend import FakeBackend, install, make_token, make_user
from security.guard import RateLimiter


#装配一个不限流的认证服务
def _svc():
    return auth_service.AuthService(login_limiter=RateLimiter(limit=100, window=60))


#装配一个连到离线替身的认证服务
def _svc_with_backend(fake):
    install(fake, backend_client)
    return _svc(), fake


def test_login_returns_csharp_token_and_perms():
    #登录直接拿到主服务器令牌与压平的权限键
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    code, payload = svc.login("finance", "Money#2026abc", "127.0.0.1")
    assert code == 200, payload
    assert payload["token"], payload
    assert payload["user"]["用户名"] == "finance", payload
    assert payload["user"]["角色"] == "财务部", payload
    assert payload["user"]["工号"] == "E0002", payload
    assert PERM_INVOICE_READ_ALL in payload["perms"], payload
    assert PERM_INVOICE_REVIEW in payload["perms"], payload
    print("[PASS] test_login_returns_csharp_token_and_perms")


def test_login_does_not_leak_sign_key():
    #签名密钥不下发给浏览器
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    code, payload = svc.login("employee", "Staff#2026abc", "127.0.0.1")
    assert code == 200, payload
    assert "signKey" not in payload, "登录响应不得包含签名密钥"
    assert "signKey" not in payload.get("user", {}), "user 段不得包含签名密钥"
    print("[PASS] test_login_does_not_leak_sign_key")


def test_login_rejected_is_401():
    #主服务器明确拒绝 原样回 401 不再回落任何本地表
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    code, payload = svc.login("employee", "wrong-password", "127.0.0.1")
    assert code == 401, payload
    assert "口令" in payload.get("error", ""), payload
    print("[PASS] test_login_rejected_is_401")


def test_login_unavailable_is_503():
    #主服务器不可用时是 503（基础设施故障，不是口令错误）
    fake = FakeBackend()
    fake.fail = "unavailable"
    svc, _ = _svc_with_backend(fake)
    code, payload = svc.login("employee", "Staff#2026abc", "127.0.0.1")
    assert code == 503, payload
    assert "主服务器" in payload.get("error", ""), payload
    print("[PASS] test_login_unavailable_is_503")


def test_login_wrong_password_returns_401_not_503():
    #回归：口令错必须是 401，不能被包装成「主服务器暂时不可用」
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    code, payload = svc.login("employee", "wrong-password", "127.0.0.1")
    assert code == 401, payload
    assert "暂时不可用" not in payload.get("error", ""), payload
    print("[PASS] test_login_wrong_password_returns_401_not_503")


def test_login_passes_client_ip_header():
    #真实 IP 要带给主服务器 供其按 (账号,IP) 做失败计数
    from mock_backend import restore
    restore(backend_client)          #拿回真函数（前面的用例把模块函数换成了替身）
    saved = backend_client._request
    captured = {}

    def fake_request(method, path, **kw):
        captured.clear()
        captured.update(kw)
        return 200, {"ok": True, "data": {"token": "t", "user": {"userName": "x"}}}

    try:
        backend_client._request = fake_request
        backend_client.login("u", "p", client_ip="1.2.3.4")
        assert captured["extra_headers"]["X-Client-Ip"] == "1.2.3.4", captured

        #逗号串/非法值不能带出去 否则可被用来伪造来源
        for bad in ("1.2.3.4,5.6.7.8", "not-an-ip", "  "):
            backend_client.login("u", "p", client_ip=bad)
            assert not captured.get("extra_headers"), (bad, captured)
    finally:
        backend_client._request = saved
        _svc_with_backend(FakeBackend())   #恢复替身 不影响后续用例
    print("[PASS] test_login_passes_client_ip_header")


def test_login_flow_records_client_ip():
    #端到端：登录时真实 IP 确实传到了主服务器适配层
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    svc.login("employee", "Staff#2026abc", "10.1.2.3")
    assert fake.login_ips and fake.login_ips[-1] == "10.1.2.3", fake.login_ips
    print("[PASS] test_login_flow_records_client_ip")


def test_role_codes_not_collapsed():
    #四个角色原样保留 superadmin 不再折叠成管理员
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    code, payload = svc.login("root", "Overlord#2026ak", "127.0.0.1")
    assert code == 200, payload
    assert payload["user"]["角色代码"] == "superadmin", payload
    assert payload["user"]["角色"] == "超级管理员", payload
    print("[PASS] test_role_codes_not_collapsed")


def test_authenticate_uses_cache():
    #第二次校验命中内存缓存 不再打主服务器
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    token = make_token("employee")
    a1 = svc.authenticate(token)
    a2 = svc.authenticate(token)
    assert a1 and a2, "应能识别有效令牌"
    assert a1.username == "employee" and a1.employee_id == "E0001"
    me_calls = [c for c in fake.calls if c[0] == "me"]
    assert len(me_calls) == 1, f"应只调用一次 /me，实际 {len(me_calls)}"
    assert a1.sign_key == "sk-test", "签名密钥应从令牌载荷取"
    print("[PASS] test_authenticate_uses_cache")


def test_authenticate_expired_locally_without_backend():
    #本地已能判定过期 不拿废令牌去打主服务器
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    expired = make_token("employee", ttl=-60)
    assert svc.authenticate(expired) is None
    assert not [c for c in fake.calls if c[0] == "me"], "过期令牌不应请求 /me"
    print("[PASS] test_authenticate_expired_locally_without_backend")


def test_authenticate_invalid_token_negative_cache():
    #主服务器判 401 后进入负缓存 不反复请求
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    fake.fail = "unauthorized"
    token = make_token("employee")
    assert svc.authenticate(token) is None
    assert svc.authenticate(token) is None
    me_calls = [c for c in fake.calls if c[0] == "me"]
    assert len(me_calls) == 1, f"负缓存应只请求一次，实际 {len(me_calls)}"
    print("[PASS] test_authenticate_invalid_token_negative_cache")


def test_authenticate_raises_when_backend_down():
    #主服务器故障要向上抛 不能伪装成未登录
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    fake.fail = "unavailable"
    try:
        svc.authenticate(make_token("employee"))
        raise AssertionError("主服务器不可用时应抛出 BackendError")
    except backend_client.BackendUnauthorized:
        raise AssertionError("不可用不能被当成令牌失效")
    except backend_client.BackendError:
        pass
    print("[PASS] test_authenticate_raises_when_backend_down")


def test_logout_clears_cache():
    #登出只清网关缓存 主服务器不做吊销
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    token = make_token("employee")
    assert svc.authenticate(token) is not None
    assert svc.logout(token) is True
    #清缓存后再验会重新问主服务器
    fake.fail = "unauthorized"
    assert svc.authenticate(token) is None
    print("[PASS] test_logout_clears_cache")


def test_error_status_mapping():
    #状态码到异常类型的映射
    cases = [
        (401, "", backend_client.BackendUnauthorized),
        (403, "IP_BANNED", backend_client.BackendUnavailable),
        (403, "FORBIDDEN", backend_client.BackendRejected),
        (429, "RATE_LIMITED", backend_client.BackendRejected),
        (400, "SUBMIT_REJECTED", backend_client.BackendRejected),
        (502, "FEISHU_UNAVAILABLE", backend_client.BackendUnavailable),
        (500, "INTERNAL", backend_client.BackendUnavailable),
    ]
    for status, code, expected in cases:
        try:
            backend_client._raise_for(status, {"ok": False, "code": code, "message": "x"})
            raise AssertionError(f"{status} 应当抛 {expected.__name__}")
        except expected:
            pass
    #429 要保留状态码 供网关原样回 429
    try:
        backend_client._raise_for(429, {"ok": False, "code": "RATE_LIMITED", "message": "x"})
    except backend_client.BackendRejected as e:
        assert e.status == 429, e.status
    print("[PASS] test_error_status_mapping")


def test_build_auth_requires_backend_url():
    #未配置主服务器地址时认证墙整体关闭（fail-closed）
    saved = backend_client.base_url, backend_client.enabled
    try:
        backend_client.base_url = lambda: ""
        backend_client.enabled = lambda: False
        assert auth_service.build_auth() is None
        backend_client.base_url = lambda: "http://127.0.0.1:8899"
        backend_client.enabled = lambda: True
        assert auth_service.build_auth() is not None
    finally:
        backend_client.base_url, backend_client.enabled = saved
    print("[PASS] test_build_auth_requires_backend_url")


def test_submit_uses_token_identity():
    #提交时身份由主服务器按令牌盖章 前端传的申请人无效
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    code, payload = svc.login("employee", "Staff#2026abc", "127.0.0.1")
    actor = svc.authenticate(payload["token"])
    assert actor is not None
    data = backend_client.submit_with_token(
        actor.token, actor.sign_key,
        department="研发部", rtype="差旅费", amount=500,
        reason="出差往返", has_invoice="有票", city="上海",
    )
    assert data["record_id"], data
    assert fake.submitted[-1]["applicant"] == "张三", fake.submitted[-1]
    assert fake.submitted[-1]["employeeId"] == "E0001", fake.submitted[-1]
    print("[PASS] test_submit_uses_token_identity")


def test_list_scope_by_role():
    #列表范围由主服务器按角色裁剪
    fake = FakeBackend()
    svc, _ = _svc_with_backend(fake)
    emp = svc.authenticate(make_token("employee"))
    fin = svc.authenticate(make_token("finance", role="finance"))
    assert backend_client.list_invoices(emp.token, emp.sign_key)["scope"] == "mine"
    assert backend_client.list_invoices(fin.token, fin.sign_key)["scope"] == "all"
    print("[PASS] test_list_scope_by_role")


if __name__ == "__main__":
    test_login_returns_csharp_token_and_perms()
    test_login_does_not_leak_sign_key()
    test_login_rejected_is_401()
    test_login_unavailable_is_503()
    test_login_wrong_password_returns_401_not_503()
    test_login_passes_client_ip_header()
    test_login_flow_records_client_ip()
    test_role_codes_not_collapsed()
    test_authenticate_uses_cache()
    test_authenticate_expired_locally_without_backend()
    test_authenticate_invalid_token_negative_cache()
    test_authenticate_raises_when_backend_down()
    test_logout_clears_cache()
    test_error_status_mapping()
    test_build_auth_requires_backend_url()
    test_submit_uses_token_identity()
    test_list_scope_by_role()
    print("\n账号权威链路单测全部通过")
