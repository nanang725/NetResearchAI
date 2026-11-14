
"""
NetResearch AI - 混合检索服务

功能：
1. SQLite 论文文本块读取
2. jieba 中英文混合分词
3. BM25 关键词检索
4. BGE-M3 语义向量检索
5. RRF 排名融合
6. 单篇论文和全库检索
7. 全库逐篇召回，避免单篇论文垄断
8. 按论文轮流选择证据
9. 论文级参考文献区域识别
10. 保留正文中的正常引用
11. 过滤作者简介、版权声明和题录页
12. 使用 SQLite 论文标题修复向量数据库旧标题
13. BM25 和 BGE-M3 使用统一的参考文献过滤集合
14. 返回统一论文证据结构
"""

import logging
import re
from collections import defaultdict
from typing import Any

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session

from app.models.paper import Paper, PaperChunk
from app.services.embedding_service import search_similar_chunks

logger = logging.getLogger(__name__)


# ============================================================
# 一、检索参数
# ============================================================

RRF_K = 60

DEFAULT_CANDIDATE_MULTIPLIER = 4
MIN_CANDIDATE_LIMIT = 10

FILTER_CANDIDATE_MULTIPLIER = 3


# ============================================================
# 二、非正文识别规则
# ============================================================

REFERENCE_HEADERS = {
    "参考文献",
    "参考资料",
    "references",
    "bibliography",
    "works cited",
    "literature cited",
    "reference",
}

AUTHOR_BIO_HEADERS = {
    "作者简介",
    "作者介绍",
    "作者信息",
    "author biography",
    "author biographies",
    "biographical notes",
    "author bio",
}

APPENDIX_HEADERS = {
    "appendix",
    "appendices",
    "附录",
}


COPYRIGHT_PATTERNS = [
    r"provided proper attribution is provided",
    r"hereby grants permission",
    r"reproduce the tables and figures",
    r"all rights reserved",
    r"copyright\s*©?",
    r"creativecommons\.org",
    r"creative commons attribution",
]


# 只有多个特征同时出现，才倾向于认为是参考文献条目。
# 特别注意：单独出现 [1]、2020、et al. 不足以判定。
REFERENCE_DOI_PATTERNS = [
    r"\bdoi\s*:\s*",
    r"\bdoi\.org/",
]

REFERENCE_VENUE_PATTERNS = [
    r"\bproceedings\b",
    r"\bconference\b",
    r"\bjournal\b",
    r"\btransactions\b",
    r"\bvol\.\s*\d+",
    r"\bvolume\s+\d+",
    r"\bno\.\s*\d+",
    r"\bissue\s+\d+",
    r"\bpp?\.\s*\d+[-–]\d+",
    r"\bpages?\s+\d+[-–]\d+",
]

REFERENCE_AUTHOR_PATTERNS = [
    r"\bet al\.",
    r"\band\s+[A-Z][a-z]+",
    r"\b[A-Z]\.\s*[A-Z]?[a-z]+",
]

REFERENCE_YEAR_PATTERN = r"\b(?:19|20)\d{2}\b"

REFERENCE_NUMBER_PATTERN = (
    r"^\s*(?:\[\s*\d+\s*\]|\d{1,3}\s*[.)、])\s*"
)


# 中文文献出版特征
CHINESE_REFERENCE_PATTERNS = [
    r"\[\s*[JCMRDA]\s*\]",
    r"\b(?:19|20)\d{2}\s*[，,]\s*\d+\s*[\(（]",
    r"等[.。]\s*.{0,30}\[\s*[JCMRDA]\s*\]",
    r"等[.。]\s*.{0,30}\b(?:19|20)\d{2}",
    r"第\d+卷",
    r"第\d+期",
    r"页码?\s*\d+[-–]\d+",
    r"doi\s*[:：]",
]


AUTHOR_BIO_PATTERNS = [
    r"出生于\s*19\d{2}\s*年",
    r"出生于\s*20\d{2}\s*年",
    r"主要研究方向为",
    r"主要研究领域为",
    r"研究方向包括",
    r"现任.{0,15}(教授|副教授|研究员|副研究员)",
    r"author.{0,30}received.{0,30}degree",
    r"his research interests include",
    r"her research interests include",
]


