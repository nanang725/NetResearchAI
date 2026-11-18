from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.citation import CitationEdge, CitationReference
from app.models.knowledge_graph import (
    KnowledgeEdge,
    KnowledgeExtractionRun,
    KnowledgeNode,
)
from app.models.paper import Paper, PaperChunk


EXTRACTION_VERSION = "1"
NODE_TYPES = {
    "paper",
    "author",
    "topic",
    "method",
    "dataset",
    "model",
    "finding",
    "institution",
}
RELATION_TYPES = {
    "CITES",
    "AUTHORED_BY",
    "STUDIES",
    "USES_METHOD",
    "USES_DATASET",
    "USES_MODEL",
    "SUPPORTS",
    "CONTRADICTS",
    "EXTENDS",
    "AFFILIATED_WITH",
}

_SPACE_PATTERN = re.compile(r"\s+")
_SENTENCE_PATTERN = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_CITATION_MARKER_PATTERN = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_REFERENCE_LABEL_PATTERN = re.compile(r"^\s*\[?(\d+)\]?[.)、]?\s+")
_INSTITUTION_PATTERN = re.compile(
    r"\b("
    r"[A-Z][A-Za-z&.'-]*(?:\s+[A-Z][A-Za-z&.'-]*){0,6}"
    r"\s+(?:University|Institute|Laboratory|College|Academy|"
    r"Research Center|School)"
    r")\b"
)

