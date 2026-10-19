"""
M1 单测（不联网、不调用真实模型 / 付费 API）
=========================================
用 MockProvider 验证适配层逻辑：消息组装、system 注入、历史保留、重试机制、返回归一化。

运行：
    python tests/test_llm.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.llm import LLMAdapter, MockProvider, LLMResult


def test_mock_basic():
    mock = MockProvider()
    adapter = LLMAdapter(mock)
    r = adapter.chat("你好", system="你是小财报销助手")
    assert isinstance(r, LLMResult)
    assert r.provider == "mock"
    assert r.text.startswith("[MOCK]")
    # system 与 user 消息都被正确组装
    assert mock.calls[0]["system"] == "你是小财报销助手"
    assert mock.calls[0]["messages"][-1]["content"] == "你好"
    print("[PASS] test_mock_basic")


def test_retry_then_fail():
    class Flaky(MockProvider):
        def __init__(self):
            super().__init__()
            self.n = 0

        def complete(self, system, messages, **kw):
            self.n += 1
            raise RuntimeError("boom")

    flaky = Flaky()
    adapter = LLMAdapter(flaky, retries=2)
    raised = False
    try:
        adapter.chat("x")
    except RuntimeError:
        raised = True
    assert raised, "应当抛出 RuntimeError"
    assert flaky.n == 2, "应当重试 2 次"
    print("[PASS] test_retry_then_fail")


def test_history_preserved():
    mock = MockProvider()
    adapter = LLMAdapter(mock)
    adapter.chat("第一句", history=[{"role": "user", "content": "上一条"}])
    assert mock.calls[0]["messages"][0]["content"] == "上一条"
    print("[PASS] test_history_preserved")


def test_rate_limit_backoff():
    """429 速率限制时应指数退避重试，而非立即失败。"""
    import time as _time

    sleeps = []
    real_sleep = _time.sleep
    _time.sleep = lambda s: sleeps.append(s)  # 用记录代替真实等待，避免测试变慢

    class RateLimitedThenOK(MockProvider):
        def __init__(self):
            super().__init__()
            self.n = 0

        def complete(self, system, messages, **kw):
            self.n += 1
            if self.n <= 2:
                raise RuntimeError("Error code: 429 - 速率限制")
            return MockProvider.complete(self, system, messages, **kw)

    rl = RateLimitedThenOK()
    adapter = LLMAdapter(rl, retries=4)
    r = adapter.chat("x")
    _time.sleep = real_sleep  # 还原，避免影响其他测试
    assert r.text.startswith("[MOCK]"), "第三次应成功返回"
    assert rl.n == 3, f"前两次429退避+第三次成功, got {rl.n}"
    # 退避间隔应为 2s、4s（前两次 429；注意 MockProvider 内部还有一次 0.01s 不算退避）
    assert sleeps[:2] == [2, 4], f"退避间隔应为 [2,4], got {sleeps}"
    print("[PASS] test_rate_limit_backoff")


if __name__ == "__main__":
    test_mock_basic()
    test_retry_then_fail()
    test_history_preserved()
    test_rate_limit_backoff()
    print("\n全部 M1 单测通过")