METADATA_PATTERNS = [
    r"issn\s*\d",
    r"cn\s*\d",
    r"网络首发论文",
    r"网络首发日期",
    r"引用格式",
    r"题目\s*[：:]",
    r"作者\s*[：:]",
    r"收稿日期",
    r"出版日期",
]


# ============================================================
# 三、通用文本处理
# ============================================================

def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _get_nonempty_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip()
    ]


def _normalize_heading(line: str) -> str:
    """
    规范化章节标题。

    支持：
    References
    REFERENCES
    参考文献
    参考文献：
    7 References
    7. References
    """

    value = (line or "").strip().lower()

    value = re.sub(
        r"^\s*\d+(?:\.\d+)*\s*[.)、]?\s*",
        "",
        value,
    )

    value = value.strip(" :：.-_")

    return value


def _is_reference_header_line(line: str) -> bool:
    """
    只识别独立的章节标题。

    不会因为正文句子中出现 references
    就把整篇论文判定为参考文献区域。
    """

    normalized = _normalize_heading(line)

    if normalized in REFERENCE_HEADERS:
        return True

    # 允许标题形式：
    # 参考文献（References）
    # References (参考文献)
    compact = re.sub(r"[\s()（）]", "", normalized)

    if compact in {
        "参考文献references",
        "references参考文献",
        "参考资料references",
    }:
        return True

    return False


def _is_appendix_header_line(line: str) -> bool:
    """
    识别附录章节标题。

    支持：
    Appendix
    Appendix A
    Appendices
    附录
    附录一
    """

    value = (line or "").strip().lower()

    if value in APPENDIX_HEADERS:
        return True

    if re.fullmatch(
        r"appendix(?:\s+[a-z0-9]+)?(?:\s*[:：.-].*)?",
        value,
    ):
        return True

    if re.fullmatch(
        r"appendices(?:\s*[:：.-].*)?",
        value,
    ):
        return True

    if re.fullmatch(
        r"附录(?:[一二三四五六七八九十0-9]+)?(?:\s*[:：].*)?",
        value,
    ):
        return True

    return False


# ============================================================
# 四、参考文献条目特征识别
# ============================================================

def _reference_line_signals(line: str) -> dict[str, bool]:
    """
    计算单行文本中的文献格式特征。

    不把正文引用 [1] 或 (Smith, 2020)
    单独当作参考文献条目。

    需要结合编号、作者、年份、出版信息等特征。
    """

    value = (line or "").strip()
    lower = value.lower()

    numbered = bool(
        re.search(REFERENCE_NUMBER_PATTERN, value)
    )

    has_year = bool(
        re.search(REFERENCE_YEAR_PATTERN, value)
    )

    has_doi = any(
        re.search(pattern, lower, re.IGNORECASE)
        for pattern in REFERENCE_DOI_PATTERNS
    )

    has_venue = any(
        re.search(pattern, lower, re.IGNORECASE)
        for pattern in REFERENCE_VENUE_PATTERNS
    )

    has_author = any(
        re.search(pattern, value, re.IGNORECASE)
        for pattern in REFERENCE_AUTHOR_PATTERNS
    )

    has_chinese_format = any(
        re.search(pattern, value, re.IGNORECASE)
        for pattern in CHINESE_REFERENCE_PATTERNS
    )

    # 常见作者格式：
    # Smith, J.
    # Smith, J. and Brown, K.
    # 张三, 李四
    has_author_list = bool(
        re.search(
            r"^[A-Z][A-Za-z'-]+,\s*[A-Z](?:\.|,)",
            value,
        )
        or re.search(
            r"^[\u4e00-\u9fff]{2,4}[，,]\s*"
            r"[\u4e00-\u9fff]{2,4}",
            value,
        )
    )

    return {
        "numbered": numbered,
        "year": has_year,
        "doi": has_doi,
        "venue": has_venue,
        "author": has_author or has_author_list,
        "chinese_format": has_chinese_format,
    }


