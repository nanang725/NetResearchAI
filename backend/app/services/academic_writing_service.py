from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk, PaperElement
from app.services.retrieval_service import search_chunks_hybrid


OUTLINE_EVIDENCE_LIMIT = 8
OUTLINE_DISCLAIMER = (
    "大纲由系统依据本地论文检索 Chunk 组织生成。引用内容来自已解析原文；"
    "没有证据支持的章节会明确标记，不生成虚构结论。"
)
REFERENCES_DISCLAIMER = (
    "参考文献元数据仅从本地 SQLite 论文库读取，未确认的字段保留为未知。"
    "本功能不调用生成模型补写 DOI、作者、标题、期刊或年份。"
)


def _clean_excerpt(text: str, limit: int = 700) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return normalized[:limit]


def _known_text(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().casefold() not in {
        "unknown",
        "unknownauthor",
        "未知",
        "未知作者",
        "n/a",
        "none",
    }


def _paper_or_404(db: Session, paper_id: int) -> Paper:
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if paper is None:
        raise AppError(
            "论文不存在。",
            code="PAPER_NOT_FOUND",
            status_code=404,
            details={"paper_id": paper_id},
        )
    return paper


def _section_label(element: PaperElement | None, index: int) -> str:
    if element is None or not element.section_path:
        return f"证据片段 {index}"
    try:
        path = json.loads(element.section_path)
    except (TypeError, json.JSONDecodeError):
        path = None
    if isinstance(path, list):
        labels = [str(item).strip() for item in path if str(item).strip()]
        if labels:
            return labels[-1][:120]
    return f"证据片段 {index}"


def _outline_evidence(
    db: Session,
    paper: Paper,
    topic: str,
) -> tuple[list[dict[str, Any]], list[PaperChunk]]:
    results = search_chunks_hybrid(
        db=db,
        query=topic,
        limit=OUTLINE_EVIDENCE_LIMIT,
        paper_id=paper.id,
    )
    chunk_ids = []
    for item in results:
        try:
            chunk_id = int(item.get("chunk_id"))
        except (TypeError, ValueError):
            continue
        if int(item.get("paper_id", -1)) == paper.id:
            chunk_ids.append(chunk_id)
    chunk_ids = list(dict.fromkeys(chunk_ids))
    if not chunk_ids:
        raise AppError(
            "当前论文没有可用于生成大纲的检索证据。",
            code="OUTLINE_INSUFFICIENT_EVIDENCE",
            status_code=422,
            details={"paper_id": paper.id, "topic": topic},
        )

    chunks = (
        db.query(PaperChunk)
        .filter(
            PaperChunk.paper_id == paper.id,
            PaperChunk.id.in_(chunk_ids),
        )
        .order_by(PaperChunk.id.asc())
        .all()
    )
    chunk_map = {chunk.id: chunk for chunk in chunks}
    element_ids = [
        chunk.element_id
        for chunk in chunks
        if chunk.element_id is not None
    ]
    elements = (
        db.query(PaperElement)
        .filter(PaperElement.id.in_(element_ids))
        .all()
        if element_ids
        else []
    )
    element_map = {element.id: element for element in elements}

    valid_chunks = [
        chunk_map[chunk_id]
        for chunk_id in chunk_ids
        if chunk_id in chunk_map and chunk_map[chunk_id].text.strip()
    ]
    if not valid_chunks:
        raise AppError(
            "检索结果未对应到当前论文的有效本地 Chunk。",
            code="OUTLINE_INSUFFICIENT_EVIDENCE",
            status_code=422,
            details={"paper_id": paper.id, "topic": topic},
        )

    evidence = []
    for index, chunk in enumerate(valid_chunks[:5], start=1):
        element = element_map.get(chunk.element_id)
        evidence.append(
            {
                "citation_id": index,
                "paper_id": paper.id,
                "title": paper.title,
                "page_number": chunk.page_number,
                "chunk_id": chunk.id,
                "section_label": _section_label(element, index),
                "excerpt": _clean_excerpt(chunk.text),
            }
        )
    return evidence, valid_chunks[:5]


def generate_academic_outline(
    db: Session,
    *,
    paper_id: int,
    topic: str,
) -> dict[str, Any]:
    paper = _paper_or_404(db, paper_id)
    normalized_topic = re.sub(r"\s+", " ", topic or "").strip()
    if not normalized_topic:
        normalized_topic = paper.title
    if len(normalized_topic) < 2 or len(normalized_topic) > 200:
        raise AppError(
            "写作主题长度必须为 2 至 200 字符。",
            code="INVALID_WRITING_TOPIC",
            status_code=422,
        )

    evidence, chunks = _outline_evidence(db, paper, normalized_topic)
    introduction = evidence[:2]
    evidence_children = []
    for index, item in enumerate(evidence, start=1):
        evidence_children.append(
            {
                "level": 2,
                "heading": f"1.{index} {item['section_label']}",
                "writing_points": [
                    (
                        f"围绕主题“{normalized_topic}”说明该段材料呈现的"
                        "研究背景、问题或方法。"
                    ),
                    (
                        "仅依据所附原文提炼作者观点，不扩展原文未出现的"
                        "数据、结果或因果结论。"
                    ),
                ],
                "evidence": [item],
                "evidence_status": "covered",
                "unsupported_note": None,
            }
        )

    sections = [
        {
            "level": 1,
            "heading": "1. 研究背景与问题",
            "writing_points": [
                (
                    f"以本地论文《{paper.title}》为研究对象，限定主题为"
                    f"“{normalized_topic}”。"
                ),
                (
                    "从证据中提炼研究动机和问题边界；若原文未明确陈述，"
                    "不得补写为作者观点。"
                ),
            ],
            "evidence": introduction,
            "evidence_status": "covered" if introduction else "insufficient_evidence",
            "unsupported_note": (
                None
                if introduction
                else "当前检索结果不足，不能确认研究背景。"
            ),
        },
        *evidence_children,
        {
            "level": 1,
            "heading": "2. 证据综合与论证",
            "writing_points": [
                "按证据之间的共同点、差异或方法关系组织论证。",
                "每个事实性判断均回链到论文页码和 Chunk。",
            ],
            "evidence": evidence,
            "evidence_status": "covered",
            "unsupported_note": None,
        },
        {
            "level": 1,
            "heading": "3. 局限、结论与待补证据",
            "writing_points": [
                (
                    "当前检索证据未被用于推断研究局限或最终结论；"
                    "请回到论文原文确认后再撰写。"
                ),
                "不得根据标题或常识自动生成实验结果。",
            ],
            "evidence": [],
            "evidence_status": "insufficient_evidence",
            "unsupported_note": (
                "该章节缺少经当前检索确认的直接证据，因此明确标记为待补，"
                "系统未生成事实性结论。"
            ),
        },
    ]
    return {
        "paper": {
            "paper_id": paper.id,
            "title": paper.title,
            "authors": paper.authors,
            "publication_year": paper.publication_year,
            "doi": paper.doi,
        },
        "topic": normalized_topic,
        "title": f"{paper.title}：学术写作大纲",
        "sections": sections,
        "evidence_sources": evidence,
        "evidence_chunk_count": len(chunks),
        "generated_by": "evidence_based_outline_template",
        "content_labels": {
            "original_evidence": "引用论文原文 Chunk，未改写事实内容",
            "generated_content": "写作提示与章节组织提示",
            "statistics": "本接口不包含数据统计",
        },
        "disclaimer": OUTLINE_DISCLAIMER,
    }


def _bibtex_key(paper_id: int) -> str:
    return f"localPaper{paper_id}"


def _reference_item(
    db: Session,
    paper: Paper,
) -> dict[str, Any]:
    chunks = (
        db.query(PaperChunk)
        .filter(PaperChunk.paper_id == paper.id)
        .order_by(
            PaperChunk.page_number.asc(),
            PaperChunk.page_chunk_index.asc(),
            PaperChunk.id.asc(),
        )
        .all()
    )
    first_chunk = next((chunk for chunk in chunks if chunk.text.strip()), None)
    title_known = _known_text(paper.title)
    authors_known = _known_text(paper.authors)
    year_known = paper.publication_year is not None
    doi_known = _known_text(paper.doi)
    venue_known = _known_text(paper.venue)
    unknown_fields = [
        field
        for field, known in (
            ("title", title_known),
            ("authors", authors_known),
            ("year", year_known),
            ("doi", doi_known),
            ("venue", venue_known),
        )
        if not known
    ]
    verified = first_chunk is not None
    evidence = (
        {
            "paper_id": paper.id,
            "title": paper.title,
            "page_number": first_chunk.page_number,
            "chunk_id": first_chunk.id,
            "excerpt": _clean_excerpt(first_chunk.text, 420),
        }
        if first_chunk is not None
        else None
    )
    citation_parts = [
        paper.authors if authors_known else "作者：未知",
        paper.title if title_known else "标题：未知",
        str(paper.publication_year) if year_known else "年份：未知",
        paper.venue if venue_known else "期刊/会议：未知",
        f"DOI: {paper.doi}" if doi_known else "DOI：未知",
    ]
    bib_fields = []
    if authors_known:
        bib_fields.append(f"  author = {{{paper.authors}}}")
    if title_known:
        bib_fields.append(f"  title = {{{paper.title}}}")
    if year_known:
        bib_fields.append(f"  year = {{{paper.publication_year}}}")
    if venue_known:
        bib_fields.append(f"  venue = {{{paper.venue}}}")
    if doi_known:
        bib_fields.append(f"  doi = {{{paper.doi}}}")
    bib_fields.append(f"  note = {{Local library paper ID {paper.id}}}")
    return {
        "paper_id": paper.id,
        "verified_local": verified,
        "can_cite": verified,
        "title": paper.title if title_known else None,
        "authors": paper.authors if authors_known else None,
        "year": paper.publication_year if year_known else None,
        "venue": paper.venue if venue_known else None,
        "doi": paper.doi if doi_known else None,
        "unknown_fields": unknown_fields,
        "metadata_sources": {
            "title": "local_sqlite" if title_known else "unknown",
            "authors": "local_sqlite" if authors_known else "unknown",
            "year": "local_sqlite" if year_known else "unknown",
            "venue": "local_sqlite" if venue_known else "unknown",
            "doi": "local_sqlite" if doi_known else "unknown",
        },
        "citation_text": "; ".join(citation_parts) + ".",
        "bibtex": "@misc{" + _bibtex_key(paper.id) + ",\n"
        + ",\n".join(bib_fields)
        + "\n}",
        "evidence": evidence,
        "verification_note": (
            "已关联本地论文和可核验 Chunk。"
            if verified
            else "本地论文存在，但没有可核验的正文 Chunk，不能作为可靠引用。"
        ),
    }


def list_verifiable_references(
    db: Session,
    *,
    paper_ids: list[int],
) -> dict[str, Any]:
    if len(paper_ids) != len(set(paper_ids)):
        raise AppError(
            "参考文献论文不能重复选择。",
            code="DUPLICATE_PAPER_ID",
            status_code=422,
        )
    if paper_ids:
        papers = (
            db.query(Paper)
            .filter(Paper.id.in_(paper_ids))
            .order_by(Paper.id.asc())
            .all()
        )
        paper_map = {paper.id: paper for paper in papers}
        missing = [paper_id for paper_id in paper_ids if paper_id not in paper_map]
        if missing:
            raise AppError(
                "参考文献包含不存在的本地论文。",
                code="PAPER_NOT_FOUND",
                status_code=404,
                details={"paper_ids": missing},
            )
    else:
        papers = db.query(Paper).order_by(Paper.id.asc()).all()

    references = [_reference_item(db, paper) for paper in papers]
    return {
        "references": references,
        "total_count": len(references),
        "verified_count": sum(item["verified_local"] for item in references),
        "unknown_field_count": sum(
            len(item["unknown_fields"]) for item in references
        ),
        "disclaimer": REFERENCES_DISCLAIMER,
        "content_labels": {
            "original_evidence": "本地论文元数据与正文 Chunk",
            "generated_content": "无，References 不经过生成模型",
            "statistics": "本地核验数量统计",
        },
    }
