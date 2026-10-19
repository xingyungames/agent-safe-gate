"""
小财奴 · 本地实时联调脚本（自测用，非比赛交付主体）
=================================================
用途
----
把你输入的话经 XiaoCaiAgent 路由，验证 B/C/D 分支能否真正写入/查询你的飞书多维表格。
所有凭证只从 .env 读取，本文件不写任何密钥。

前置
----
1) 复制 .env.example 为 .env，填入真实 FEISHU_APP_ID / FEISHU_APP_SECRET /
   FEISHU_APP_TOKEN / FEISHU_TABLE_ID（用你跑通案例里的值，别瞎猜）。
2) 飞书自建应用开通 bitable 权限，并把应用加为多维表格协作人。
3) 主表需有 13 字段（名称同 REIMBURSEMENT_FIELDS）。
4) LLM：默认读 LLM_PROVIDER；若未装 openai，A 分支降级为模拟回复，
   B/C/D 飞书功能不受影响（方便你先单独验证飞书收发）。

运行
----
    cd xiaocainu
    python run_live.py
输入示例
    > 我要报销差旅费5000元 工号A123      （B 写草稿 + C 预审回写）
    > 查一下工号A123的报销                （D 查进度）
    > 招待费报销超过多少需要审批          （A 咨询，需 openai）
    Ctrl+C 退出
"""
from __future__ import annotations

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    XiaoCaiAgent,
    LLMAdapter,
    MockProvider,
    FeishuBitableClient,
    Retriever,
)
from models.config import knowledge_dir


def build_agent() -> XiaoCaiAgent:
    # LLM：优先真实 provider；无 openai 时降级 mock（不影响 B/C/D 飞书）
    try:
        llm = LLMAdapter.from_env()
    except ImportError:
        print("[提示] 未安装 openai，A 分支将用模拟回复；B/C/D 飞书功能正常。")
        llm = LLMAdapter(MockProvider())

    retriever = Retriever.from_directory(knowledge_dir())

    feishu = None
    table_id = ""
    if os.getenv("FEISHU_APP_ID"):
        feishu = FeishuBitableClient.from_env()
        table_id = os.getenv("FEISHU_TABLE_ID", "")
        print(f"[飞书] 已接入：table_id={table_id or '(未设置)'}")
    else:
        print("[飞书] 未检测到 FEISHU_APP_ID，B/C/D 走降级（不真发）。")

    return XiaoCaiAgent(llm, retriever, feishu=feishu, table_id=table_id)


def main() -> None:
    agent = build_agent()
    print("\n===== 小财奴实时联调（输入问题，Ctrl+C 退出）=====")
    try:
        while True:
            try:
                text = input("\n你> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text in ("exit", "quit", "q"):
                break
            res = agent.route(text)
            print("小财> " + json.dumps(res, ensure_ascii=False, default=str))
    except KeyboardInterrupt:
        print("\n再见。")


if __name__ == "__main__":
    main()