def _is_reference_entry_line(line: str) -> bool:
    """
    判断一行是否具有足够强的参考文献条目特征。

    规则：
    1. 不能只凭 [1] 判定。
    2. 必须结合年份、作者或出版信息。
    3. DOI、期刊、会议、卷期页码属于强信号。
    """

    value = (line or "").strip()

    if len(value) < 35:
        return False

    signals = _reference_line_signals(value)

    strong_metadata = (
        signals["doi"]
        or signals["venue"]
        or signals["chinese_format"]
    )

    has_publication_year = signals["year"]

    has_author_information = signals["author"]

    has_reference_number = signals["numbered"]

    # 典型编号参考文献：
    # [1] Vaswani, A., et al. Attention...
    # 包含编号、作者、年份/出版信息。
    if (
        has_reference_number
        and has_author_information
        and (has_publication_year or strong_metadata)
    ):
        return True

    # 无编号参考文献：
    # 作者 + 年份 + 期刊/会议/DOI 等出版信息。
    if (
        has_author_information
        and has_publication_year
        and strong_metadata
    ):
        return True

    # 中文文献条目。
    if (
        signals["chinese_format"]
        and has_publication_year
        and (has_reference_number or strong_metadata)
    ):
        return True

    return False


def _is_reference_chunk(text: str) -> bool:
    """
    判断一个文本块是否主要由参考文献条目组成。

    该函数用于没有明确 References 标题的情况。

    注意：
    正文里偶尔出现 [1]、(Smith, 2020) 不会被过滤。
    """

    if not text:
        return False

    lines = _get_nonempty_lines(text)

    if not lines:
        return False

    # 独立章节标题
    if any(
        _is_reference_header_line(line)
        for line in lines
    ):
        return True

    reference_lines = [
        line
        for line in lines
        if _is_reference_entry_line(line)
    ]

    if not reference_lines:
        return False

    ratio = len(reference_lines) / len(lines)

    # 多行文本块：至少两行是明确的文献条目，
    # 且占比超过一半。
    if len(lines) >= 2:
        return (
            len(reference_lines) >= 2
            and ratio >= 0.5
        )

    # 单行文本块：必须自身具有完整文献结构。
    return (
        len(lines) == 1
        and _is_reference_entry_line(lines[0])
    )


# ============================================================
# 五、论文级参考文献区域识别
# ============================================================

def detect_reference_chunk_ids(
    db: Session,
    paper_id: int,
) -> set[int]:
    """
    根据整篇论文的文本块顺序识别参考文献区域。

    依赖 PaperChunk.chunk_index。

    处理方式：

    1. 按 chunk_index 升序读取一篇论文。
    2. 遇到独立 References / 参考文献标题后，
       开启参考文献区域模式。
    3. 从标题所在块开始，过滤后续连续文本块。
    4. 遇到明确的 Appendix / 附录标题时，
       结束参考文献区域模式。
    5. 对没有章节标题的参考文献条目，
       使用严格的条目特征识别作为补充。

    返回：
        需要过滤的 PaperChunk.id 集合。
    """

    chunks = (
        db.query(PaperChunk)
        .filter(PaperChunk.paper_id == paper_id)
        .order_by(
            PaperChunk.chunk_index.asc(),
            PaperChunk.id.asc(),
        )
        .all()
    )

    if not chunks:
        return set()

    reference_ids: set[int] = set()

    in_reference_section = False

    for chunk in chunks:
        chunk_id = chunk.id
        text = chunk.text or ""

        lines = _get_nonempty_lines(text)

        has_reference_header = any(
            _is_reference_header_line(line)
            for line in lines
        )

        has_appendix_header = any(
            _is_appendix_header_line(line)
            for line in lines
        )

        # 明确的 References 标题：
        # 标题所在块也过滤，避免标题本身成为证据。
        if has_reference_header:
            in_reference_section = True
            reference_ids.add(chunk_id)
            continue

        # 明确的附录标题：
        # 结束参考文献区域模式。
        if has_appendix_header:
            in_reference_section = False
            continue

        # 已进入参考文献区域：
        # 后续连续文本块统一过滤。
        if in_reference_section:
            reference_ids.add(chunk_id)
            continue

        # 没有明确章节标题时，
        # 使用严格的参考文献条目特征识别。
        if _is_reference_chunk(text):
            reference_ids.add(chunk_id)

    print(
        f"[参考文献识别] paper_id={paper_id}, "
        f"总文本块={len(chunks)}, "
        f"参考文献文本块={len(reference_ids)}"
    )

    return reference_ids


# ============================================================
# 六、作者简介识别
# ============================================================

