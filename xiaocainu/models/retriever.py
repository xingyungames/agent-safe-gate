"""
M2 · 轻量 RAG 检索器（纯标准库实现，零额外依赖）
================================================
设计要点
--------
- 不引入 chromadb / faiss 等重依赖：比赛 KB 仅 4 份文档、现场演示量小，
  纯 Python 的 BM25 基线足够，且源码更干净、官方扫描风险更低、部署更稳。
- 检索 = 把文档切块(chunk) → 建 BM25 倒排 → query 时算得分取 top-k。
- 中文分词用「ASCII 词 + CJK 单字」的轻量方案（无需 jieba），对报销领域关键词足够。
- 本模块**完全离线、零依赖**：rerank（重排）与 compress（压缩）做成「可插拔回调」，
  默认 None/关闭，单测不联网、不依赖 M1 的 LLM；线上由 M4 注入 Agnes 能力。

本次增强（对照优化清单）
----------------------
① 标题切块：按 Markdown #/##/### 标题切分，整节不剁碎，标题关键词进 chunk。
② top-k + 相关度截断：只取前 k 个，并丢弃远低于最高分的噪音块。
③ 可插拔 rerank：BM25 捞候选池 → 注入的 reranker（如 Agnes）重排，弥补无 embedding。
④ category 来源过滤：chunk 记录 category（文件名派生），query 可按类路由（A 咨询 / B+C 填单）。
⑤ 可插拔 compress：拼 prompt 前用 compress(query,text) 抽取相关句，压 token、防爆上下文。

对外接口
--------
    from models.retriever import Retriever, Chunk, format_context
    ret = Retriever.from_directory("knowledge")          # 默认按标题切块
    hits = ret.query("差旅费报销需要哪些凭证", top_k=3)   # 含截断
    ctx = format_context(hits)                            # 拼成给 LLM 的上下文
"""
from __future__ import annotations

import os
import re
import math
from dataclasses import dataclass, field


@dataclass
class Chunk:
    """检索返回的最小单元。

    category : 来源类别（默认取自文件名，如 "报销制度"/"填单规则"），供 M4 分支路由。
    """

    text: str
    source: str = ""
    category: str = ""
    score: float = 0.0
    meta: dict = field(default_factory=dict)


