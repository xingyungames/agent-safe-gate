"""
M5 · 会话存储（内存级，不落库）
==========================
设计要点
--------
- 支撑 /chat 多轮对话：把每轮的 user/assistant 消息存进 session，下一轮作为 history
  喂给 LLM（Agnes），实现连续 chat。
- 同时存 B/C 分支的「跨轮草稿 draft」：用户分多次补齐字段时，bot 追问→用户补信息→
  合并后一次性提交。
- **内存级、进程内、带 TTL 过期、不写数据库、不持久化**——遵循数据最小化原则：
  对话上下文仅驻留内存会话，降低敏感财务信息（姓名/金额/部门）泄露面。
- 不引入任何第三方依赖（std lib 实现），贴合项目「可扫描/少依赖」竞赛约束。
"""
from __future__ import annotations

import time


class SessionStore:
    """进程内会话存储：session_id -> {messages, draft, ts}。"""

    def __init__(self, ttl: int = 1800, max_turns: int = 20) -> None:
        self.ttl = ttl            # 会话无活动过期秒数（默认 30 分钟）
        self.max_turns = max_turns  # 保留的最大消息数（防无限增长）
        self._sessions: dict = {}

    # -------------------------------------------------- 内部
    def _expire(self) -> None:
        now = time.time()
        stale = [k for k, v in self._sessions.items() if now - v["ts"] > self.ttl]
        for k in stale:
            del self._sessions[k]

    def _touch(self, sid: str) -> dict:
        self._expire()
        s = self._sessions.get(sid)
        if s is None:
            s = {"messages": [], "draft": None, "ts": time.time()}
            self._sessions[sid] = s
        s["ts"] = time.time()
        return s

    # -------------------------------------------------- 消息（多轮 history）
    def get_messages(self, sid: str) -> list:
        return self._touch(sid)["messages"]

    def append(self, sid: str, role: str, content: str) -> None:
        s = self._touch(sid)
        s["messages"].append({"role": role, "content": content})
        if len(s["messages"]) > self.max_turns:
            s["messages"] = s["messages"][-self.max_turns:]

    # -------------------------------------------------- 草稿（B/C 跨轮补全）
    def get_draft(self, sid: str):
        return self._touch(sid).get("draft")

    def set_draft(self, sid: str, draft: dict) -> None:
        self._touch(sid)["draft"] = draft

    # -------------------------------------------------- 调试/状态
    def count(self) -> int:
        self._expire()
        return len(self._sessions)
