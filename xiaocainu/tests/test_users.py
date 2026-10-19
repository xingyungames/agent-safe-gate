"""M8 用户表模块单测（纯离线，Fake 客户端零网络）。运行：python tests/test_users.py"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.feishu import FeishuError
from models import users
from security.auth import verify_password


class FakeUserClient:
    """内存版飞书客户端：list/create/update 三方法 + 调用记录。"""

    def __init__(self, seed_fields=None):
        self._next_id = 1
        self.records = []
        for f in seed_fields or []:
            self.records.append({"record_id": f"rec{self._next_id}", "fields": dict(f)})
            self._next_id += 1
        self.list_calls = 0
        self.updates = []
        self.created = []

    def list_records(self, table_id, *, page_size=20, filter_expr="", app_token=""):
        self.list_calls += 1
        return [{"record_id": r["record_id"], "fields": dict(r["fields"])}
                for r in self.records]

    def create_records(self, table_id, records, app_token=""):
        ids = []
        for f in records:
            rid = f"rec{self._next_id}"
            self._next_id += 1
            self.records.append({"record_id": rid, "fields": dict(f)})
            ids.append(rid)
        self.created.extend(records)
        return ids

    def update_record(self, table_id, record_id, fields, app_token=""):
        self.updates.append((record_id, dict(fields)))
        for r in self.records:
            if r["record_id"] == record_id:
                r["fields"].update(fields)
                return record_id
        raise FeishuError("record not found")


def _seed():
    """预置两个账号：zhangsan（员工/启用）、lisi（员工/停用）。"""
    return [
        users.make_user_record(username="zhangsan", name="张三",
                               role="员工", password="pw123", employee_id="E0001"),
        users.make_user_record(username="lisi", name="李四",
                               role="员工", password="pw456", status="停用"),
    ]


# ---------------------------------------------------------- 记录构造
def test_make_user_record_validation():
    try:
        users.make_user_record(username="x", name="x", role="超管",
                               password="pw")
        assert False, "非法角色应抛 ValueError"
    except ValueError:
        pass
    try:
        users.make_user_record(username="  ", name="x", role="员工",
                               password="pw")
        assert False, "空用户名应抛 ValueError"
    except ValueError:
        pass
    try:
        users.make_user_record(username="x", name="x", role="员工",
                               password="")
        assert False, "空密码应抛 ValueError"
    except ValueError:
        pass
    rec = users.make_user_record(username="zhangsan", name="张三",
                                 role="员工", password="pw123", employee_id="E0001")
    assert rec["用户名"] == "zhangsan" and rec["角色"] == "员工"
    assert rec["账号状态"] == "启用"
    assert rec["密码哈希"] != "pw123" and len(rec["盐"]) == 32
    assert verify_password("pw123", rec["盐"], rec["密码哈希"]), "生成的哈希应可校验"
    assert "pw123" not in str(rec), "明文密码不得出现在记录里"
    print("[PASS] test_make_user_record_validation")


# ---------------------------------------------------------- 登录校验
def test_authenticate_flow():
    store = users.UserStore(FakeUserClient(_seed()), "tblUsers")
    # 正确密码
    item, err = users.authenticate(store, "zhangsan", "pw123")
    assert item is not None and not err, "正确密码应通过"
    assert item["fields"]["姓名"] == "张三"
    # 错误密码
    _, err = users.authenticate(store, "zhangsan", "wrong!")
    assert err == "用户名或密码错误"
    # 不存在用户 → 与错密码同一句提示（防用户名枚举）
    _, err2 = users.authenticate(store, "nobody", "whatever")
    assert err2 == "用户名或密码错误" == err, "不存在与错密码提示必须一致"
    # 停用账号
    _, err3 = users.authenticate(store, "lisi", "pw456")
    assert "停用" in err3, "停用账号应有明确提示"
    # 用户名首尾空格容错
    item, _ = users.authenticate(store, "  zhangsan ", "pw123")
    assert item is not None, "用户名带空格应容错"
    print("[PASS] test_authenticate_flow")


# ---------------------------------------------------------- 缓存
def test_store_cache():
    client = FakeUserClient(_seed())
    store = users.UserStore(client, "tblUsers", cache_ttl=60)
    store.get_by_username("zhangsan")
    store.get_by_username("zhangsan")
    store.get_by_username("lisi")
    assert client.list_calls == 1, "TTL 内三次查询应只拉一次表（防刷飞书限流）"
    store.invalidate()
    store.get_by_username("zhangsan")
    assert client.list_calls == 2, "invalidate 后应重新拉取"
    print("[PASS] test_store_cache")


# ---------------------------------------------------------- 列表屏蔽凭据
def test_list_users_masks_hash():
    store = users.UserStore(FakeUserClient(_seed()), "tblUsers")
    rows = store.list_users()
    assert len(rows) == 2
    for u in rows:
        assert "密码哈希" not in u and "盐" not in u, "列表输出不得包含密码哈希/盐"
        assert "record_id" in u and u.get("用户名")
    print("[PASS] test_list_users_masks_hash")


# ---------------------------------------------------------- 重置密码 / 停用启用
def test_reset_and_disable():
    client = FakeUserClient(_seed())
    store = users.UserStore(client, "tblUsers", cache_ttl=0)  # 每次重读，贴近真实改表后状态
    users.reset_password(store, "zhangsan", "newpass!")
    item, err = users.authenticate(store, "zhangsan", "newpass!")
    assert item is not None and not err, "重置后新密码应可登录"
    _, err = users.authenticate(store, "zhangsan", "pw123")
    assert err, "旧密码应失效"
    assert client.updates and "密码哈希" in client.updates[0][1], "重置应走 update_record"
    # 停用后再启用
    users.set_user_status(store, "zhangsan", enabled=False)
    _, err = users.authenticate(store, "zhangsan", "newpass!")
    assert "停用" in err, "停用后应拒绝登录"
    users.set_user_status(store, "zhangsan", enabled=True)
    item, _ = users.authenticate(store, "zhangsan", "newpass!")
    assert item is not None, "重新启用后应可登录"
    # 不存在的用户
    try:
        users.reset_password(store, "nobody", "x")
        assert False, "重置不存在用户应抛 ValueError"
    except ValueError:
        pass
    print("[PASS] test_reset_and_disable")


# ---------------------------------------------------------- 登录回写
def test_touch_last_login():
    client = FakeUserClient(_seed())
    store = users.UserStore(client, "tblUsers", cache_ttl=0)
    store.touch_last_login("zhangsan")
    assert client.updates and "最后登录时间" in client.updates[0][1]
    item = store.get_by_username("zhangsan")
    assert isinstance(item["fields"].get("最后登录时间"), int), "应回写毫秒时间戳"
    # 不存在用户静默不抛
    store.touch_last_login("nobody")
    print("[PASS] test_touch_last_login")


if __name__ == "__main__":
    test_make_user_record_validation()
    test_authenticate_flow()
    test_store_cache()
    test_list_users_masks_hash()
    test_reset_and_disable()
    test_touch_last_login()
    print("\n全部 M8 用户表模块单测通过")