class Retriever:
    """基于 Okapi BM25 的轻量检索器（纯标准库 + 可插拔重排/压缩）。"""

    def __init__(
        self,
        chunks: list | None = None,
        k1: float = 1.5,
        b: float = 0.75,
        reranker=None,
    ) -> None:
        self.k1 = k1
        self.b = b
        self.reranker = reranker  # 可选回调 reranker(query, chunks)->list[Chunk]
        self._docs: list[tuple[Chunk, list[str]]] = []  # (chunk, tokens)
        self._df: dict[str, int] = {}  # term -> 文档频次
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0
        if chunks:
            self.add(chunks)

    # ------------------------------------------------------------------ 分词
    @staticmethod
    def tokenize(text: str) -> list[str]:
        # ASCII 词（转小写）+ CJK 单字；对中文关键词检索足够
        raw = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", text or "")
        return [t.lower() for t in raw]

    # -------------------------------------------------------------- 建立索引
    def add(self, chunks: list) -> None:
        """chunks: list[Chunk] 或 list[(text, source)] 或 list[str]。"""
        parsed: list[tuple[Chunk, list[str]]] = []
        for c in chunks:
            if isinstance(c, Chunk):
                text, source, category = c.text, c.source, c.category
            elif isinstance(c, tuple) and len(c) == 2:
                text, source = c
                category = ""
            else:
                text, source = str(c), ""
                category = ""
            # category 为空时从文件名派生（去掉扩展名）
            if not category and source:
                category = os.path.splitext(os.path.basename(source))[0]
            toks = self.tokenize(text)
            parsed.append(
                (
                    Chunk(
                        text=text,
                        source=source,
                        category=category,
                        meta={"tokens": len(toks)},
                    ),
                    toks,
                )
            )

        for _, toks in parsed:
            for t in set(toks):
                self._df[t] = self._df.get(t, 0) + 1
        self._docs.extend(parsed)

        total = sum(len(toks) for _, toks in self._docs)
        self._avgdl = total / len(self._docs) if self._docs else 0.0
        n = len(self._docs)
        for t, df in self._df.items():
            # Okapi IDF（加 1 平滑，避免 0）
            self._idf[t] = math.log(1 + (n - df + 0.5) / (df + 0.5))

    # ----------------------------------------------------- 候选池 / category
    @staticmethod
    def _match_category(chunk: Chunk, categories) -> bool:
        """categories 为 str 或 list[str]，子串匹配（category 或 source 任一命中）。"""
        if not categories:
            return True
        if isinstance(categories, str):
            cats = [categories]
        else:
            cats = list(categories)
        hay = (chunk.category + " " + chunk.source).lower()
        return any(cat.strip().lower() in hay for cat in cats if cat.strip())

    # ------------------------------------------------------------------- 查询
    def query(
        self,
        question: str,
        top_k: int = 3,
        categories=None,
        min_score_ratio: float = 0.0,
        min_score: float = 0.0,
    ) -> list[Chunk]:
        """检索主入口。

        top_k            : 最终返回条数（默认 3，符合「lost in the middle」最优区间）。
        categories       : 来源类别过滤（str/list），用于 M4 分支路由；None 不过滤。
        min_score_ratio  : 截断阀（相对）。块得分 < 最高分×该比例 则丢弃；0 关闭。
        min_score        : 截断阀（绝对）。块得分 < 该值 则丢弃；0 关闭。
        """
        q_tokens = self.tokenize(question)
        if not q_tokens or not self._docs:
            return []

        # 1) category 过滤：先缩小候选集（分支路由）
        docs = [
            (c, t) for (c, t) in self._docs if self._match_category(c, categories)
        ]
        if not docs:
            return []

        # 2) BM25 打分并降序
        scored: list[tuple[float, Chunk]] = []
        for chunk, toks in docs:
            s = self._bm25(q_tokens, toks)
            chunk.score = round(s, 4)
            scored.append((s, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)

        # 3) 候选池：给 reranker 足够多的候选（BM25 便宜，多捞无妨）
        cand_k = max(top_k * 4, 12)
        pool: list[Chunk] = [c for _, c in scored[:cand_k]]

        # 4) 可插拔重排（默认 None → 跳过，保持离线）
        if self.reranker is not None:
            pool = list(self.reranker(question, pool))

        # 5) 相关度截断：丢弃远低于最高分的噪音块
        if pool:
            top_score = max(c.score for c in pool)
            floor = min_score
            if min_score_ratio > 0:
                floor = max(floor, top_score * min_score_ratio)
            kept = [c for c in pool if c.score >= floor]
        else:
            kept = []

        return kept[:top_k]

    def _bm25(self, q_tokens: list[str], doc_tokens: list[str]) -> float:
        doc_len = len(doc_tokens)
        freq: dict[str, int] = {}
        for t in doc_tokens:
            freq[t] = freq.get(t, 0) + 1
        score = 0.0
        denom_base = self.k1 * (1 - self.b + self.b * doc_len / (self._avgdl or 1))
        for qt in set(q_tokens):
            if qt not in self._idf:
                continue
            f = freq.get(qt, 0)
            if f == 0:
                continue
            idf = self._idf[qt]
            score += idf * (f * (self.k1 + 1)) / (f + denom_base)
        return score

    # ---------------------------------------------------------- 便捷构造方法
    @classmethod
    def from_directory(
        cls,
        directory: str,
        chunk_by: str = "heading",
        max_chars: int = 1200,
        overlap: int = 120,
        reranker=None,
        **kw,
    ) -> "Retriever":
        """从目录加载文档并切块建索引。

        chunk_by  : "heading" 按 Markdown 标题切（默认，整节不剁碎）；
                    "char"    回退到旧版字符切块（兼容老行为）。
        max_chars : 超大节的字符上限（heading 模式下，超长节再按字符切）。
        overlap   : 字符切块的重叠字符数。
        """
        return cls(
            load_documents(
                directory, chunk_by=chunk_by, max_chars=max_chars, overlap=overlap
            ),
            reranker=reranker,
            **kw,
        )


# ----------------------------------------------------------- 文档切块与加载
def split_markdown(text: str, max_chars: int = 1200, overlap: int = 120) -> list[str]:
    """按 Markdown 标题切块：每个 #/##/### 标题 + 其正文 = 一个 chunk。

    - 标题关键词（如「差旅费」）直接进 chunk，提升 BM25 命中。
    - 整节作为一个语义单元，不被硬字符切点剁碎。
    - 单节超过 max_chars 的超大段，回退到字符切块（split_text）。
    """
    text = (text or "").strip()
    if not text:
        return []
    lines = text.splitlines()
    sections: list[tuple[int, str, list[str]]] = []  # (level, heading, body_lines)
    cur_level = 0
    cur_heading = ""
    cur_body: list[str] = []
    for ln in lines:
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            if cur_heading or cur_body:
                sections.append((cur_level, cur_heading, cur_body))
            cur_level = len(m.group(1))
            cur_heading = m.group(2).strip()
            cur_body = []
        else:
            cur_body.append(ln)
    if cur_heading or cur_body:
        sections.append((cur_level, cur_heading, cur_body))

    chunks: list[str] = []
    for level, heading, body in sections:
        body_text = "\n".join(body).strip()
        if not body_text:  # 跳过无正文的纯标题节（如文档大标题）
            continue
        marker = "#" * level if level else ""
        block = (f"{marker} {heading}\n{body_text}" if heading else body_text).strip()
        if not block:
            continue
        if len(block) <= max_chars:
            chunks.append(block)
        else:
            # 超大节：在标题下再按字符切，保证不超长
            for piece in split_text(body_text, max_chars=max_chars, overlap=overlap):
                chunks.append(f"{marker} {heading}\n{piece}" if heading else piece)
    return chunks


def split_text(text: str, max_chars: int = 400, overlap: int = 80) -> list[str]:
    """把长文档切成 ~max_chars 的段落块（优先按空行分段，超长段硬切并留 overlap）。

    保留作为 split_markdown 的回退，以及「chunk_by="char"」的兼容入口。
    """
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        paras = [text]

    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 1 <= max_chars:
            buf = (buf + "\n" + p) if buf else p
        else:
            if buf:
                chunks.append(buf)
            if len(p) > max_chars:
                step = max(1, max_chars - overlap)
                for i in range(0, len(p), step):
                    chunks.append(p[i : i + max_chars])
                buf = ""
            else:
                buf = p
    if buf:
        chunks.append(buf)
    return chunks


def load_documents(
    directory: str,
    chunk_by: str = "heading",
    max_chars: int = 1200,
    overlap: int = 120,
) -> list[Chunk]:
    """读取目录下所有 .md / .txt，切块成 Chunk 列表。目录不存在则返回空。

    chunk_by  : "heading"（默认）按标题切；"char" 按字符切。
    category  : 自动取文件名（去扩展名），供 M4 分支路由。
    """
    chunks: list[Chunk] = []
    if not os.path.isdir(directory):
        return chunks
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith((".md", ".txt")):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        category = os.path.splitext(name)[0]
        if chunk_by == "char":
            pieces = split_text(content, max_chars=max_chars, overlap=overlap)
        else:
            pieces = split_markdown(content, max_chars=max_chars, overlap=overlap)
        for piece in pieces:
            chunks.append(Chunk(text=piece, source=name, category=category))
    return chunks


# --------------------------------------------------------- 给 M4 用的上下文
def format_context(
    chunks: list[Chunk],
    max_chars: int = 1500,
    compress=None,
    query: str = "",
) -> str:
    """把检索结果拼成可直接塞进 prompt 的上下文文本。

    compress : 可选回调 compress(query, text)->str，只保留与 query 相关的句子，
               用于压 token、防上下文爆炸；默认 None 关闭。
    query    : 传给 compress 的查询词（压缩相关句时需要）。
    """
    parts: list[str] = []
    total = 0
    for i, c in enumerate(chunks, 1):
        body = c.text
        if compress is not None:
            body = compress(query, body)
        if not body or not body.strip():  # 压缩后整块为空 → 跳过（纯噪音块）
            continue
        block = f"[资料{i}]({c.source})\n{body}"
        if total + len(block) > max_chars and parts:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)
