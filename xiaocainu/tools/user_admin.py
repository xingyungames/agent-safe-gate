"""
M8 · 管理员账号管理 CLI（最小闭环）
==================================
密码哈希只能在脚本端生成写入（表里永远没有明文，也严禁手填哈希列）。
管理员通过命令行管账号，不建 Web 管理 UI（最小权限 + 凭据不落前端）。

用法（在 xiaocainu 项目根目录执行）：
    python tools/user_admin.py add    --username zhangsan --name 张三 --role 员工 [--eid E0001] [--password ***]
    python tools/user_admin.py reset  --username zhangsan [--password ***]
    python tools/user_admin.py disable --username zhangsan
    python tools/user_admin.py enable --username zhangsan
    python tools/user_admin.py list

依赖 .env：FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_APP_TOKEN / FEISHU_USER_TABLE_ID
"""
from __future__ import annotations

import argparse
import getpass
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.feishu import FeishuBitableClient, FeishuError  # noqa: E402  (import models 触发 .env 加载)
from models import users  # noqa: E402


def _build_store() -> users.UserStore:
    table_id = os.getenv("FEISHU_USER_TABLE_ID", "")
    if not (os.getenv("FEISHU_APP_ID") and table_id):
        print("❌ 缺少配置：请确认 .env 已填 FEISHU_APP_ID / FEISHU_APP_SECRET / "
              "FEISHU_APP_TOKEN / FEISHU_USER_TABLE_ID（见 docs/TABLE_SETUP.md）")
        sys.exit(2)
    client = FeishuBitableClient.from_env()
    return users.UserStore(client, table_id)


def _ask_password(args) -> str:
    pwd = args.password
    if not pwd:
        pwd = getpass.getpass("请输入密码：")
        pwd2 = getpass.getpass("再输一遍确认：")
        if pwd != pwd2:
            print("❌ 两次输入不一致")
            sys.exit(1)
    if not pwd:
        print("❌ 密码不能为空")
        sys.exit(1)
    return pwd


def cmd_add(args) -> None:
    store = _build_store()
    if store.get_by_username(args.username):
        print(f"❌ 用户名已存在：{args.username}")
        sys.exit(1)
    rec = users.make_user_record(
        username=args.username, name=args.name,
        role=args.role, password=_ask_password(args), employee_id=args.eid,
    )
    try:
        ids = store.client.create_records(store.table_id, [rec])
    except FeishuError as e:
        print(f"❌ 写入飞书失败：{e}")
        sys.exit(1)
    store.invalidate()
    print(f"✅ 账号已创建：{args.username}（{args.role}）")


def cmd_reset(args) -> None:
    store = _build_store()
    try:
        users.reset_password(store, args.username, _ask_password(args))
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except FeishuError as e:
        print(f"❌ 写入飞书失败：{e}")
        sys.exit(1)
    print(f"✅ 密码已重置：{args.username}")


def cmd_status(args) -> None:
    store = _build_store()
    try:
        name = users.set_user_status(store, args.username, enabled=args.action == "enable")
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except FeishuError as e:
        print(f"❌ 写入飞书失败：{e}")
        sys.exit(1)
    print(f"✅ 账号已{'启用' if args.action == 'enable' else '停用'}：{name}")


def cmd_list(args) -> None:
    store = _build_store()
    try:
        rows = store.list_users()
    except FeishuError as e:
        print(f"❌ 读取飞书失败：{e}")
        sys.exit(1)
    if not rows:
        print("（用户表为空）")
        return
    print(f"共 {len(rows)} 个账号：")
    for u in rows:
        print(f"  👤 {u.get('姓名', '-') or '-'}  用户名={u.get('用户名', '-')}"
              f"  角色={u.get('角色', '-') or '-'}  部门={u.get('部门', '-') or '-'}"
              f"  工号={u.get('工号', '-') or '-'}  状态={u.get('账号状态', '-') or '-'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="小财奴账号管理 CLI（仅管理员使用）")
    sub = parser.add_subparsers(dest="action", required=True)

    p_add = sub.add_parser("add", help="新增账号")
    p_add.add_argument("--username", required=True)
    p_add.add_argument("--name", default="", help="姓名（默认同用户名）")
    p_add.add_argument("--role", default="员工",
                       help=f"角色：{'/'.join(sorted(users.ALLOWED_ROLES))}（默认员工）")
    p_add.add_argument("--eid", default="", help="工号（选填）")
    p_add.add_argument("--password", default="", help="密码（不传则交互输入）")
    p_add.set_defaults(func=cmd_add)

    p_reset = sub.add_parser("reset", help="重置密码")
    p_reset.add_argument("--username", required=True)
    p_reset.add_argument("--password", default="", help="密码（不传则交互输入）")
    p_reset.set_defaults(func=cmd_reset)

    for act in ("disable", "enable"):
        p = sub.add_parser(act, help=f"{'停用' if act == 'disable' else '启用'}账号")
        p.add_argument("--username", required=True)
        p.set_defaults(func=cmd_status, action=act)

    p_list = sub.add_parser("list", help="列出全部账号（不含密码哈希）")
    p_list.set_defaults(func=cmd_list, action="list")

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
