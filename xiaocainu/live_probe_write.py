"""
真实飞书探测 + 一次性全字段写入（临时脚本，测完可删）
=================================================
流程：
  1) 用 .env 凭证初始化 FeishuBitableClient（verbose 打印原始回传）
  2) list_fields 探测表结构（字段名/类型/单选选项）
  3) 按字段类型生成测试值，跳过只读/复杂字段（公式、关联、人员等）
  4) batch_create 一次性写入所有可写字段
  5) get_record 读回核验
注意：凭证只从 .env 读取；本脚本不写死任何密钥。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.feishu import FeishuBitableClient

TYPE_NAME = {
    1: "文本", 2: "数字", 3: "单选", 4: "多选", 5: "日期",
    7: "复选框", 11: "人员", 13: "电话", 15: "超链接",
    17: "位置", 20: "公式", 21: "双向关联", 22: " lookup(关联)",
    1001: "货币", 1002: "进度", 1003: "邮箱",
}
# 可写且能安全生成测试值的类型
HANDLED = {1, 2, 3, 4, 5, 7, 13, 15, 1001, 1002, 1003}
# 只读/复杂，跳过避免写入报错
SKIP = {11, 20, 21, 22}
SKIP_MARK = object()


def gen_value(f):
    t = f.get("type")
    name = f.get("field_name")
    prop = f.get("property") or {}
    if t in SKIP or t not in HANDLED:
        return SKIP_MARK
    if t == 1:
        return f"【真实写入测试】{name}"
    if t in (2, 1001):
        return 1.23
    if t == 1002:
        return 50
    if t == 3:  # 单选：飞书要求值为「选项名字符串」，不是 {"text":...}
        opts = prop.get("options") or []
        if not opts:
            return SKIP_MARK  # 无选项的单选字段无法安全写入，跳过
        return opts[0]["name"]
    if t == 4:  # 多选：值为选项名字符串列表
        opts = prop.get("options") or []
        if not opts:
            return SKIP_MARK
        return [opts[0]["name"]]
    if t == 5:  # 日期：毫秒时间戳
        return int(time.time() * 1000)
    if t == 7:
        return True
    if t == 13:
        return "13800000000"
    if t == 15:
        return {"text": "测试链接", "link": "https://example.com"}
    if t == 1003:
        return "test@example.com"
    return f"【真实写入测试】{name}"


def main():
    table_id = os.getenv("FEISHU_TABLE_ID", "")
    assert table_id, "缺少 FEISHU_TABLE_ID"
    client = FeishuBitableClient(
        os.getenv("FEISHU_APP_ID"),
        os.getenv("FEISHU_APP_SECRET"),
        os.getenv("FEISHU_APP_TOKEN"),
        verbose=True,
    )  # 已 load_env()

    print("===== 1) 探测字段 =====")
    fields = client.list_fields(table_id)
    print(f"共 {len(fields)} 个字段：")
    payload = {}
    for f in fields:
        t = f.get("type")
        nm = f.get("field_name")
        print(f"  - {nm}  type={t} ({TYPE_NAME.get(t, '未知')})")
        if t in (3, 4):
            opts = (f.get("property") or {}).get("options") or []
            print(f"      选项: {[o.get('name') for o in opts]}（共{len(opts)}）")
        v = gen_value(f)
        if v is SKIP_MARK:
            print(f"      ↳ 跳过（只读/复杂字段，不写入）")
            continue
        payload[nm] = v

    print(f"\n===== 2) 一次性写入 {len(payload)} 个可写字段 =====")
    ids = client.create_records(table_id, [payload])
    print("写入返回 record_id:", ids)

    print("\n===== 3) 读回核验 =====")
    rec = client.get_record(table_id, ids[0])
    print("读回字段：")
    for k, v in rec.get("fields", {}).items():
        print(f"  {k}: {v}")

    print(f"\n✅ 完成。record_id={ids[0]} 可在飞书表格中核对/删除。")


if __name__ == "__main__":
    main()
