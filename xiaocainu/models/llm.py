"""
M1 · LLM 适配器
===============
统一封装本地 Ollama(qwen2.5:7b) 与云端 DeepSeek-V3，可配置切换。

设计要点
--------
- 两个 provider 都是 OpenAI 兼容接口，共用一套 client（base_url 不同）。
- 本模块是独立可单测单元：测试时注入 MockProvider，无需真实模型/付费 API。
- LLMAdapter.chat() 对外统一入口，负责组装消息、重试、归一化返回。

真实调用需要：pip install openai 并配置 .env（见 .env.example）。
"""
from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable

from .config import get_provider_config

logger = logging.getLogger("xiaocai.llm")


@dataclass
class LLMResult:
    """归一化的模型返回，供上层编排层统一消费。"""
    text: str
    model: str
    provider: str
    latency_ms: int
    usage: Dict = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(
        self, system: str, messages: List[Dict[str, str]], **kw
    ) -> LLMResult:
        ...


class OpenAICompatProvider:
    """OpenAI 兼容 provider：Ollama 本地 / DeepSeek 云端 通用。"""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 60,
    ) -> None:
        from openai import OpenAI  # 延迟导入：测试用 mock 时不依赖 openai

        self.name = name
        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    def complete(
        self,
        system: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 1024,
        **kw,
    ) -> LLMResult:
        chat = [{"role": "system", "content": system}] if system else []
        chat += messages
        t0 = time.time()
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=chat,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        latency = int((time.time() - t0) * 1000)
        usage: Dict = {}
        if resp.usage:
            try:
                usage = resp.usage.model_dump()
            except Exception:
                usage = {}
        return LLMResult(
            text=resp.choices[0].message.content or "",
            model=self.model,
            provider=self.name,
            latency_ms=latency,
            usage=usage,
        )


class MockProvider:
    """测试用假 provider：不联网、不花钱，用来验证编排/解析逻辑。"""

    name = "mock"

    def __init__(self, model: str = "mock-model", canned: str = "[MOCK] 这是模拟回复。") -> None:
        self.model = model
        self.canned = canned
        self.calls: List[Dict] = []

    def complete(
        self, system: str, messages: List[Dict[str, str]], **kw
    ) -> LLMResult:
        self.calls.append({"system": system, "messages": messages, "kw": kw})
        time.sleep(0.01)
        return LLMResult(
            text=self.canned,
            model=self.model,
            provider=self.name,
            latency_ms=10,
            usage={},
        )


class LLMAdapter:
    """
    对外统一入口。

    用法
    ----
        adapter = LLMAdapter.from_env()        # 生产：读 .env
        adapter = LLMAdapter(MockProvider())   # 测试：注入 mock
        r = adapter.chat("你是什么模型？", system="你是小财报销助手")
    """

    def __init__(self, provider: LLMProvider, retries: int = 2) -> None:
        self.provider = provider
        self.retries = retries

    @classmethod
    def from_env(cls) -> "LLMAdapter":
        # 配置统一收敛到 config.get_provider_config，比赛切 DeepSeek 只改 .env
        cfg = get_provider_config()
        p: LLMProvider = OpenAICompatProvider(
            name=cfg["name"],
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
        )
        return cls(p)

    def chat(
        self,
        user_prompt: str,
        system: str = "",
        history: Optional[List[Dict]] = None,
        max_retries: Optional[int] = None,
        **kw,
    ) -> LLMResult:
        messages = list(history or [])
        messages.append({"role": "user", "content": user_prompt})
        retries = self.retries if max_retries is None else max_retries
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                return self.provider.complete(system, messages, **kw)
            except Exception as e:
                last_err = e
                # 速率限制(429)：指数退避后重试，避免 rerank+compress 高频调用时雪崩
                if _is_rate_limit(e) and attempt < retries:
                    backoff = min(2 ** attempt, 30)
                    logger.warning(
                        "LLM 速率限制(429)，%ss 后重试(第%d次)", backoff, attempt
                    )
                    time.sleep(backoff)
                else:
                    logger.warning("LLM 调用失败(第%d次): %s", attempt, e)
        raise RuntimeError(f"LLM 调用在 {retries} 次重试后仍失败: {last_err}")


def _is_rate_limit(exc: Exception) -> bool:
    """粗略识别速率限制：覆盖 Agnes/OpenAI 的 429 与中文报错文案。"""
    s = str(exc)
    return (
        "429" in s
        or "RateLimit" in s
        or "rate limit" in s.lower()
        or "速率限制" in s
    )