_TOPIC_PATTERNS = (
    (
        "Retrieval-Augmented Generation",
        re.compile(
            r"\b(?:retrieval[- ]augmented generation|RAG)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Large Language Models",
        re.compile(
            r"\b(?:large language models?|LLMs?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "AI Agents",
        re.compile(
            r"\b(?:AI agents?|agentic systems?|autonomous agents?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Knowledge Graphs",
        re.compile(r"\bknowledge graphs?\b", re.IGNORECASE),
    ),
    (
        "Computer Networks",
        re.compile(
            r"\b(?:computer networks?|network protocols?|network traffic)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Academic Question Answering",
        re.compile(
            r"\b(?:academic|scholarly|scientific|literature)"
            r"\s+(?:question answering|QA|question-answering)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Information Retrieval",
        re.compile(
            r"\binformation retrieval\b",
            re.IGNORECASE,
        ),
    ),
)

_KNOWN_METHODS = {
    "BM25": re.compile(r"\bBM25\b", re.IGNORECASE),
    "Reciprocal Rank Fusion": re.compile(
        r"\b(?:Reciprocal Rank Fusion|RRF)\b",
        re.IGNORECASE,
    ),
    "Hybrid Retrieval": re.compile(
        r"\bhybrid retrieval\b",
        re.IGNORECASE,
    ),
    "Graph Neural Network": re.compile(
        r"\b(?:graph neural networks?|GNNs?)\b",
        re.IGNORECASE,
    ),
    "Contrastive Learning": re.compile(
        r"\bcontrastive learning\b",
        re.IGNORECASE,
    ),
    "Reinforcement Learning": re.compile(
        r"\breinforcement learning\b",
        re.IGNORECASE,
    ),
    "Fine-Tuning": re.compile(
        r"\bfine[- ]tuning\b",
        re.IGNORECASE,
    ),
    "PageRank": re.compile(r"\bPageRank\b", re.IGNORECASE),
}

_KNOWN_DATASETS = {
    "SQuAD": re.compile(r"\bSQuAD(?:\s*2\.0)?\b", re.IGNORECASE),
    "MS MARCO": re.compile(r"\bMS\s+MARCO\b", re.IGNORECASE),
    "HotpotQA": re.compile(r"\bHotpotQA\b", re.IGNORECASE),
    "Natural Questions": re.compile(
        r"\bNatural Questions\b",
        re.IGNORECASE,
    ),
    "WebQuestions": re.compile(r"\bWebQuestions\b", re.IGNORECASE),
    "TriviaQA": re.compile(r"\bTriviaQA\b", re.IGNORECASE),
    "BEIR": re.compile(r"\bBEIR\b", re.IGNORECASE),
    "WikiText": re.compile(r"\bWikiText(?:-103)?\b", re.IGNORECASE),
    "ImageNet": re.compile(r"\bImageNet\b", re.IGNORECASE),
}

_KNOWN_MODELS = {
    "BERT": re.compile(r"\bBERT\b", re.IGNORECASE),
    "RoBERTa": re.compile(r"\bRoBERTa\b", re.IGNORECASE),
    "GPT": re.compile(r"\bGPT(?:-[234](?:\.\d+)?)?\b", re.IGNORECASE),
    "LLaMA": re.compile(r"\bLLaMA(?:\s*\d+)?\b", re.IGNORECASE),
    "T5": re.compile(r"\b(?:T5|mT5)\b", re.IGNORECASE),
    "BGE-M3": re.compile(r"\bBGE-M3\b", re.IGNORECASE),
}

_DATASET_CONTEXT = re.compile(
    r"\b(?:dataset|corpus|benchmark)\s+"
    r"(?:called|named|known as)\s+"
    r"[\"'“”]?([A-Z][A-Za-z0-9_.-]{1,48}"
    r"(?:\s+[A-Z0-9][A-Za-z0-9_.-]{1,48}){0,3})",
    re.IGNORECASE,
)
_DATASET_PRECEDING_CONTEXT = re.compile(
    r"\b([A-Z][A-Za-z0-9_.-]{1,48}"
    r"(?:\s+[A-Z0-9][A-Za-z0-9_.-]{1,48}){0,3})"
    r"\s+(?:dataset|corpus|benchmark)\b"
)
_DATASET_TARGET_CONTEXT = re.compile(
    r"\b(?:on|using|evaluate on|with)\s+(?:the\s+)?"
    r"([A-Z][A-Za-z0-9_.-]{1,48}"
    r"(?:\s+[A-Z0-9][A-Za-z0-9_.-]{1,48}){0,3})"
    r"\s+(?:dataset|corpus|benchmark)\b",
    re.IGNORECASE,
)
_MODEL_CONTEXT = re.compile(
    r"\b(?:model|architecture|framework)\s+"
    r"(?:called|named|known as)\s+"
    r"[\"'“”]?([A-Z][A-Za-z0-9_.-]{1,48}"
    r"(?:\s+[A-Z0-9][A-Za-z0-9_.-]{1,48}){0,3})",
    re.IGNORECASE,
)
_METHOD_CONTEXT = re.compile(
    r"\b(?:method|approach|algorithm|technique)\s+"
    r"(?:called|named)\s+"
    r"[\"'“”]?([A-Z][A-Za-z0-9_.-]{1,48}"
    r"(?:\s+[A-Z0-9][A-Za-z0-9_.-]{1,48}){0,3})",
    re.IGNORECASE,
)
_FINDING_CUE = re.compile(
    r"\b(?:we find|we observe|results? (?:show|indicate|demonstrate)|"
    r"experiments? (?:show|demonstrate)|our results|outperform|"
    r"significantly improves?|is effective|are effective)\b",
    re.IGNORECASE,
)
_SUPPORT_CUE = re.compile(
    r"\b(?:supports?|consistent with|confirms?|corroborat\w*)\b",
    re.IGNORECASE,
)
_CONTRADICT_CUE = re.compile(
    r"\b(?:contradicts?|inconsistent with|fails to|challenges?|"
    r"contrary to)\b",
    re.IGNORECASE,
)
_EXTEND_CUE = re.compile(
    r"\b(?:extends?|builds? (?:up)?on|improves? upon|"
    r"follow[- ]up)\b",
    re.IGNORECASE,
)
_GENERIC_NAME_STOPWORDS = {
    "dataset",
    "datasets",
    "corpus",
    "benchmark",
    "model",
    "models",
    "method",
    "methods",
    "approach",
    "approaches",
    "this",
    "these",
    "our",
    "the",
    "paper",
    "work",
}


@dataclass(frozen=True)
class EvidenceChunk:
    paper_id: int
    page_number: int
    chunk_id: int
    text: str


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return _SPACE_PATTERN.sub(" ", normalized).strip()


def _clean_label(value: str, max_length: int = 1024) -> str:
    label = _SPACE_PATTERN.sub(" ", (value or "").strip(" \t\r\n,;:.-"))
    return label[:max_length]


def _evidence_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sentences(text: str) -> list[str]:
    values = [
        _clean_label(value, 2000)
        for value in _SENTENCE_PATTERN.split(text or "")
        if _clean_label(value, 2000)
    ]
    return values


def _evidence_excerpt(text: str, needle: str | None = None) -> str:
    if needle:
        normalized_needle = _normalize(needle)
        for sentence in _sentences(text):
            if normalized_needle in _normalize(sentence):
                return sentence[:2000]
    return _clean_label(text, 2000)


def _split_authors(raw: str | None) -> list[str]:
    if not raw:
        return []
    values = re.split(r"\s*(?:;|\band\b|&)\s*", raw)
    if len(values) == 1:
        values = re.split(r"\s*,\s*", raw)
    authors: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_label(value, 160)
        if len(cleaned) < 2 or len(cleaned) > 120:
            continue
        normalized = _normalize(cleaned)
        if normalized in seen:
            continue
        seen.add(normalized)
        authors.append(cleaned)
    return authors


def _generic_name_allowed(value: str) -> bool:
    cleaned = _clean_label(value, 80)
    normalized = _normalize(cleaned)
    if len(cleaned) < 2 or normalized in _GENERIC_NAME_STOPWORDS:
        return False
    if normalized.split(" ", 1)[0] in _GENERIC_NAME_STOPWORDS:
        return False
    return not cleaned.endswith((".", ",", ";"))


def _ensure_node(
    db: Session,
    cache: dict[tuple[str, str, int | None], KnowledgeNode],
    *,
    node_type: str,
    label: str,
    paper_id: int | None = None,
    attributes: dict[str, Any] | None = None,
) -> KnowledgeNode:
    cleaned = _clean_label(label)
    normalized = _normalize(cleaned)
    key = (node_type, normalized, paper_id if node_type == "paper" else None)
    cached = cache.get(key)
    if cached is not None:
        return cached
    query = db.query(KnowledgeNode).filter(
        KnowledgeNode.node_type == node_type,
        KnowledgeNode.normalized_name == normalized,
    )
    if node_type == "paper":
        query = query.filter(KnowledgeNode.paper_id == paper_id)
    else:
        query = query.filter(KnowledgeNode.paper_id.is_(None))
    node = query.first()
    if node is None:
        node = KnowledgeNode(
            node_type=node_type,
            label=cleaned,
            normalized_name=normalized,
            paper_id=paper_id if node_type == "paper" else None,
            attributes_json=_json_dump(attributes or {}),
        )
        db.add(node)
        db.flush()
    elif attributes:
        existing = {}
        try:
            loaded = json.loads(node.attributes_json or "{}")
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            pass
        existing.update(attributes)
        node.attributes_json = _json_dump(existing)
    cache[key] = node
    return node


def _add_edge(
    db: Session,
    *,
    source: KnowledgeNode,
    target: KnowledgeNode,
    relation_type: str,
    evidence: EvidenceChunk,
    evidence_text: str,
    confidence: float,
    extraction_method: str,
    attributes: dict[str, Any] | None = None,
) -> bool:
    if relation_type not in RELATION_TYPES:
        raise ValueError(f"Unsupported knowledge relation: {relation_type}")
    if source.id == target.id:
        return False
    excerpt = _clean_label(evidence_text, 6000)
    if not excerpt:
        return False
    digest = _evidence_hash(
        f"{source.id}|{target.id}|{relation_type}|{evidence.chunk_id}|{excerpt}"
    )
    for pending in db.new:
        if not isinstance(pending, KnowledgeEdge):
            continue
        if (
            pending.source_node_id == source.id
            and pending.target_node_id == target.id
            and pending.relation_type == relation_type
            and pending.chunk_id == evidence.chunk_id
            and pending.evidence_hash == digest
        ):
            return False
    exists = (
        db.query(KnowledgeEdge.id)
        .filter(
            KnowledgeEdge.source_node_id == source.id,
            KnowledgeEdge.target_node_id == target.id,
            KnowledgeEdge.relation_type == relation_type,
            KnowledgeEdge.chunk_id == evidence.chunk_id,
            KnowledgeEdge.evidence_hash == digest,
        )
        .first()
    )
    if exists is not None:
        return False
    db.add(
        KnowledgeEdge(
            source_node_id=source.id,
            target_node_id=target.id,
            relation_type=relation_type,
            paper_id=evidence.paper_id,
            page_number=evidence.page_number,
            chunk_id=evidence.chunk_id,
            evidence_text=excerpt,
            evidence_hash=digest,
            confidence=max(0.0, min(1.0, confidence)),
            extraction_method=extraction_method,
            attributes_json=_json_dump(attributes or {}),
        )
    )
    return True


def _chunk_for_reference(
    chunks: list[PaperChunk],
    reference: CitationReference,
) -> PaperChunk | None:
    same_page = [
        chunk
        for chunk in chunks
        if chunk.page_number == reference.source_page_number
    ]
    candidates = same_page or chunks
    if not candidates:
        return None
    title = _normalize(reference.title or "")
    raw_text = _normalize(reference.raw_text or "")[:500]
    doi = _normalize(reference.doi or "")
    best: tuple[float, PaperChunk] | None = None
    for chunk in candidates:
        text = _normalize(chunk.text)
        score = 0.0
        if title and title in text:
            score += 8.0
        if raw_text and raw_text in text:
            score += 6.0
        if doi and doi in text:
            score += 5.0
        if not score:
            tokens = {
                token
                for token in re.findall(r"[a-z0-9]{4,}", _normalize(
                    reference.title or reference.raw_text
                ))
            }
            if tokens:
                text_tokens = set(re.findall(r"[a-z0-9]{4,}", text))
                overlap = len(tokens & text_tokens)
                score = overlap / max(1, min(len(tokens), 30))
        if score >= 0.25 and (best is None or score > best[0]):
            best = (score, chunk)
    return best[1] if best is not None else None


def _citation_labels(reference: CitationReference) -> set[str]:
    match = _REFERENCE_LABEL_PATTERN.match(reference.raw_text or "")
    return {match.group(1)} if match else set()


def _semantic_relation(sentence: str) -> str | None:
    if _CONTRADICT_CUE.search(sentence):
        return "CONTRADICTS"
    if _EXTEND_CUE.search(sentence):
        return "EXTENDS"
    if _SUPPORT_CUE.search(sentence):
        return "SUPPORTS"
    return None


def _paper_node(
    db: Session,
    cache: dict[tuple[str, str, int | None], KnowledgeNode],
    paper: Paper,
) -> KnowledgeNode:
    node = _ensure_node(
        db,
        cache,
        node_type="paper",
        label=paper.title,
        paper_id=paper.id,
        attributes={
            "publication_year": paper.publication_year,
            "doi": paper.doi,
            "venue": paper.venue,
        },
    )
    node.label = paper.title
    node.normalized_name = _normalize(paper.title)
    return node


def _extract_authors(
    db: Session,
    chunks_by_paper: dict[int, list[PaperChunk]],
    paper: Paper,
    paper_node: KnowledgeNode,
    cache: dict[tuple[str, str, int | None], KnowledgeNode],
) -> int:
    added = 0
    chunks = chunks_by_paper.get(paper.id, [])
    for author in _split_authors(paper.authors):
        normalized_author = _normalize(author)
        surname = normalized_author.split()[-1] if normalized_author else ""
        match = next(
            (
                chunk
                for chunk in chunks
                if normalized_author in _normalize(chunk.text)
                or (len(surname) >= 3 and surname in _normalize(chunk.text))
            ),
            None,
        )
        if match is None:
            continue
        author_node = _ensure_node(
            db,
            cache,
            node_type="author",
            label=author,
        )
        evidence = EvidenceChunk(
            paper_id=paper.id,
            page_number=match.page_number,
            chunk_id=match.id,
            text=match.text,
        )
        added += int(
            _add_edge(
                db,
                source=paper_node,
                target=author_node,
                relation_type="AUTHORED_BY",
                evidence=evidence,
                evidence_text=_evidence_excerpt(match.text, author),
                confidence=0.95 if normalized_author in _normalize(match.text) else 0.82,
                extraction_method="author_metadata_chunk_match",
            )
        )
    return added


def _extract_institutions(
    db: Session,
    chunks: list[PaperChunk],
    paper: Paper,
    paper_node: KnowledgeNode,
    cache: dict[tuple[str, str, int | None], KnowledgeNode],
) -> int:
    added = 0
    authors = _split_authors(paper.authors)
    for chunk in chunks:
        if chunk.page_number > 4:
            break
        for match in _INSTITUTION_PATTERN.finditer(chunk.text):
            institution = _clean_label(match.group(1), 300)
            if not _generic_name_allowed(institution):
                continue
            institution_node = _ensure_node(
                db,
                cache,
                node_type="institution",
                label=institution,
            )
            evidence = EvidenceChunk(
                paper_id=paper.id,
                page_number=chunk.page_number,
                chunk_id=chunk.id,
                text=chunk.text,
            )
            added += int(
                _add_edge(
                    db,
                    source=paper_node,
                    target=institution_node,
                    relation_type="AFFILIATED_WITH",
                    evidence=evidence,
                    evidence_text=_evidence_excerpt(chunk.text, institution),
                    confidence=0.72,
                    extraction_method="affiliation_context_rule",
                )
            )
            normalized_chunk = _normalize(chunk.text)
            for author in authors:
                normalized_author = _normalize(author)
                surname = normalized_author.split()[-1] if normalized_author else ""
                if normalized_author not in normalized_chunk and (
                    len(surname) < 3 or surname not in normalized_chunk
                ):
                    continue
                author_node = _ensure_node(
                    db,
                    cache,
                    node_type="author",
                    label=author,
                )
                added += int(
                    _add_edge(
                        db,
                        source=author_node,
                        target=institution_node,
                        relation_type="AFFILIATED_WITH",
                        evidence=evidence,
                        evidence_text=_evidence_excerpt(chunk.text, author),
                        confidence=0.68,
                        extraction_method="cooccurrence_affiliation_rule",
                    )
                )
    return added


def _extract_topics(
    db: Session,
    chunk: PaperChunk,
    paper_node: KnowledgeNode,
    cache: dict[tuple[str, str, int | None], KnowledgeNode],
) -> int:
    added = 0
    evidence = EvidenceChunk(
        paper_id=chunk.paper_id,
        page_number=chunk.page_number,
        chunk_id=chunk.id,
        text=chunk.text,
    )
    for label, pattern in _TOPIC_PATTERNS:
        match = pattern.search(chunk.text)
        if match is None:
            continue
        topic_node = _ensure_node(
            db,
            cache,
            node_type="topic",
            label=label,
        )
        added += int(
            _add_edge(
                db,
                source=paper_node,
                target=topic_node,
                relation_type="STUDIES",
                evidence=evidence,
                evidence_text=_evidence_excerpt(chunk.text, match.group(0)),
                confidence=0.9,
                extraction_method="topic_lexicon_rule",
            )
        )
    return added


def _extract_named_entities(
    db: Session,
    chunk: PaperChunk,
    paper_node: KnowledgeNode,
    cache: dict[tuple[str, str, int | None], KnowledgeNode],
) -> int:
    added = 0
    evidence = EvidenceChunk(
        paper_id=chunk.paper_id,
        page_number=chunk.page_number,
        chunk_id=chunk.id,
        text=chunk.text,
    )
    groups = (
        ("method", "USES_METHOD", _KNOWN_METHODS, _METHOD_CONTEXT),
        (
            "dataset",
            "USES_DATASET",
            _KNOWN_DATASETS,
            (
                _DATASET_CONTEXT,
                _DATASET_PRECEDING_CONTEXT,
                _DATASET_TARGET_CONTEXT,
            ),
        ),
        ("model", "USES_MODEL", _KNOWN_MODELS, _MODEL_CONTEXT),
    )
    for node_type, relation_type, known, contextual in groups:
        found: dict[str, tuple[str, float]] = {}
        for label, pattern in known.items():
            match = pattern.search(chunk.text)
            if match is not None:
                found[_normalize(label)] = (label, 0.88)
        contextual_patterns = (
            contextual if isinstance(contextual, tuple) else (contextual,)
        )
        for pattern in contextual_patterns:
            for match in pattern.finditer(chunk.text):
                candidate = _clean_label(match.group(1), 120)
                if _generic_name_allowed(candidate):
                    found.setdefault(
                        _normalize(candidate),
                        (candidate, 0.7),
                    )
        for label, confidence in found.values():
            node = _ensure_node(
                db,
                cache,
                node_type=node_type,
                label=label,
            )
            added += int(
                _add_edge(
                    db,
                    source=paper_node,
                    target=node,
                    relation_type=relation_type,
                    evidence=evidence,
                    evidence_text=_evidence_excerpt(chunk.text, label),
                    confidence=confidence,
                    extraction_method=f"{node_type}_lexicon_context_rule",
                )
            )
    return added


def _extract_findings(
    db: Session,
    chunks: list[PaperChunk],
    paper_node: KnowledgeNode,
    cache: dict[tuple[str, str, int | None], KnowledgeNode],
) -> int:
    added = 0
    seen: set[str] = set()
    for chunk in chunks:
        if len(seen) >= 20:
            break
        for sentence in _sentences(chunk.text):
            if len(sentence) < 40 or _FINDING_CUE.search(sentence) is None:
                continue
            normalized = _normalize(sentence)
            if normalized in seen:
                continue
            seen.add(normalized)
            finding_node = _ensure_node(
                db,
                cache,
                node_type="finding",
                label=sentence[:320],
                attributes={
                    "claim_kind": "evidence",
                    "source_paper_id": chunk.paper_id,
                },
            )
            evidence = EvidenceChunk(
                paper_id=chunk.paper_id,
                page_number=chunk.page_number,
                chunk_id=chunk.id,
                text=chunk.text,
            )
            added += int(
                _add_edge(
                    db,
                    source=paper_node,
                    target=finding_node,
                    relation_type="SUPPORTS",
                    evidence=evidence,
                    evidence_text=sentence,
                    confidence=0.76,
                    extraction_method="finding_sentence_cue",
                    attributes={"claim_kind": "evidence"},
                )
            )
    return added


def _extract_citations_and_cross_paper_relations(
    db: Session,
    papers: list[Paper],
    chunks_by_paper: dict[int, list[PaperChunk]],
    paper_nodes: dict[int, KnowledgeNode],
    cache: dict[tuple[str, str, int | None], KnowledgeNode],
) -> tuple[int, list[str]]:
    added = 0
    diagnostics: list[str] = []
    paper_ids = [paper.id for paper in papers]
    references = (
        db.query(CitationReference)
        .filter(CitationReference.source_paper_id.in_(paper_ids))
        .order_by(CitationReference.id.asc())
        .all()
    )
    reference_by_id = {reference.id: reference for reference in references}
    labels_by_reference: dict[tuple[int, int, str], CitationReference] = {}
    for reference in references:
        for label in _citation_labels(reference):
            labels_by_reference[
                (
                    reference.source_paper_id,
                    reference.source_page_number,
                    label,
                )
            ] = reference

    edges = (
        db.query(CitationEdge)
        .filter(CitationEdge.citing_paper_id.in_(paper_ids))
        .order_by(CitationEdge.id.asc())
        .all()
    )
    for edge in edges:
        reference = reference_by_id.get(edge.citation_reference_id)
        if reference is None or edge.cited_paper_id not in paper_nodes:
            continue
        chunk = _chunk_for_reference(
            chunks_by_paper.get(edge.citing_paper_id, []),
            reference,
        )
        if chunk is None:
            diagnostics.append(
                f"参考文献 {reference.id} 未找到可定位 Chunk，未生成 CITES 关系。"
            )
            continue
        evidence = EvidenceChunk(
            paper_id=edge.citing_paper_id,
            page_number=chunk.page_number,
            chunk_id=chunk.id,
            text=chunk.text,
        )
        added += int(
            _add_edge(
                db,
                source=paper_nodes[edge.citing_paper_id],
                target=paper_nodes[edge.cited_paper_id],
                relation_type="CITES",
                evidence=evidence,
                evidence_text=reference.raw_text,
                confidence=edge.match_confidence,
                extraction_method=f"citation_edge_{edge.match_method}",
                attributes={"citation_reference_id": reference.id},
            )
        )

    for paper in papers:
        for chunk in chunks_by_paper.get(paper.id, []):
            for sentence in _sentences(chunk.text):
                relation_type = _semantic_relation(sentence)
                if relation_type is None:
                    continue
                marker_match = _CITATION_MARKER_PATTERN.search(sentence)
                if marker_match is None:
                    continue
                labels = re.findall(r"\d+", marker_match.group(1))
                targets = {
                    labels_by_reference.get(
                        (paper.id, chunk.page_number, label)
                    ).target_paper_id
                    for label in labels
                    if labels_by_reference.get(
                        (paper.id, chunk.page_number, label)
                    )
                    is not None
                }
                for target_paper_id in targets:
                    if target_paper_id is None or target_paper_id not in paper_nodes:
                        continue
                    evidence = EvidenceChunk(
                        paper_id=paper.id,
                        page_number=chunk.page_number,
                        chunk_id=chunk.id,
                        text=chunk.text,
                    )
                    added += int(
                        _add_edge(
                            db,
                            source=paper_nodes[paper.id],
                            target=paper_nodes[target_paper_id],
                            relation_type=relation_type,
                            evidence=evidence,
                            evidence_text=sentence,
                            confidence=0.78,
                            extraction_method="citation_context_cue_rule",
                        )
                    )
    return added, diagnostics


def rebuild_knowledge_graph(
    db: Session,
    paper_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    all_papers = db.query(Paper).order_by(Paper.id.asc()).all()
    if not all_papers:
        raise AppError(
            "本地论文库为空，无法构建科研知识图谱。",
            code="KNOWLEDGE_GRAPH_EMPTY_LIBRARY",
            status_code=409,
        )
    requested_ids = (
        sorted(set(int(value) for value in paper_ids))
        if paper_ids is not None
        else [paper.id for paper in all_papers]
    )
    paper_by_id = {paper.id: paper for paper in all_papers}
    missing = [paper_id for paper_id in requested_ids if paper_id not in paper_by_id]
    if missing:
        raise AppError(
            "部分论文不存在，未执行知识图谱重建。",
            code="KNOWLEDGE_GRAPH_PAPER_NOT_FOUND",
            status_code=404,
            details={"paper_ids": missing},
        )
    papers = [paper_by_id[paper_id] for paper_id in requested_ids]
    run = KnowledgeExtractionRun(
        paper_ids_json=_json_dump(requested_ids),
        paper_count=len(papers),
        status="running",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        db.query(KnowledgeEdge).filter(
            KnowledgeEdge.paper_id.in_(requested_ids)
        ).delete(synchronize_session=False)
        db.flush()

        chunks = (
            db.query(PaperChunk)
            .filter(PaperChunk.paper_id.in_(requested_ids))
            .order_by(
                PaperChunk.paper_id.asc(),
                PaperChunk.page_number.asc(),
                PaperChunk.chunk_index.asc(),
                PaperChunk.id.asc(),
            )
            .all()
        )
        chunks_by_paper: dict[int, list[PaperChunk]] = defaultdict(list)
        for chunk in chunks:
            chunks_by_paper[chunk.paper_id].append(chunk)

        cache: dict[tuple[str, str, int | None], KnowledgeNode] = {}
        paper_nodes = {
            paper.id: _paper_node(db, cache, paper)
            for paper in papers
        }
        diagnostics: list[str] = []
        skipped_chunk_count = 0
        for paper in papers:
            paper_chunks = chunks_by_paper.get(paper.id, [])
            if not paper_chunks:
                diagnostics.append(
                    f"论文 {paper.id} 没有可定位 Chunk，仅建立 Paper 节点。"
                )
                continue
            _extract_authors(
                db,
                chunks_by_paper,
                paper,
                paper_nodes[paper.id],
                cache,
            )
            _extract_institutions(
                db,
                paper_chunks,
                paper,
                paper_nodes[paper.id],
                cache,
            )
            _extract_findings(
                db,
                paper_chunks,
                paper_nodes[paper.id],
                cache,
            )
            for chunk in paper_chunks:
                if not (chunk.text or "").strip():
                    skipped_chunk_count += 1
                    continue
                _extract_topics(
                    db,
                    chunk,
                    paper_nodes[paper.id],
                    cache,
                )
                _extract_named_entities(
                    db,
                    chunk,
                    paper_nodes[paper.id],
                    cache,
                )
        _, citation_diagnostics = _extract_citations_and_cross_paper_relations(
            db,
            papers,
            chunks_by_paper,
            paper_nodes,
            cache,
        )
        diagnostics.extend(citation_diagnostics)
        db.flush()

        active_ids = {
            source_id
            for (source_id,) in db.query(KnowledgeEdge.source_node_id).all()
        } | {
            target_id
            for (target_id,) in db.query(KnowledgeEdge.target_node_id).all()
        }
        orphan_nodes = (
            db.query(KnowledgeNode)
            .filter(
                KnowledgeNode.node_type != "paper",
                ~KnowledgeNode.id.in_(active_ids),
            )
            .all()
            if active_ids
            else []
        )
        for node in orphan_nodes:
            db.delete(node)

        db.flush()
        run = db.get(KnowledgeExtractionRun, run.id)
        if run is None:
            raise RuntimeError("Knowledge extraction run disappeared.")
        run.status = "completed"
        run.node_count = db.query(KnowledgeNode).count()
        run.edge_count = db.query(KnowledgeEdge).count()
        run.skipped_chunk_count = skipped_chunk_count
        run.diagnostics_json = _json_dump(diagnostics)
        run.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(KnowledgeExtractionRun, run.id)
        if run is not None:
            run.status = "failed"
            run.error_message = getattr(exc, "message", str(exc))[:4000]
            run.completed_at = datetime.utcnow()
            db.commit()
        raise

    return {
        "run_id": run.id,
        "status": run.status,
        "paper_ids": requested_ids,
        "paper_count": run.paper_count,
        "node_count": run.node_count,
        "edge_count": run.edge_count,
        "skipped_chunk_count": run.skipped_chunk_count,
        "diagnostics": diagnostics,
        "extraction_version": EXTRACTION_VERSION,
    }
