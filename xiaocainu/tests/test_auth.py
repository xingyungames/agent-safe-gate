"""M8 认证模块单测（纯离线，零网络零凭证）。运行：python tests/test_auth.py"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from security.auth import (
    PBKDF2_ITERATIONS,
    TokenRevoker,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)
from security.guard import RateLimiter

SECRET = "unit-test-secret-123"


# ---------------------------------------------------------- 密码哈希
def test_password_hash_random_salt():
    """同一密码两次哈希：盐随机不同、哈希不同，但各自都能校验通过。"""
    s1, h1 = hash_password("Passw0rd!")
    s2, h2 = hash_password("Passw0rd!")
    assert s1 != s2, "两次哈希的盐必须不同（防彩虹表）"
    assert h1 != h2, "盐不同则哈希必不同"
    assert verify_password("Passw0rd!", s1, h1), "正确密码应通过"
    assert verify_password("Passw0rd!", s2, h2), "第二组也应通过"
    print("[PASS] test_password_hash_random_salt")


def test_password_verify_rejects():
    """错误密码 / 哈希被篡改 / 非法 hex / 空值 → 一律 False 且不抛异常。"""
    s, h = hash_password("abc123")
    assert not verify_password("abc124", s, h), "错密码应拒绝"
    assert not verify_password("abc123", s, h[:-2] + "00"), "哈希被篡改应拒绝"
    assert not verify_password("abc123", "zz", h), "非法 hex 盐应拒绝（不抛异常）"
    assert not verify_password("abc123", s, ""), "空哈希应拒绝"
    assert not verify_password("", s, h), "空密码应拒绝"
    print("[PASS] test_password_verify_rejects")


# ---------------------------------------------------------- token
def test_token_roundtrip():
    """签发→校验还原 claims；iat/exp/jti 自动补齐。"""
    tok = issue_token({"username": "zhangsan", "role": "员工"}, SECRET, ttl=600)
    claims = verify_token(tok, SECRET)
    assert claims is not None, "合法 token 应通过校验"
    assert claims["username"] == "zhangsan"
    assert claims["role"] == "员工"
    assert isinstance(claims["jti"], str) and len(claims["jti"]) >= 8
    assert claims["exp"] > claims["iat"]
    print("[PASS] test_token_roundtrip")


def test_token_tamper_and_wrong_secret():
    """改 payload / 换密钥签名 / 缺点号 → 全部拒绝。"""
    tok = issue_token({"username": "zhangsan", "role": "员工"}, SECRET)
    body, sig = tok.rsplit(".", 1)
    # 篡改 payload（换角色提权）
    import base64, json

    def b64(d):
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    evil = b64(json.dumps({"username": "zhangsan", "role": "管理员", "exp": 9999999999},
                          ensure_ascii=False, separators=(",", ":")).encode())
    assert verify_token(f"{evil}.{sig}", SECRET) is None, "改 payload 后签名失效应拒绝"
    assert verify_token(tok, "another-secret") is None, "错误密钥应拒绝"
    assert verify_token("no-dot-token", SECRET) is None, "无签名段应拒绝"
    assert verify_token("", SECRET) is None, "空 token 应拒绝"
    print("[PASS] test_token_tamper_and_wrong_secret")


def test_token_expired():
    """过期 token 拒绝；ttl 非法值不崩溃。"""
    tok = issue_token({"username": "a"}, SECRET, ttl=-5)  # 已过期
    assert verify_token(tok, SECRET) is None, "过期 token 应拒绝"
    tok2 = issue_token({"username": "a"}, SECRET, ttl=600)
    assert verify_token(tok2, SECRET, now=9999999999) is None, "模拟时间前移后应过期"
    print("[PASS] test_token_expired")


def test_token_revoked():
    """logout（jti 拉黑）后 token 立即失效；未拉黑的正常。"""
    revoker = TokenRevoker()
    tok1 = issue_token({"username": "a"}, SECRET)
    tok2 = issue_token({"username": "b"}, SECRET)
    c1 = verify_token(tok1, SECRET, revoked=revoker)
    assert c1 is not None
    revoker.revoke(c1["jti"])
    assert verify_token(tok1, SECRET, revoked=revoker) is None, "已撤销 token 应拒绝"
    assert verify_token(tok2, SECRET, revoked=revoker) is not None, "其他 token 不受影响"
    # set 形式的 revoked 也应支持
    assert verify_token(tok1, SECRET, revoked={c1["jti"]}) is None, "set 黑名单同样生效"
    print("[PASS] test_token_revoked")


# ---------------------------------------------------------- 登录限流（复用 guard.RateLimiter）
def test_login_rate_limit():
    """登录限流：窗口内超过上限拒绝（按 用户名|IP 维度）。"""
    rl = RateLimiter(limit=3, window=60)
    key = "zhangsan|127.0.0.1"
    assert rl.allow(key) and rl.allow(key) and rl.allow(key), "前 3 次放行"
    assert not rl.allow(key), "第 4 次应拒绝"
    assert rl.allow("lisi|127.0.0.1"), "其他用户不受影响"
    print("[PASS] test_login_rate_limit")


def test_default_iterations_constant():
    """迭代次数保持 100k（降迭代=降安全，防止无意改掉）。"""
    assert PBKDF2_ITERATIONS >= 100_000
    print("[PASS] test_default_iterations_constant")


if __name__ == "__main__":
    test_password_hash_random_salt()
    test_password_verify_rejects()
    test_token_roundtrip()
    test_token_tamper_and_wrong_secret()
    test_token_expired()
    test_token_revoked()
    test_login_rate_limit()
    test_default_iterations_constant()
    print("\n全部 M8 认证模块单测通过")
