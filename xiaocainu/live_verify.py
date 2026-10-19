"""
小财奴 · 真实飞书表联调验证（只读探测 + 写示例数据）
=================================================
用途：把用户新建的两张飞书表与 docs/TABLE_SETUP.md 的枚举设计逐字比对，并写入示例数据验证端到端。
安全：探测为只读（list_fields）；写示例为极小批量（主表 2 条 + 用户表 3 条），且写入前打印将要写的值。
运行：py live_verify.py   （需真实凭证在 .env；飞书调用需关闭沙箱）
"""
from __future__ import annotations

import os
import sys
import time

# Windows 控制台 UTF-8 兼容（避免 emoji/中文编码崩溃）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure = sys.stdout.reconfigure  # noqa
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.env import load_env
from models.feishu import (
    FeishuBitableClient, FeishuError, build_reimbursement_record, filter_by_schema,
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
MAIN_TABLE = os.getenv("FEISHU_TABLE_ID", "")
USER_TABLE = os.getenv("FEISHU_USER_TABLE_ID", "")


# 期望的单选枚举（与 docs/TABLE_SETUP.md 逐字一致）
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


def probe_table(client, table_id, label):
    print(f"\n{'='*60}\n📋 探测 [{label}]  table_id={table_id}\n{'='*60}")
    try:
        fields = client.list_fields(table_id)
    except FeishuError as e:
        print(f"  ❌ 列字段失败：{e}")
        return None
    names = []
    print(f"  共 {len(fields)} 个字段：")
    problems = []
    ss_found = {}
    for f in fields:
        fname = f.get("field_name", "")
        ftype = f.get("type")
        names.append(fname)
        opts = []
        if ftype == 3:
            prop = f.get("property", {}) or {}
            opts = [o.get("name") for o in prop.get("options", [])]
            ss_found[fname] = set(opts)
        tname = TYPE_NAME.get(ftype, str(ftype))
        opt_str = f"  选项={opts}" if opts else ""
        print(f"   - {fname}  [{tname}]{opt_str}")
    # 比对单选枚举
    print("\n  —— 单选枚举比对 ——")
    for fname, want in EXPECTED_SS.items():
        if fname not in names:
            # 该表可能不含此字段（如用户表无城市）
            continue
        got = ss_found.get(fname)
        if got is None:
            problems.append(f"{fname} 不是单选类型")
            print(f"   ❌ {fname}：期望单选，实际非单选")
            continue
        missing = want - got
        extra = got - want
        if not missing and not extra:
            print(f"   ✅ {fname}：选项与设计并字一致")
        else:
            if missing:
                problems.append(f"{fname} 缺选项 {sorted(missing)}")
                print(f"   ❌ {fname}：缺选项 {sorted(missing)}")
            if extra:
                problems.append(f"{fname} 多出选项 {sorted(extra)}")
                print(f"   ⚠️  {fname}：多出选项 {sorted(extra)}（设计外，不影响写入）")
    verdict = "✅ 全部一致" if not problems else f"⚠️ 存在问题：{len(problems)} 项"
    print(f"\n  比对结论：{verdict}")
    return {"names": names, "ss_found": ss_found, "problems": problems}


def write_samples(client, main_table, user_table):
    print(f"\n{'='*60}\n📝 写入示例数据\n{'='*60}")

    # ---- 主表示例 1：走完整 agent 流水线（验证抽取→归一→枚举校验→写表）----
    print("\n[主表示例1] 通过 XiaoCaiAgent 完整链路提交一张差旅费单")
    llm = LLMAdapter(MockProvider(canned="ok"))
    ret = Retriever([])
    agent = XiaoCaiAgent(llm, ret, feishu=client, table_id=main_table)
    r1 = agent.route("我要报销 张三 研发部 差旅费 1600 有发票 北京 2026/10/1")
    print(f"   首轮 intent={r1['intent']}，是否进确认态：{'确认提交' in r1['answer']}")
    r2 = agent.route("确认提交", base_draft=r1.get("draft"))
    print(f"   提交结果：record_id={r2.get('record_id')}，单号回显={'BX' in (r2.get('answer') or '')}")
    if r2.get("record_id"):
        print(f"   答案片段：{(r2.get('answer') or '')[:120].replace(chr(10),' ')}")

    # ---- 主表示例 2：直接构造（招待费 2500 有票 → 合规结果=风险，演示枚举值）----
    print("\n[主表示例2] 直接构造一张招待费单（金额>2000 → 合规结果=风险）")
    rec2 = build_reimbursement_record(
        applicant="钱会计", department="财务部", rtype="招待费", amount=2500,
        reason="客户招待餐费", has_invoice="有票", city="上海", date_str="2026/10/2",
        compliance="风险", risk_summary="单次招待费超2000元，需总经理事前审批",
        status="待审核", employee_id="E0002",
    )
    rec2 = filter_by_schema(rec2, client.field_names(main_table))
    enum_errs = []  # 本地枚举闸门（与落库前一致）
    from models.feishu import validate_enum_fields
    enum_errs = validate_enum_fields(rec2)
    print(f"   写前枚举校验：{'通过' if not enum_errs else enum_errs}")
    try:
        ids2 = client.create_records(main_table, [rec2])
        print(f"   写入成功 record_id={ids2}")
    except FeishuError as e:
        print(f"   ❌ 写入失败：{e}")

    # ---- 用户表示例：3 个演示账号（密码哈希由脚本生成，明文不落表）----
    print("\n[用户表示例] 写入 员工/财务/管理员 各 1 个演示账号")
    demos = [
        ("E0001", "张三", "zhangsan", "研发部", "员工", "test123456"),
        ("E0002", "钱会计", "caiwu", "财务部", "财务", "test123456"),
        ("E0003", "管大爷", "admin", "行政部", "管理员", "test123456"),
    ]
    for emp_id, name, uname, dept, role, pw in demos:
        try:
            rec = make_user_record(username=uname, name=name, department=dept,
                                   role=role, password=pw, employee_id=emp_id)
            rec = filter_by_schema(rec, client.field_names(user_table))
            ids = client.create_records(user_table, [rec])
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
                  f"{f.get('报销类型','-')} | ¥{f.get('金额','-')} | {f.get('状态','-')} | "
                  f"合规={f.get('合规结果','-')}")
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
    print("🔧 小财奴 真实飞书表联调验证")
    if not (APP_ID and APP_SECRET and APP_TOKEN and MAIN_TABLE and USER_TABLE):
        print("❌ 缺少飞书凭证：请确认 .env 中 FEISHU_APP_ID/SECRET/TOKEN/TABLE_ID/USER_TABLE_ID")
        return
    try:
        client = FeishuBitableClient(APP_ID, APP_SECRET, APP_TOKEN)
        # 触发一次 token 换取以验证凭证与 base 可读
        client._ensure_token()
        print("✅ 飞书鉴权成功（app_token 可达）")
    except FeishuError as e:
        print(f"❌ 飞书鉴权/连通失败：{e}")
        return

    p_main = probe_table(client, MAIN_TABLE, "报销单主表V2")
    p_user = probe_table(client, USER_TABLE, "用户表")

    if p_main and not p_main["problems"]:
        try:
            write_samples(client, MAIN_TABLE, USER_TABLE)
            readback(client, MAIN_TABLE, USER_TABLE)
        except Exception as e:
            print(f"❌ 写示例/回读异常：{e}")
    else:
        print("\n⚠️ 主表枚举与设计并字不一致，已跳过写示例（先修正表结构或 docs 再写）。")


if __name__ == "__main__":
    main()
