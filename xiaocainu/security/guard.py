"""
M7 · 边界防御守卫（纯 stdlib）
===========================
提供三层入口防御，供 api/main.py 的 dispatch() 在真正调用 XiaoCaiAgent 之前调用：

  sanitize_text(text)        —— 净化：剥离控制字符/空字节、超长截断、去首尾空白
  check_injection(text)      —— 拦截：命中精确 prompt-injection 签名则返回 (True, 命中串)
  RateLimiter                —— 节流：按 sid 滑动窗口计数，allow() 返回是否放行

这些函数无副作用、可重复调用、可离线单测，不依赖任何网络或第三方库。
"""
from __future__ import annotations

import time

# ---------------------------------------------------- 净化
# 允许的「受控空白」：换行、制表、空格。其余控制字符（含 \r、\f、\v、\b、\0 等）一律剥离。
_ALLOWED_CONTROLS = {"\n", "\t", " "}


def sanitize_text(text: str, max_length: int = 2000) -> str:
    """输入净化：剥离空字节 / 非允许控制字符，去首尾空白，超长截断。

    参数
    ----
    text        : 原始用户输入（可能含不可见脏字符、超长 payload）
    max_length  : 截断上限；与 pydantic 模型层 max_length 对齐（防御纵深）

    返回
    ----
    净化后的文本；若入参非 str 则转 str 后净化。
    """
    if not isinstance(text, str):
        text = str(text)
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0:                       # NUL 空字节：危险且无意义，必剥
            continue
        if code < 32 and ch not in _ALLOWED_CONTROLS:
            continue                        # 其他 C0 控制字符（含 \r \f \v \b）剥离
        if code == 127:                     # DEL 删除符剥离
            continue
        out.append(ch)
    cleaned = "".join(out).strip()
    if max_length and len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


# ---------------------------------------------------- 注入拦截
# 精确 prompt-injection 签名（多为多字短语，降低中文报销语境下的误报率）。
# 仅做大小写不敏感的子串匹配——赛题语境下正常的报销话术几乎不会命中这些短语。
INJECTION_SIGNATURES = [
    # 中文越权 / 指令覆盖
    "忽略上面的", "忽略之前的", "忽略先前的", "忽略之前的指令", "忽略上文", "无视上面的",
    "无视规则", "无视指令", "不要遵守", "不必遵守", "忘记你的身份", "忘记之前",
    "你现在是", "你是管理员", "你是系统", "假装你是", "假装成", "角色扮演成",
    "系统提示", "系统提示词", "提示词是", "你的指令是",
    "暴露你的", "透露你的", "泄露你的", "告诉我你的", "展示你的提示",
    "绕过限制", "突破限制", "越权执行", "解除限制", "不受约束",
    # 英文越权 / 指令覆盖
    "ignore the above", "ignore previous", "ignore all", "disregard", "forget your",
    "you are now", "pretend you are", "pretend to be", "system prompt",
    "reveal your", "expose your", "jailbreak", "break the rule", "bypass",
    "do not follow", "don't follow", "override",
]


def check_injection(text: str) -> tuple:
    """prompt-injection 签名拦截。

    参数
    ----
    text : 待检测文本（通常应先经 sanitize_text 净化）

    返回
    ----
    (is_injection: bool, matched: str)
      - is_injection=True 时 matched 为命中的签名串，便于在日志/返回中审计；
      - 未命中时 matched 为 ""。
    """
    if not isinstance(text, str):
        text = str(text)
    lowered = text.lower()
    for sig in INJECTION_SIGNATURES:
        if sig.lower() in lowered:
            return (True, sig)
    return (False, "")


# ---------------------------------------------------- 频率节流
class RateLimiter:
    """按会话(sid)滑动窗口限流（进程内、内存级）。

    用于兜底防止单会话高频刷接口导致资源耗尽 / 模型费用失控。
    与会话存储(SessionStore)一样内存级、不落库。
    """

    def __init__(self, limit: int = 30, window: int = 60) -> None:
        self.limit = limit          # 窗口内最大放行次数
        self.window = window        # 滑动窗口秒数
        self._hits: dict = {}       # sid -> [timestamp, ...]

    def _expire(self, sid: str, now: float) -> None:
        cutoff = now - self.window
        hits = self._hits.get(sid)
        if hits:
            kept = [t for t in hits if t > cutoff]
            if kept:
                self._hits[sid] = kept
            else:
                self._hits.pop(sid, None)

    def allow(self, sid: str, now: float | None = None) -> bool:
        """判断 sid 本次请求是否放行。放行则记录一次命中。

        参数
        ----
        sid : 会话标识（来自请求 session_id，缺省用 "default"）
        now : 可注入当前时间戳（便于离线单测）

        返回
        ----
        True=放行 / False=触发限流（调用方应返回 429）
        """
        now = now if now is not None else time.time()
        self._expire(sid, now)
        hits = self._hits.setdefault(sid, [])
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def count(self, sid: str, now: float | None = None) -> int:
        """当前窗口内 sid 已命中次数（调试/状态用）。"""
        now = now if now is not None else time.time()
        self._expire(sid, now)
        return len(self._hits.get(sid, []))

    def would_allow(self, sid: str, now: float | None = None) -> bool:
        """只读判断：窗口内是否还能放行，但不记录命中。

        用于「仅失败才计数」的登录限流（M8）：先 would_allow 预检，
        验证失败后再 allow 记一次；正常登录不消耗配额。
        """
        now = now if now is not None else time.time()
        self._expire(sid, now)
        return len(self._hits.get(sid, [])) < self.limit
