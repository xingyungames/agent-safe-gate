"""
M7 · 安全模块（小财奴）
===================
三维安全架构（3D Security）的「边界防御」落地代码包：

  1. 输入净化   sanitize_text —— 剥离控制字符 / 空字节 / 超长截断，防脏数据入语义层
  2. 注入拦截   check_injection —— 精确 prompt-injection 签名拦截（防越权/套取系统提示）
  3. 频率节流   RateLimiter —— 按 session_id 滑动窗口限流，防刷/资源耗尽

设计原则
--------
- 纯 stdlib（无第三方依赖），贴合竞赛「源码可扫描 / 少依赖」硬约束。
- 与框架解耦：本模块不依赖 FastAPI，可被 dispatch() 直接调用，也便于离线单测。
- 防御纵深：与 M5 已在 pydantic 模型层做的「长度/类型/数值边界」约束互补，
  形成「契约层 → 语义层」双闸。
"""
from .guard import (
    sanitize_text,
    check_injection,
    INJECTION_SIGNATURES,
    RateLimiter,
)

__all__ = [
    "sanitize_text",
    "check_injection",
    "INJECTION_SIGNATURES",
    "RateLimiter",
]
