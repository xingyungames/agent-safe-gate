"""
小财奴 · 新表联调探针（自动识别主表/用户表 + 探测 + 试写 + 回读）
================================================================
用法：py probe_new.py   （需真实凭证在 .env；飞书调用需关闭沙箱）
特点：先探测两张表的字段，按「报销类型」字段自动判定谁是主表谁是用户表，
      避免 URL 顺序搞反导致写错表。写前过枚举闸门；写入极小批量。
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.env import load_env
from models.feishu import (
    FeishuBitableClient, FeishuError, build_reimbursement_record,
    filter_by_schema, validate_enum_fields,
    DEPT_WHITELIST, CITY_WHITELIST,
)
from models.users import make_user_record
from models.retriever import Retriever
from models.llm import LLMAdapter, MockProvider
from models.agent import XiaoCaiAgent

load_env()

APP_ID = os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "")
T1 = os.getenv("FEISHU_TABLE_ID", "")        # 第一个 URL 的表
T2 = os.getenv("FEISHU_USER_TABLE_ID", "")   # 第二个 URL 的表

EXPECTED_SS = {
    "报销类型": {"差旅费", "招待费", "办公用品", "交通费"},
    "票据状态": {"有票", "无票", "未提"},
    "合规结果": {"通过", "风险", "驳回", "超2000需总经理审批"},
    "状态": {"待审核", "审核中", "已通过", "已驳回"},
    "部门": set(DEPT_WHITELIST),
    "城市": set(CITY_WHITELIST),
    "角色": {"员工", "财务", "管理员"},
    "账号状态": {"启用", "停用"},
}
TYPE_NAME = {1: "文本", 2: "数字", 3: "单选", 5: "日期", 1005: "自动编号"}


def probe(client, table_id):
    try:
        fields = client.list_fields(table_id)
    except FeishuError as e:
        return None, f"列字段失败：{e}"
    names = []
    ss_found = {}
    for f in fields:
        fname = f.get("field_name", "")
        ftype = f.get("type")
        names.append(fname)
        if ftype == 3:
            prop = f.get("property", {}) or {}
            opts = [o.get("name") for o in prop.get("options", [])]
            ss_found[fname] = set(opts)
    return (names, ss_found), None


def compare(names, ss_found, label):
    problems = []
    print(f"\n  —— [{label}] 单选枚举比对 ——")
    for fname, want in EXPECTED_SS.items():
        if fname not in names:
            continue
        got = ss_found.get(fname)
        if got is None:
            problems.append(f"{fname} 不是单选")
            print(f"   ❌ {fname}：期望单选，实际非单选")
            continue
        missing = want - got
        extra = got - want
        if not missing and not extra:
            print(f"   ✅ {fname}：与选项逐字一致")
        else:
            if missing:
                problems.append(f"{fname} 缺 {sorted(missing)}")
                print(f"   ❌ {fname}：缺 {sorted(missing)}")
            if extra:
                problems.append(f"{fname} 多 {sorted(extra)}")
                print(f"   ⚠️  {fname}：多 {sorted(extra)}（设计外，不影响写）")
    return problems


def write_samples(client, main_table, user_table):
    print(f"\n{'='*60}\n📝 试写示例数据\n{'='*60}")

    # 主表示例：招待费 2500 → 合规结果=风险（直接构造，命中枚举闸门）
    print("\n[主表] 写一张招待费单（金额>2000 → 合规结果=风险）")
    rec = build_reimbursement_record(
        applicant="钱会计", department="财务部", rtype="招待费", amount=2500,
        reason="客户招待餐费", has_invoice="有票", city="上海", date_str="2026/10/2",
        compliance="风险", risk_summary="单次招待费超2000元，需总经理事前审批",
        status="待审核", employee_id="E0002",
    )
    rec = filter_by_schema(rec, client.field_names(main_table))
    errs = validate_enum_fields(rec)
    print(f"   写前枚举校验：{'通过' if not errs else errs}")
    try:
        ids = client.create_records(main_table, [rec])
        print(f"   ✅ 主表写入成功 record_id={ids}")
    except FeishuError as e:
        print(f"   ❌ 主表写入失败：{e}")

    # 用户表：3 个演示账号
    print("\n[用户表] 写 员工/财务/管理员 各 1 个演示账号")
    demos = [
        ("E0001", "张三", "zhangsan", "研发部", "员工", "test123456"),
        ("E0002", "钱会计", "caiwu", "财务部", "财务", "test123456"),
        ("E0003", "管大爷", "admin", "行政部", "管理员", "test123456"),
    ]
    for emp_id, name, uname, dept, role, pw in demos:
        try:
            r = make_user_record(username=uname, name=name, department=dept,
                                 role=role, password=pw, employee_id=emp_id)
            r = filter_by_schema(r, client.field_names(user_table))
            ids = client.create_records(user_table, [r])
            print(f"   ✅ {role} {name}({uname}) 写入成功 record_id={ids}")
        except FeishuError as e:
            print(f"   ❌ {role} {name} 写入失败：{e}")
        except ValueError as e:
            print(f"   ❌ {role} {name} 参数错误：{e}")


def readback(client, main_table, user_table):
    print(f"\n{'='*60}\n🔍 回读确认\n{'='*60}")
    try:
        mains = client.list_records(main_table, page_size=10)
        print(f"\n主表记录数（最近 {len(mains)} 条）：")
        for m in mains[:6]:
            f = m.get("fields", {})
            print(f"   {f.get('单号','-')} | {f.get('申请人','-')} | {f.get('部门','-')} | "
                  f"{f.get('报销类型','-')} | ¥{f.get('金额','-')} | {f.get('状态','-')} | 合规={f.get('合规结果','-')}")
    except FeishuError as e:
        print(f"   ❌ 主表查询失败：{e}")
    try:
        users = client.list_records(user_table, page_size=10)
        print(f"\n用户表记录数（最近 {len(users)} 条）：")
        for u in users[:6]:
            f = u.get("fields", {})
            print(f"   {f.get('工号','-')} | {f.get('姓名','-')} | {f.get('用户名','-')} | "
                  f"{f.get('部门','-')} | {f.get('角色','-')} | {f.get('账号状态','-')} "
                  f"（密码哈希长度={len(str(f.get('密码哈希','')))}）")
    except FeishuError as e:
        print(f"   ❌ 用户表查询失败：{e}")


def main():
    print("🔧 小财奴 新表联调探针")
    if not (APP_ID and APP_SECRET and APP_TOKEN and T1 and T2):
        print("❌ 缺少飞书凭证（FEISHU_APP_ID/SECRET/TOKEN/TABLE_ID/USER_TABLE_ID）")
        return
    try:
        client = FeishuBitableClient(APP_ID, APP_SECRET, APP_TOKEN)
        client._ensure_token()
        print("✅ 飞书鉴权成功（base 可达）")
    except FeishuError as e:
        print(f"❌ 鉴权/连通失败：{e}")
        return

    print(f"\n>>> 探测两张表（自动识别主表/用户表）")
    (n1, s1), e1 = probe(client, T1)
    (n2, s2), e2 = probe(client, T2)
    if e1 or e2 or n1 is None or n2 is None:
        print(f"   T1({T1}) 错误：{e1}")
        print(f"   T2({T2}) 错误：{e2}")
        return

    is_main1 = "报销类型" in n1
    main_table = T1 if is_main1 else T2
    user_table = T2 if is_main1 else T1
    print(f"   表1({T1}) 字段数={len(n1)}，含『报销类型』={is_main1} → 判为{'主表' if is_main1 else '用户表'}")
    print(f"   表2({T2}) 字段数={len(n2)} → 判为{'用户表' if is_main1 else '主表'}")

    print(f"\n{'='*60}\n📋 探测 [主表] {main_table}\n{'='*60}")
    for f in (n1 if is_main1 else n2):
        pass
    # 打印主表字段
    mn, ms = (n1, s1) if is_main1 else (n2, s2)
    print(f"  字段：{mn}")
    pm = compare(mn, ms, "主表")
    print(f"\n{'='*60}\n📋 探测 [用户表] {user_table}\n{'='*60}")
    un, us = (n2, s2) if is_main1 else (n1, s1)
    print(f"  字段：{un}")
    pu = compare(un, us, "用户表")

    if not pm and not pu:
        write_samples(client, main_table, user_table)
        readback(client, main_table, user_table)
    else:
        print("\n⚠️ 存在枚举不一致，已跳过写入（先对齐表结构）。")


if __name__ == "__main__":
    main()
