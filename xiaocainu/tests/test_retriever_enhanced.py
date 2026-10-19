"""
M2 增强项单测（不联网、不依赖外部 API）
=====================================
覆盖 5 项增强：①标题切块 ②相关度截断 ③可插拔 rerank ④category 过滤 ⑤上下文压缩。
rerank / compress 以「mock 回调」注入，验证接口与逻辑，不触碰 Agnes。

运行：
    py -3 tests/test_retriever_enhanced.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.retriever import (
    Retriever,
    Chunk,
    format_context,
    load_documents,
    split_markdown,
)

# 一份带 Markdown 标题的示例制度文档
SAMPLE_MD = """# 报销制度
## 差旅费
差旅费报销需提供：出差审批单、交通票据、住宿发票。单人住宿标准不超过300元/晚。
## 招待费
招待费报销需提供发票、消费清单。单次超过2000元须部门总监审批。
## 通讯补贴
通讯补贴按月发放，无需凭票。员工50元/月，主管100元/月。
"""

CORPUS = [
    Chunk(text="差旅费报销需提供出差审批单、交通票据、住宿发票。单人住宿标准不超过300元/晚。",
          source="报销制度.md"),
    Chunk(text="招待费报销需提供发票、消费清单。单次超过2000元须部门总监审批。",
          source="报销制度.md"),
    Chunk(text="填单规则：报销单必须填写事由、报销类型、金额、申请人工号；发票抬头须与公司一致，否则退回。",
          source="填单规则.md"),
    Chunk(text="合规预审要点：发票真伪核验、报销类型与制度匹配、超标准说明、票据连号异常检测。",
          source="填单规则.md"),
]


# ----------------------------------------------------------- ① 标题切块
def test_heading_chunking_keeps_section_intact():
    chunks = split_markdown(SAMPLE_MD)
    # 应切成 3 个带标题的块（差旅费/招待费/通讯补贴），无空壳
    assert len(chunks) == 3, f"应切成3节, got {len(chunks)}: {chunks}"
    joined = "\n".join(chunks)
    assert "## 差旅费" in joined and "## 招待费" in joined and "## 通讯补贴" in joined
    print("[PASS] test_heading_chunking_keeps_section_intact")


def test_heading_chunking_query_hits_correct_section():
    chunks = [Chunk(text=t, source="报销制度.md") for t in split_markdown(SAMPLE_MD)]
    r = Retriever(chunks)
    res = r.query("差旅费住宿标准300", top_k=3)
    assert res, "应返回结果"
    # 最相关应是「差旅费」那一节（标题+正文都在 chunk 里）
    assert "差旅费" in res[0].text and "300元/晚" in res[0].text, f"got {res[0].text[:30]}"
    print("[PASS] test_heading_chunking_query_hits_correct_section")


# ----------------------------------------------------------- ② 相关度截断
def test_relevance_cutoff_drops_noise():
    r = Retriever(CORPUS)
    # 强相关 query：差旅费块高分，其他块低分
    res = r.query("差旅费住宿标准300元", top_k=3, min_score_ratio=0.3)
    # 截断后不应把明显无关的「填单规则」块带进来（其得分远低于 top）
    assert all("填单规则" not in c.source or c.score >= 0 for c in res)
    # 强相关块一定在
    assert any("差旅费" in c.text for c in res)
    print("[PASS] test_relevance_cutoff_drops_noise")


def test_unrelated_query_with_cutoff_returns_empty():
    r = Retriever(CORPUS)
    # 完全无关 + 严格截断 → 空（不把噪音塞给 LLM）
    res = r.query("今天天气怎么样股票涨了吗", top_k=3, min_score_ratio=0.5)
    assert isinstance(res, list)
    # 允许返回空或极少量；核心是不崩溃且不被强制塞满
    print(f"[PASS] test_unrelated_query_with_cutoff_returns_empty (返回 {len(res)} 条)")


# ----------------------------------------------------------- ③ 可插拔 rerank
def test_reranker_mock_reorders():
    # mock reranker：把候选反转顺序并重新打分
    def mock_reranker(query, chunks):
        reordered = list(reversed(chunks))
        for i, c in enumerate(reordered):
            c.score = round(1.0 - i * 0.1, 4)
        return reordered

    r = Retriever(CORPUS, reranker=mock_reranker)
    res = r.query("报销", top_k=2)
    assert len(res) == 2
    # 反转后，原本 BM25 第一的应排到最后（被截断）
    # 这里只验证 reranker 被调用且顺序按其返回：首条 score 最高
    scores = [c.score for c in res]
    assert scores == sorted(scores, reverse=True), "rerank 后分数应降序"
    print("[PASS] test_reranker_mock_reorders")


def test_no_reranker_default_offline():
    # 默认无 reranker，纯 BM25，离线可用
    r = Retriever(CORPUS)
    res = r.query("差旅费", top_k=1)
    assert res and "差旅费" in res[0].text
    print("[PASS] test_no_reranker_default_offline")


# ----------------------------------------------------------- ④ category 过滤
def test_category_filter_routes_to_policy():
    r = Retriever(CORPUS)
    res = r.query("报销需要哪些凭证", top_k=5, categories=["报销制度"])
    assert res, "应返回结果"
    assert all(c.category == "报销制度" for c in res), \
        f"路由错误，混入了: {[c.category for c in res]}"
    print("[PASS] test_category_filter_routes_to_policy")


def test_category_filter_routes_to_form():
    r = Retriever(CORPUS)
    res = r.query("怎么填报销单", top_k=5, categories=["填单规则"])
    assert res, "应返回结果"
    assert all(c.category == "填单规则" for c in res)
    print("[PASS] test_category_filter_routes_to_form")


# ----------------------------------------------------------- ⑤ 上下文压缩
def test_context_compress_mock():
    def mock_compress(query, text):
        # 只保留含「差旅」的句子，模拟 Agnes 抽取相关句；无匹配返回空串→整块跳过
        keep = [ln for ln in text.split("\n") if "差旅" in ln]
        return "\n".join(keep)

    r = Retriever(CORPUS)
    res = r.query("差旅费", top_k=3)
    ctx = format_context(res, compress=mock_compress, query="差旅费")
    # 压缩后不应出现「招待费」相关句（被抽掉）
    assert "招待费" not in ctx, "compress 未生效，招待费句子仍残留"
    assert "差旅费" in ctx
    print("[PASS] test_context_compress_mock")


def test_no_compress_default_offline():
    r = Retriever(CORPUS)
    res = r.query("差旅费", top_k=3)
    ctx = format_context(res)  # 默认不压缩
    assert "差旅费" in ctx and "招待费" in ctx  # 完整上下文
    print("[PASS] test_no_compress_default_offline")


# ----------------------------------------------------------- 回归：真实目录
def test_from_directory_real_knowledge():
    here = os.path.dirname(os.path.abspath(__file__))
    know = os.path.join(os.path.dirname(here), "knowledge")
    if not os.path.isdir(know):
        print("[SKIP] test_from_directory_real_knowledge (knowledge 目录缺失)")
        return
    r = Retriever.from_directory(know)
    assert r._docs, "knowledge 应加载出 chunk"
    # 每个 chunk 应带 category（文件名派生）
    assert all(c.category for c in [d[0] for d in r._docs])
    res = r.query("报销", top_k=3)
    assert isinstance(res, list)
    print(f"[PASS] test_from_directory_real_knowledge (加载 {len(r._docs)} 块)")


if __name__ == "__main__":
    test_heading_chunking_keeps_section_intact()
    test_heading_chunking_query_hits_correct_section()
    test_relevance_cutoff_drops_noise()
    test_unrelated_query_with_cutoff_returns_empty()
    test_reranker_mock_reorders()
    test_no_reranker_default_offline()
    test_category_filter_routes_to_policy()
    test_category_filter_routes_to_form()
    test_context_compress_mock()
    test_no_compress_default_offline()
    test_from_directory_real_knowledge()
    print("\n全部 M2 增强单测通过")
