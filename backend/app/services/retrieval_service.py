
"""
NetResearch AI - 混合检索服务

功能：
1. 从 SQLite 读取论文文本块
2. jieba 中英文混合分词
3. BM25 关键词检索
4. BGE-M3 语义向量检索
5. RRF 融合两路检索结果
6. 支持单篇论文和全库检索
7. 返回统一的论文证据结构
"""

import re
from typing import Any

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session

from app.models.paper import Paper, PaperChunk
from app.services.embedding_service import search_similar_chunks


# RRF 排名融合参数
RRF_K = 60

# 两路检索各自召回的候选数量
DEFAULT_CANDIDATE_MULTIPLIER = 4


def tokenize(text: str) -> list[str]:
    """
    中英文混合分词。

    中文：jieba 分词 + 相邻双字词
    英文：保留英文单词、数字和技术标识
    """
    if not text:
        return []

    text = text.lower()
    tokens: list[str] = []

    # 提取英文单词、数字和技术标识
    english_tokens = re.findall(
        r"[a-z][a-z0-9_+#.-]*|\d+(?:\.\d+)*",
        text,
    )
    tokens.extend(english_tokens)

    # 提取连续中文片段
    chinese_segments = re.findall(
        r"[\u4e00-\u9fff]+",
        text,
    )

    for segment in chinese_segments:

        # jieba 分词
        for word in jieba.cut(segment):
            word = word.strip()
            if len(word) >= 2:
                tokens.append(word)

        # 中文相邻双字词
        if len(segment) >= 2:
            for index in range(len(segment) - 1):
                tokens.append(segment[index:index + 2])

        # 单字中文词
        if len(segment) == 1:
            tokens.append(segment)

    # 去重，保持原始顺序
    return list(dict.fromkeys(
        token for token in tokens if token.strip()
    ))


