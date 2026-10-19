"""
M2 单测（不联网、不依赖外部文件）
==============================
用内置 mini 语料验证检索器：相关性排序、top_k 限制、空查询、上下文拼接。

运行：
    py -3 tests/test_retriever.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.retriever import Retriever, Chunk, format_context


CORPUS = [
    Chunk(
        text="差旅费报销需提供：1) 出差审批单；2) 交通票据（火车票/机票）；3) 住宿发票；"
        "4) 差旅费报销单。单人住宿标准不超过300元/晚。",
        source="报销制度.md",
    ),
    Chunk(
        text="招待费报销需提供：发票、消费清单、招待事项说明及参与人员名单。"
        "单次招待费超过2000元须部门总监审批。",
        source="报销制度.md",
    ),
    Chunk(
        text="通讯补贴按月发放，无需凭票报销。标准为：员工50元/月，主管100元/月，经理150元/月。",
        source="小财奴.md",
    ),
    Chunk(
        text="填单规则：报销单必须填写事由、报销类型、金额、申请人工号；"
        "发票抬头须与公司名称一致，否则退回。",
        source="填单规则.md",
    ),
    Chunk(
        text="合规预审要点：发票真伪核验、报销类型与制度匹配、超标准说明、票据连号异常检测。"
        "预审不通过则标记为风险单。",
        source="填单规则.md",
    ),
]


def test_basic_retrieval():
    r = Retriever(CORPUS)
    res = r.query("差旅费报销需要哪些凭证", top_k=3)
    assert res, "应返回结果"
    # 最相关应是差旅费那条（含独特词：出差审批单 / 住宿标准）
    assert "出差审批单" in res[0].text, f"最相关应为差旅费块, got {res[0].text[:20]}"
    print("[PASS] test_basic_retrieval")


def test_top_k_limit():
    r = Retriever(CORPUS)
    res = r.query("报销", top_k=2)
    assert len(res) <= 2
    scores = [c.score for c in res]
    assert scores == sorted(scores, reverse=True), "分数应降序"
    print("[PASS] test_top_k_limit")


def test_empty_query():
    r = Retriever(CORPUS)
    assert r.query("") == []
    assert r.query("   ") == []
    print("[PASS] test_empty_query")


def test_unrelated_query_no_crash():
    r = Retriever(CORPUS)
    res = r.query("今天天气怎么样", top_k=1)
    assert isinstance(res, list)  # 无关查询不应崩溃，正常返回列表
    print("[PASS] test_unrelated_query_no_crash")


def test_format_context():
    r = Retriever(CORPUS)
    res = r.query("差旅费住宿标准", top_k=3)
    ctx = format_context(res)
    assert "资料" in ctx
    assert any("住宿" in c.text for c in res)
    print("[PASS] test_format_context")


if __name__ == "__main__":
    test_basic_retrieval()
    test_top_k_limit()
    test_empty_query()
    test_unrelated_query_no_crash()
    test_format_context()
    print("\n全部 M2 单测通过")