def _is_author_bio_chunk(text: str) -> bool:
    """
    判断文本块是否主要为作者个人简介。
    """

    if not text:
        return False

    lower = text.lower()

    for line in _get_nonempty_lines(text):
        if (
            line.strip().lower().rstrip(":：")
            in AUTHOR_BIO_HEADERS
        ):
            return True

    hits = sum(
        1
        for pattern in AUTHOR_BIO_PATTERNS
        if re.search(pattern, lower, re.IGNORECASE)
    )

    if hits >= 2:
        return True

    has_birth = bool(
        re.search(
            r"出生于\s*(?:19|20)\d{2}\s*年",
            text,
        )
    )

    has_profile = bool(
        re.search(
            r"(博士|硕士|教授|副教授|研究员|副研究员)",
            text,
        )
    )

    has_research = bool(
        re.search(
            r"(主要研究方向|主要研究领域|研究方向包括)",
            text,
        )
    )

    if has_birth and has_profile:
        return True

    if has_profile and has_research and len(text) < 1000:
        return True

    return False


# ============================================================
# 七、题录页识别
# ============================================================

def _is_metadata_chunk(text: str) -> bool:
    """
    判断文本块是否主要是论文题录、作者和出版信息。
    """

    if not text:
        return False

    lower = text.lower()

    hits = sum(
        1
        for pattern in METADATA_PATTERNS
        if re.search(pattern, lower, re.IGNORECASE)
    )

    if hits >= 3:
        return True

    if (
        "网络首发论文" in text
        and ("题目：" in text or "题目:" in text)
        and ("作者：" in text or "作者:" in text)
    ):
        return True

    return False


# ============================================================
# 八、非正文统一过滤
# ============================================================

def is_non_body_chunk(
    text: str,
    reference_chunk_ids: set[int] | None = None,
    chunk_id: int | None = None,
) -> bool:
    """
    统一判断文本块是否不适合作为研究内容证据。

    reference_chunk_ids：
        论文级参考文献区域识别得到的 chunk_id 集合。

    chunk_id：
        当前文本块的 SQLite 主键。

    BM25 和 BGE-M3 均使用相同的集合过滤。
    """

    if not text:
        return True

    normalized = _normalize_text(text)
    lower = normalized.lower()

    if len(normalized) < 35:
        return True

    # 论文级参考文献区域过滤
    if (
        reference_chunk_ids is not None
        and chunk_id is not None
        and chunk_id in reference_chunk_ids
    ):
        return True

    # 版权声明
    if any(
        re.search(pattern, lower, re.IGNORECASE)
        for pattern in COPYRIGHT_PATTERNS
    ):
        return True

    # 无论文级集合时的保守兜底识别
    if (
        reference_chunk_ids is None
        and _is_reference_chunk(text)
    ):
        return True

    if _is_author_bio_chunk(text):
        return True

    if _is_metadata_chunk(text):
        return True

    return False


# ============================================================
# 九、SQLite 标题映射
# ============================================================

def _get_paper_title_map(
    db: Session,
) -> dict[int, str]:
    """
    从 SQLite 获取最新论文标题。

    SQLite 是论文标题的权威来源。
    """

    papers = db.query(Paper).all()

    return {
        paper.id: paper.title
        for paper in papers
    }


def _synchronize_result_titles(
    results: list[dict[str, Any]],
    title_map: dict[int, str],
) -> list[dict[str, Any]]:
    """
    统一修正检索结果中的论文标题。
    """

    synchronized = []

    for item in results:
        result = dict(item)

        paper_id = result.get("paper_id")

        if paper_id is not None:
            try:
                paper_id = int(paper_id)
            except (TypeError, ValueError):
                pass

        if paper_id in title_map:
            result["paper_title"] = title_map[paper_id]

        synchronized.append(result)

    return synchronized


# ============================================================
# 十、中英文混合分词
# ============================================================