def search_chunks_bm25(
    db: Session,
    query: str,
    limit: int = 5,
    paper_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    使用 BM25 检索论文文本块。
    """

    query = (query or "").strip()

    if not query or limit <= 0:
        return []

    query_tokens = tokenize(query)

    if not query_tokens:
        return []

    # 查询论文文本块
    db_query = (
        db.query(PaperChunk, Paper)
        .join(Paper, PaperChunk.paper_id == Paper.id)
    )

    # 单篇论文过滤
    if paper_id is not None:
        db_query = db_query.filter(
            PaperChunk.paper_id == paper_id
        )

    rows = db_query.all()

    if not rows:
        return []

    # 构建 BM25 语料
    corpus: list[list[str]] = []

    for chunk, paper in rows:
        document_text = f"{paper.title} {chunk.text}"
        corpus.append(tokenize(document_text))

    # 过滤无效语料
    valid_items = [
        (rows[index], corpus[index])
        for index in range(len(rows))
        if corpus[index]
    ]

    if not valid_items:
        return []

    valid_rows = [item[0] for item in valid_items]
    valid_corpus = [item[1] for item in valid_items]

    # BM25 检索
    bm25 = BM25Okapi(valid_corpus)
    scores = bm25.get_scores(query_tokens)

    ranked_indices = sorted(
        range(len(scores)),
        key=lambda index: float(scores[index]),
        reverse=True,
    )

    results: list[dict[str, Any]] = []

    for index in ranked_indices:
        score = float(scores[index])

        if score <= 0:
            continue

        chunk, paper = valid_rows[index]

        results.append({
            "chunk_id": chunk.id,
            "paper_id": chunk.paper_id,
            "paper_title": paper.title,
            "page_number": chunk.page_number,
            "text": chunk.text,
            "retrieval_method": "bm25",
            "retrieval_score": round(score, 6),
        })

        if len(results) >= limit:
            break

    print(
        f"[BM25 检索] query={query!r}, "
        f"paper_id={paper_id}, "
        f"候选文本块={len(rows)}, "
        f"有效结果={len(results)}"
    )

    return results


def reciprocal_rank_fusion(
    bm25_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    limit: int = 5,
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """
    使用 RRF（倒数排名融合）合并 BM25 和向量检索结果。

    RRF 公式：

        RRF(d) = Σ 1 / (k + rank(d))

    不直接比较 BM25 分数和向量相似度，
    而是根据两路检索中的排名进行融合。
    """

    if limit <= 0:
        return []

    merged: dict[int, dict[str, Any]] = {}

    def add_results(
        results: list[dict[str, Any]],
        method: str,
    ) -> None:

        for rank, item in enumerate(results, start=1):

            chunk_id = item.get("chunk_id")

            if chunk_id is None:
                continue

            try:
                chunk_id = int(chunk_id)
            except (TypeError, ValueError):
                continue

            if chunk_id not in merged:
                merged[chunk_id] = {
                    **item,
                    "chunk_id": chunk_id,
                    "rrf_score": 0.0,
                    "retrieval_sources": [],
                    "bm25_rank": None,
                    "vector_rank": None,
                    "bm25_score": None,
                    "vector_score": None,
                }

            record = merged[chunk_id]

            # 累加 RRF 分数
            record["rrf_score"] += 1.0 / (k + rank)

            if method not in record["retrieval_sources"]:
                record["retrieval_sources"].append(method)

            # 记录各路检索排名和原始分数
            if method == "bm25":
                record["bm25_rank"] = rank
                record["bm25_score"] = item.get(
                    "retrieval_score"
                )

            elif method == "vector":
                record["vector_rank"] = rank
                record["vector_score"] = item.get(
                    "retrieval_score"
                )

    add_results(bm25_results, "bm25")
    add_results(vector_results, "vector")

    # 按 RRF 分数排序
    ranked_results = sorted(
        merged.values(),
        key=lambda item: item["rrf_score"],
        reverse=True,
    )

    final_results: list[dict[str, Any]] = []

    for item in ranked_results[:limit]:

        sources = item["retrieval_sources"]

        if len(sources) == 2:
            method = "hybrid"
        elif "bm25" in sources:
            method = "bm25"
        else:
            method = "vector"

        final_results.append({
            "chunk_id": item["chunk_id"],
            "paper_id": item["paper_id"],
            "paper_title": item["paper_title"],
            "page_number": item["page_number"],
            "text": item["text"],
            "retrieval_method": method,
            "retrieval_score": round(
                item["rrf_score"], 8
            ),
            "retrieval_sources": sources,
            "bm25_rank": item["bm25_rank"],
            "vector_rank": item["vector_rank"],
            "bm25_score": item["bm25_score"],
            "vector_score": item["vector_score"],
        })

    return final_results


def search_chunks_hybrid(
    db: Session,
    query: str,
    limit: int = 5,
    paper_id: int | None = None,
    candidate_limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    混合检索入口。

    执行流程：
    1. BM25 关键词检索
    2. BGE-M3 语义向量检索
    3. RRF 排名融合
    4. 返回最终证据

    paper_id=None：全库检索
    paper_id=具体ID：单篇论文检索
    """

    query = (query or "").strip()

    if not query or limit <= 0:
        return []

    if candidate_limit is None:
        candidate_limit = max(
            limit * DEFAULT_CANDIDATE_MULTIPLIER,
            10,
        )

    # 第一条检索路线：BM25
    bm25_results = search_chunks_bm25(
        db=db,
        query=query,
        limit=candidate_limit,
        paper_id=paper_id,
    )

    # 第二条检索路线：BGE-M3 + ChromaDB
    vector_results = search_similar_chunks(
        query=query,
        limit=candidate_limit,
        paper_id=paper_id,
    )

    # RRF 融合
    final_results = reciprocal_rank_fusion(
        bm25_results=bm25_results,
        vector_results=vector_results,
        limit=limit,
    )

    print(
        f"[混合检索] query={query!r}, "
        f"paper_id={paper_id}, "
        f"BM25结果={len(bm25_results)}, "
        f"向量结果={len(vector_results)}, "
        f"最终结果={len(final_results)}"
    )

    return final_results