def tokenize(text: str) -> list[str]:
    """
    中文：jieba 分词 + 相邻双字词。
    英文：保留英文单词、数字和技术标识。
    """

    if not text:
        return []

    text = text.lower()

    tokens: list[str] = []

    english_tokens = re.findall(
        r"[a-z][a-z0-9_+#.-]*|\d+(?:\.\d+)*",
        text,
    )

    tokens.extend(english_tokens)

    chinese_segments = re.findall(
        r"[\u4e00-\u9fff]+",
        text,
    )

    for segment in chinese_segments:
        for word in jieba.cut(segment):
            word = word.strip()

            if len(word) >= 2:
                tokens.append(word)

        if len(segment) >= 2:
            for index in range(len(segment) - 1):
                tokens.append(
                    segment[index:index + 2]
                )

        if len(segment) == 1:
            tokens.append(segment)

    return list(dict.fromkeys(
        token
        for token in tokens
        if token.strip()
    ))


# ============================================================
# 十一、BM25 关键词检索
# ============================================================

def search_chunks_bm25(
    db: Session,
    query: str,
    limit: int = 5,
    paper_id: int | None = None,
    reference_chunk_ids: set[int] | None = None,
) -> list[dict[str, Any]]:

    query = (query or "").strip()

    if not query or limit <= 0:
        return []

    query_tokens = tokenize(query)

    if not query_tokens:
        return []

    db_query = (
        db.query(PaperChunk, Paper)
        .join(
            Paper,
            PaperChunk.paper_id == Paper.id,
        )
    )

    if paper_id is not None:
        db_query = db_query.filter(
            PaperChunk.paper_id == paper_id
        )

    rows = db_query.all()

    if not rows:
        return []

    valid_rows = []

    for chunk, paper in rows:
        if is_non_body_chunk(
            text=chunk.text,
            reference_chunk_ids=reference_chunk_ids,
            chunk_id=chunk.id,
        ):
            continue

        valid_rows.append((chunk, paper))

    if not valid_rows:
        print(
            f"[BM25 检索] paper_id={paper_id}, "
            "没有有效正文"
        )
        return []

    corpus = [
        tokenize(
            f"{paper.title} {chunk.text}"
        )
        for chunk, paper in valid_rows
    ]
    chunk_tokens = [
        set(tokenize(row[0].text))
        for row in valid_rows
    ]

    valid_items = [
        (valid_rows[index], corpus[index], chunk_tokens[index])
        for index in range(len(valid_rows))
        if corpus[index]
    ]

    if not valid_items:
        return []

    filtered_rows = [
        item[0]
        for item in valid_items
    ]

    valid_corpus = [
        item[1]
        for item in valid_items
    ]
    valid_chunk_tokens = [
        item[2]
        for item in valid_items
    ]

    bm25 = BM25Okapi(valid_corpus)

    scores = bm25.get_scores(query_tokens)

    query_token_set = set(query_tokens)
    ranked_indices = sorted(
        (
            index
            for index, tokens in enumerate(valid_chunk_tokens)
            if query_token_set.intersection(tokens)
        ),
        key=lambda index: float(scores[index]),
        reverse=True,
    )

    results: list[dict[str, Any]] = []

    for index in ranked_indices:
        score = float(scores[index])

        chunk, paper = filtered_rows[index]

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
        f"原始文本块={len(rows)}, "
        f"正文文本块={len(valid_rows)}, "
        f"有效结果={len(results)}"
    )

    return results


# ============================================================
# 十二、RRF 排名融合
# ============================================================

def reciprocal_rank_fusion(
    bm25_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    limit: int = 5,
    k: int = RRF_K,
) -> list[dict[str, Any]]:

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

            record["rrf_score"] += (
                1.0 / (k + rank)
            )

            if method not in record["retrieval_sources"]:
                record["retrieval_sources"].append(method)

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
                item["rrf_score"],
                8,
            ),
            "retrieval_sources": sources,
            "bm25_rank": item["bm25_rank"],
            "vector_rank": item["vector_rank"],
            "bm25_score": item["bm25_score"],
            "vector_score": item["vector_score"],
        })

    return final_results


# ============================================================
# 十三、论文级证据选择
# ============================================================

def select_diverse_paper_results(
    paper_results: dict[int, list[dict[str, Any]]],
    limit: int,
) -> list[dict[str, Any]]:

    if limit <= 0 or not paper_results:
        return []

    valid_groups = {
        paper_id: results
        for paper_id, results in paper_results.items()
        if results
    }

    if not valid_groups:
        return []

    ordered_papers = sorted(
        valid_groups.items(),
        key=lambda item: item[1][0]["retrieval_score"],
        reverse=True,
    )

    selected: list[dict[str, Any]] = []

    max_depth = max(
        len(results)
        for _, results in ordered_papers
    )

    for depth in range(max_depth):
        for paper_id, results in ordered_papers:
            if len(selected) >= limit:
                break

            if depth < len(results):
                selected.append(results[depth])

        if len(selected) >= limit:
            break

    paper_distribution: dict[int, int] = defaultdict(int)

    for item in selected:
        paper_distribution[item["paper_id"]] += 1

    print(
        "[论文证据分配] "
        f"参与论文数={len(valid_groups)}, "
        f"最终证据数={len(selected)}, "
        f"论文分布={dict(paper_distribution)}"
    )

    return selected


# ============================================================
# 十四、统一检查向量检索结果
# ============================================================

def _filter_vector_results(
    vector_results: list[dict[str, Any]],
    reference_chunk_ids: set[int],
    paper_id: int,
    title_map: dict[int, str],
) -> list[dict[str, Any]]:
    """
    BGE-M3 结果使用与 BM25 相同的参考文献 chunk_id 集合。

    对于向量结果中没有 chunk_id 的记录，
    不允许直接作为可靠证据返回。
    """

    filtered = []

    for item in vector_results:
        result = dict(item)

        result_paper_id = result.get("paper_id")

        if result_paper_id is not None:
            try:
                result_paper_id = int(result_paper_id)
            except (TypeError, ValueError):
                result_paper_id = None

        # 单篇检索不能混入其他论文的结果
        if (
            result_paper_id is not None
            and result_paper_id != paper_id
        ):
            continue

        chunk_id = result.get("chunk_id")

        if chunk_id is None:
            # 无法核对 SQLite chunk_id，
            # 无法保证论文级参考文献过滤一致性。
            continue

        try:
            chunk_id = int(chunk_id)
        except (TypeError, ValueError):
            continue

        if chunk_id in reference_chunk_ids:
            continue

        if is_non_body_chunk(
            text=result.get("text", ""),
            reference_chunk_ids=reference_chunk_ids,
            chunk_id=chunk_id,
        ):
            continue

        result["chunk_id"] = chunk_id
        result["paper_id"] = paper_id

        if paper_id in title_map:
            result["paper_title"] = title_map[paper_id]

        filtered.append(result)

    return filtered


# ============================================================
# 十五、单篇论文混合检索
# ============================================================

def _search_single_paper_hybrid(
    db: Session,
    query: str,
    paper_id: int,
    candidate_limit: int,
    title_map: dict[int, str],
) -> list[dict[str, Any]]:

    recall_limit = (
        candidate_limit * FILTER_CANDIDATE_MULTIPLIER
    )

    # --------------------------------------------------------
    # 先对整篇论文进行参考文献区域识别
    # --------------------------------------------------------

    reference_chunk_ids = detect_reference_chunk_ids(
        db=db,
        paper_id=paper_id,
    )

    # --------------------------------------------------------
    # BM25：传入论文级参考文献集合
    # --------------------------------------------------------

    bm25_failed = False

    try:
        bm25_results = search_chunks_bm25(
            db=db,
            query=query,
            limit=recall_limit,
            paper_id=paper_id,
            reference_chunk_ids=reference_chunk_ids,
        )
    except Exception:
        logger.exception(
            "BM25 retrieval failed for paper_id=%s",
            paper_id,
        )
        bm25_results = []
        bm25_failed = True

    # --------------------------------------------------------
    # BGE-M3：向量召回后使用相同集合过滤
    # --------------------------------------------------------

    vector_failed = False
    original_vector_count = 0

    try:
        vector_results = search_similar_chunks(
            query=query,
            limit=recall_limit,
            paper_id=paper_id,
        )
        original_vector_count = len(vector_results)

        vector_results = _filter_vector_results(
            vector_results=vector_results,
            reference_chunk_ids=reference_chunk_ids,
            paper_id=paper_id,
            title_map=title_map,
        )
    except Exception:
        logger.exception(
            "Vector retrieval failed for paper_id=%s",
            paper_id,
        )
        vector_results = []
        original_vector_count = 0
        vector_failed = True

    filtered_vector_count = (
        original_vector_count - len(vector_results)
    )

    # --------------------------------------------------------
    # 同步 BM25 标题
    # --------------------------------------------------------

    bm25_results = _synchronize_result_titles(
        bm25_results,
        title_map,
    )

    if bm25_failed or vector_failed:
        available_channels = [
            name
            for name, failed in (
                ("BM25", bm25_failed),
                ("vector", vector_failed),
            )
            if not failed
        ]
        logger.warning(
            "Degraded hybrid retrieval for paper_id=%s; "
            "available channels=%s",
            paper_id,
            ", ".join(available_channels) or "none",
        )

    # --------------------------------------------------------
    # RRF 融合
    # --------------------------------------------------------

    fused_results = reciprocal_rank_fusion(
        bm25_results=bm25_results,
        vector_results=vector_results,
        limit=candidate_limit * 2,
    )

    # 最终兜底过滤
    fused_results = [
        item
        for item in fused_results
        if not is_non_body_chunk(
            text=item.get("text", ""),
            reference_chunk_ids=reference_chunk_ids,
            chunk_id=item.get("chunk_id"),
        )
    ]

    print(
        f"[逐篇融合] paper_id={paper_id}, "
        f"参考文献块={len(reference_chunk_ids)}, "
        f"BM25={len(bm25_results)}, "
        f"Vector原始={original_vector_count}, "
        f"Vector过滤={filtered_vector_count}, "
        f"Vector正文={len(vector_results)}, "
        f"RRF={len(fused_results)}"
    )

    return fused_results


# ============================================================
# 十六、混合检索主入口
# ============================================================

def search_chunks_hybrid(
    db: Session,
    query: str,
    limit: int = 5,
    paper_id: int | None = None,
    candidate_limit: int | None = None,
) -> list[dict[str, Any]]:

    query = (query or "").strip()

    if not query or limit <= 0:
        return []

    if candidate_limit is None:
        candidate_limit = max(
            limit * DEFAULT_CANDIDATE_MULTIPLIER,
            MIN_CANDIDATE_LIMIT,
        )

    # SQLite 标题映射
    title_map = _get_paper_title_map(db)

    # ========================================================
    # 单篇论文模式
    # ========================================================

    if paper_id is not None:

        if paper_id not in title_map:
            return []

        results = _search_single_paper_hybrid(
            db=db,
            query=query,
            paper_id=paper_id,
            candidate_limit=candidate_limit,
            title_map=title_map,
        )

        final_results = results[:limit]

        print(
            f"[单篇混合检索] query={query!r}, "
            f"paper_id={paper_id}, "
            f"候选数量={len(results)}, "
            f"最终结果={len(final_results)}"
        )

        return final_results

    # ========================================================
    # 全库论文模式
    # ========================================================

    paper_rows = (
        db.query(Paper.id)
        .order_by(Paper.id.asc())
        .all()
    )

    paper_ids = [
        row[0]
        for row in paper_rows
    ]

    if not paper_ids:
        print("[全库混合检索] 当前知识库没有论文。")
        return []

    print(
        f"[全库混合检索] 开始逐篇检索，"
        f"论文数量={len(paper_ids)}, "
        f"每篇候选数量={candidate_limit}"
    )

    paper_results: dict[
        int,
        list[dict[str, Any]]
    ] = {}

    for current_paper_id in paper_ids:

        try:
            results = _search_single_paper_hybrid(
                db=db,
                query=query,
                paper_id=current_paper_id,
                candidate_limit=candidate_limit,
                title_map=title_map,
            )

            if results:
                paper_results[current_paper_id] = results

            print(
                f"[逐篇检索] "
                f"paper_id={current_paper_id}, "
                f"有效正文证据={len(results)}"
            )

        except Exception as exc:
            print(
                f"[逐篇检索失败] "
                f"paper_id={current_paper_id}, "
                f"错误={exc}"
            )

    if not paper_results:
        print(
            "[全库混合检索] "
            "没有检索到有效正文证据。"
        )
        return []

    # ========================================================
    # 论文多样性证据选择
    # ========================================================

    final_results = select_diverse_paper_results(
        paper_results=paper_results,
        limit=limit,
    )

    print(
        f"[全库混合检索完成] query={query!r}, "
        f"知识库论文数={len(paper_ids)}, "
        f"有正文结果论文数={len(paper_results)}, "
        f"最终证据数={len(final_results)}"
    )

    return final_results
