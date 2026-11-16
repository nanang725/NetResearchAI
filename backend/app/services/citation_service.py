from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.citation import CitationEdge, CitationReference
from app.models.paper import Paper, PaperElement, PaperVersion


CITATION_PARSER_VERSION = "1"
GRAPH_DISCLAIMER = (
    "核心论文来自当前本地论文语料的引用图计算，不代表整个学术领域的权威排名。"
)

_DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[^\s,;，。；]+", re.IGNORECASE)
_ARXIV_PATTERN = re.compile(
    r"(?:arxiv\s*:\s*)?("
    r"(?:[a-z-]+/\d{7})|(?:\d{4}\.\d{4,5})"
    r")(?:v\d+)?",
    re.IGNORECASE,
)
_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")
_LEADING_ENTRY_PATTERN = re.compile(r"^\s*\[?(\d+)\]?[.)、]?\s+")
_ENTRY_START_PATTERN = re.compile(r"(?m)^\s*(?:\[\d+\]|\d+[.)、])\s+")


@dataclass
class PaperCandidate:
    canonical_id: int
    papers: list[Paper]
    normalized_titles: set[str]
    dois: set[str]
    arxiv_ids: set[str]
    years: set[int]
    authors: list[str]


def normalize_title(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = _LEADING_ENTRY_PATTERN.sub("", normalized)
    return "".join(
        character
        for character in normalized
        if character.isalnum()
    )


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().casefold()
    normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", normalized)
    normalized = re.sub(r"^doi\s*:\s*", "", normalized)
    normalized = normalized.rstrip(".,;)")
    return normalized or None


def normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _ARXIV_PATTERN.search(value)
    if match is None:
        return None
    return match.group(1).casefold()


def split_reference_entries(text: str) -> list[str]:
    """Split a reference element conservatively into individual entries."""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    starts = list(_ENTRY_START_PATTERN.finditer(normalized))
    if len(starts) > 1:
        entries: list[str] = []
        for index, match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(normalized)
            value = normalized[match.start():end].strip()
            if value:
                entries.append(value)
        return entries
    paragraphs = [
        re.sub(r"\s+", " ", value).strip()
        for value in re.split(r"\n\s*\n", normalized)
        if value.strip()
    ]
    if len(paragraphs) > 1:
        return paragraphs
    return [re.sub(r"\s+", " ", normalized).strip()]


def extract_reference_fields(raw_text: str) -> dict:
    doi_match = _DOI_PATTERN.search(raw_text)
    doi = normalize_doi(doi_match.group(0) if doi_match else None)
    arxiv_match = _ARXIV_PATTERN.search(raw_text)
    arxiv_id = normalize_arxiv_id(arxiv_match.group(0) if arxiv_match else None)
    years = [int(value) for value in _YEAR_PATTERN.findall(raw_text)]
    year = years[-1] if years else None

    cleaned = raw_text
    if doi_match:
        cleaned = cleaned.replace(doi_match.group(0), " ")
    if arxiv_match:
        cleaned = cleaned.replace(arxiv_match.group(0), " ")
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = _LEADING_ENTRY_PATTERN.sub("", cleaned)
    segments = [
        re.sub(r"\s+", " ", segment).strip(" .;，。")
        for segment in re.split(r"[.;。]\s*", cleaned)
        if segment.strip(" .;，。")
    ]
    title_candidates = [
        segment
        for segment in segments
        if (
            len(re.findall(r"[A-Za-z]{2,}", segment)) >= 4
            or len(
                re.findall(
                    r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+\b",
                    segment,
                )
            )
            or len(re.findall(r"[\u3400-\u9fff]", segment)) >= 8
        )
        and len(segment) <= 500
    ]
    title = max(title_candidates, key=len) if title_candidates else None
    authors = segments[0] if segments and segments[0] != title else None
    if authors and len(authors) > 1000:
        authors = authors[:1000]

    return {
        "title": title,
        "normalized_title": normalize_title(title) or None,
        "authors": authors,
        "year": year,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "venue": None,
    }


def _paper_text_fields(paper: Paper) -> str:
    return "\n".join(
        value
        for value in (
            paper.title,
            paper.original_filename,
            paper.doi,
            paper.pdf_metadata,
        )
        if value
    )


def _split_authors(value: str | None) -> list[str]:
    if not value:
        return []
    return [
        item.strip()
        for item in re.split(r"[,，;、]", value)
        if item.strip()
    ]


def _canonical_candidates(db: Session) -> tuple[dict[int, PaperCandidate], dict[int, int]]:
    papers = db.query(Paper).order_by(Paper.id.asc()).all()
    versions = db.query(PaperVersion).order_by(PaperVersion.paper_id.asc()).all()
    by_paper = {version.paper_id: version for version in versions}
    groups: dict[str, list[PaperVersion]] = defaultdict(list)
    for version in versions:
        groups[version.group_id].append(version)
    canonical_by_version_group: dict[str, int] = {}
    for group_id, members in groups.items():
        retained = [member for member in members if member.retained]
        selected = min(
            retained or members,
            key=lambda member: (member.version_number, member.paper_id),
        )
        canonical_by_version_group[group_id] = selected.paper_id

    canonical_map: dict[int, int] = {}
    members_by_canonical: dict[int, list[Paper]] = defaultdict(list)
    paper_map = {paper.id: paper for paper in papers}
    for paper in papers:
        version = by_paper.get(paper.id)
        canonical_id = (
            canonical_by_version_group[version.group_id]
            if version is not None
            else paper.id
        )
        canonical_map[paper.id] = canonical_id
        members_by_canonical[canonical_id].append(paper)

    candidates: dict[int, PaperCandidate] = {}
    for canonical_id, members in members_by_canonical.items():
        if canonical_id not in paper_map:
            canonical_id = min(paper.id for paper in members)
        normalized_titles = {
            normalize_title(paper.normalized_title or paper.title)
            for paper in members
            if normalize_title(paper.normalized_title or paper.title)
        }
        dois = {
            doi
            for paper in members
            if (doi := normalize_doi(paper.doi))
        }
        arxiv_ids = {
            arxiv_id
            for paper in members
            if (arxiv_id := normalize_arxiv_id(_paper_text_fields(paper)))
        }
        years = {
            int(paper.publication_year)
            for paper in members
            if paper.publication_year
        }
        authors = [
            author
            for paper in members
            for author in _split_authors(paper.authors)
        ]
        candidates[canonical_id] = PaperCandidate(
            canonical_id=canonical_id,
            papers=members,
            normalized_titles=normalized_titles,
            dois=dois,
            arxiv_ids=arxiv_ids,
            years=years,
            authors=authors,
        )
    return candidates, canonical_map


def _match_reference(
    reference: CitationReference,
    source_canonical_id: int,
    candidates: dict[int, PaperCandidate],
) -> None:
    reference.target_paper_id = None
    reference.match_status = "unmatched"
    reference.match_method = None
    reference.match_confidence = 0.0

    exact_matches: list[tuple[int, str, float]] = []
    reference_doi = normalize_doi(reference.doi)
    reference_arxiv = normalize_arxiv_id(reference.arxiv_id)
    normalized_reference_text = normalize_title(reference.raw_text)
    source_candidate = candidates.get(source_canonical_id)
    if source_candidate is not None:
        source_self_match = (
            bool(reference_doi and reference_doi in source_candidate.dois)
            or bool(
                reference_arxiv
                and reference_arxiv in source_candidate.arxiv_ids
            )
            or any(
                title
                and len(title) >= 10
                and title in normalized_reference_text
                for title in source_candidate.normalized_titles
            )
        )
        if source_self_match:
            reference.match_status = "self_reference"
            reference.match_method = "self"
            return

    for candidate in candidates.values():
        if candidate.canonical_id == source_canonical_id:
            continue
        if reference_doi and reference_doi in candidate.dois:
            exact_matches.append((candidate.canonical_id, "doi", 1.0))
            continue
        if reference_arxiv and reference_arxiv in candidate.arxiv_ids:
            exact_matches.append((candidate.canonical_id, "arxiv", 1.0))
            continue
        if any(
            title
            and len(title) >= 10
            and title in normalized_reference_text
            for title in candidate.normalized_titles
        ):
            exact_matches.append((candidate.canonical_id, "title_exact", 0.98))

    unique_exact = {
        (target_id, method, confidence)
        for target_id, method, confidence in exact_matches
    }
    if len(unique_exact) == 1:
        target_id, method, confidence = next(iter(unique_exact))
        if target_id == source_canonical_id:
            reference.match_status = "self_reference"
            reference.match_method = method
            return
        reference.target_paper_id = target_id
        reference.match_status = "exact"
        reference.match_method = method
        reference.match_confidence = confidence
        return
    if len(unique_exact) > 1:
        reference.match_status = "ambiguous"
        reference.match_method = "multiple_exact"
        return

    reference_title = normalize_title(reference.title or reference.normalized_title)
    if not reference_title:
        return
    scored: list[tuple[float, int]] = []
    for candidate in candidates.values():
        if candidate.canonical_id == source_canonical_id:
            continue
        title_score = max(
            (
                difflib.SequenceMatcher(
                    None,
                    reference_title,
                    candidate_title,
                ).ratio()
                for candidate_title in candidate.normalized_titles
            ),
            default=0.0,
        )
        year_match = bool(reference.year and reference.year in candidate.years)
        author_match = bool(
            reference.authors
            and any(
                normalize_title(author) in normalize_title(reference.authors)
                for author in candidate.authors
                if normalize_title(author)
            )
        )
        if title_score >= 0.96 or (
            title_score >= 0.88 and (year_match or author_match)
        ):
            confidence = min(
                0.99,
                title_score
                + (0.02 if year_match else 0.0)
                + (0.02 if author_match else 0.0),
            )
            scored.append((confidence, candidate.canonical_id))
    scored.sort(reverse=True)
    if not scored:
        return
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.03:
        reference.match_status = "ambiguous"
        reference.match_method = "title_similarity"
        return
    confidence, target_id = scored[0]
    if target_id == source_canonical_id:
        reference.match_status = "self_reference"
        reference.match_method = "title_similarity"
        return
    reference.target_paper_id = target_id
    reference.match_status = "high_confidence"
    reference.match_method = "title_similarity"
    reference.match_confidence = round(confidence, 4)


def _sync_edges(db: Session, canonical_map: dict[int, int]) -> None:
    db.query(CitationEdge).delete(synchronize_session=False)
    references = (
        db.query(CitationReference)
        .filter(
            CitationReference.match_status.in_(("exact", "high_confidence")),
            CitationReference.target_paper_id.isnot(None),
        )
        .order_by(
            CitationReference.match_confidence.desc(),
            CitationReference.id.asc(),
        )
        .all()
    )
    selected_by_pair: dict[tuple[int, int], CitationReference] = {}
    for reference in references:
        citing_id = canonical_map.get(reference.source_paper_id, reference.source_paper_id)
        cited_id = canonical_map.get(
            reference.target_paper_id,
            reference.target_paper_id,
        )
        if citing_id == cited_id:
            continue
        selected_by_pair.setdefault((citing_id, cited_id), reference)
    for (citing_id, cited_id), reference in selected_by_pair.items():
        db.add(
            CitationEdge(
                citing_paper_id=citing_id,
                cited_paper_id=cited_id,
                citation_reference_id=reference.id,
                match_method=reference.match_method or "unknown",
                match_confidence=reference.match_confidence,
            )
        )


def rebuild_citations(
    db: Session,
    paper_ids: list[int] | None = None,
) -> dict:
    all_papers = db.query(Paper).order_by(Paper.id.asc()).all()
    known_ids = {paper.id for paper in all_papers}
    selected_ids = sorted(set(paper_ids)) if paper_ids is not None else sorted(known_ids)
    missing = [paper_id for paper_id in selected_ids if paper_id not in known_ids]
    if missing:
        raise AppError(
            "指定论文不存在。",
            code="PAPER_NOT_FOUND",
            status_code=404,
            details={"paper_ids": missing},
        )

    if paper_ids is None:
        db.query(CitationEdge).delete(synchronize_session=False)
        db.query(CitationReference).delete(synchronize_session=False)
    elif selected_ids:
        db.query(CitationEdge).filter(
            CitationEdge.citing_paper_id.in_(selected_ids)
        ).delete(synchronize_session=False)
        db.query(CitationReference).filter(
            CitationReference.source_paper_id.in_(selected_ids)
        ).delete(synchronize_session=False)

    for paper_id in selected_ids:
        elements = (
            db.query(PaperElement)
            .filter(
                PaperElement.paper_id == paper_id,
                PaperElement.element_type == "reference",
            )
            .order_by(
                PaperElement.page_number.asc(),
                PaperElement.element_index.asc(),
            )
            .all()
        )
        for element in elements:
            entries = split_reference_entries(element.text)
            for entry_index, raw_text in enumerate(entries):
                fields = extract_reference_fields(raw_text)
                db.add(
                    CitationReference(
                        source_paper_id=paper_id,
                        source_page_id=element.page_id,
                        source_element_id=element.id,
                        source_page_number=element.page_number,
                        source_entry_index=entry_index,
                        raw_text=raw_text,
                        normalized_text=normalize_title(raw_text),
                        title=fields["title"],
                        normalized_title=fields["normalized_title"],
                        authors=fields["authors"],
                        year=fields["year"],
                        doi=fields["doi"],
                        arxiv_id=fields["arxiv_id"],
                        venue=fields["venue"],
                        parser_version=CITATION_PARSER_VERSION,
                    )
                )
    db.flush()

    candidates, canonical_map = _canonical_candidates(db)
    references = (
        db.query(CitationReference)
        .order_by(CitationReference.id.asc())
        .all()
    )
    for reference in references:
        source_canonical_id = canonical_map.get(
            reference.source_paper_id,
            reference.source_paper_id,
        )
        _match_reference(reference, source_canonical_id, candidates)
    db.flush()
    _sync_edges(db, canonical_map)
    db.commit()
    return citation_statistics(db)


def citation_statistics(db: Session) -> dict:
    references = db.query(CitationReference).all()
    matched_count = sum(
        reference.match_status in {"exact", "high_confidence"}
        and reference.target_paper_id is not None
        for reference in references
    )
    ambiguous_count = sum(
        reference.match_status == "ambiguous"
        and reference.target_paper_id is None
        for reference in references
    )
    return {
        "reference_count": len(references),
        "matched_reference_count": matched_count,
        "ambiguous_reference_count": ambiguous_count,
        "unmatched_reference_count": (
            len(references) - matched_count - ambiguous_count
        ),
        "edge_count": db.query(CitationEdge).count(),
    }


def compute_pagerank(
    node_ids: list[int],
    edges: list[tuple[int, int]],
    *,
    damping: float = 0.85,
    max_iterations: int = 100,
    tolerance: float = 1e-10,
) -> dict[int, float]:
    if not node_ids:
        return {}
    node_set = set(node_ids)
    outgoing: dict[int, list[int]] = {node_id: [] for node_id in node_ids}
    for source, target in edges:
        if source in node_set and target in node_set:
            outgoing[source].append(target)
    count = len(node_ids)
    ranks = {node_id: 1.0 / count for node_id in node_ids}
    for _ in range(max_iterations):
        dangling = sum(
            ranks[node_id]
            for node_id in node_ids
            if not outgoing[node_id]
        )
        next_ranks = {
            node_id: (1.0 - damping) / count + damping * dangling / count
            for node_id in node_ids
        }
        for source in node_ids:
            targets = outgoing[source]
            if not targets:
                continue
            share = damping * ranks[source] / len(targets)
            for target in targets:
                next_ranks[target] += share
        delta = sum(
            abs(next_ranks[node_id] - ranks[node_id])
            for node_id in node_ids
        )
        ranks = next_ranks
        if delta < tolerance:
            break
    return ranks


def _graph_metrics(
    db: Session,
) -> tuple[
    dict[int, PaperCandidate],
    list[CitationEdge],
    dict[int, int],
    dict[int, int],
    dict[int, float],
    dict[int, int],
    set[int],
]:
    candidates, _canonical_map = _canonical_candidates(db)
    edges = db.query(CitationEdge).order_by(CitationEdge.id.asc()).all()
    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    edge_pairs: list[tuple[int, int]] = []
    for edge in edges:
        incoming[edge.cited_paper_id] += 1
        outgoing[edge.citing_paper_id] += 1
        edge_pairs.append((edge.citing_paper_id, edge.cited_paper_id))
    node_ids = sorted(candidates)
    ranks = compute_pagerank(node_ids, edge_pairs)
    ordered = sorted(
        node_ids,
        key=lambda paper_id: (
            ranks.get(paper_id, 0.0),
            incoming[paper_id],
        ),
        reverse=True,
    )
    rank_by_id = {
        paper_id: index + 1
        for index, paper_id in enumerate(ordered)
    }
    connected = sorted(
        [
            paper_id
            for paper_id in node_ids
            if incoming[paper_id] or outgoing[paper_id]
        ],
        key=lambda paper_id: (
            ranks.get(paper_id, 0.0),
            incoming[paper_id],
        ),
        reverse=True,
    )
    core_count = (
        max(1, min(5, math.ceil(len(connected) * 0.2)))
        if connected
        else 0
    )
    foundational = set(connected[:core_count])
    return (
        candidates,
        edges,
        dict(incoming),
        dict(outgoing),
        ranks,
        rank_by_id,
        foundational,
    )


def get_citation_graph(
    db: Session,
    *,
    limit: int = 200,
    min_citations: int = 0,
    paper_id: int | None = None,
    include_isolated: bool = True,
) -> dict:
    (
        candidates,
        edges,
        incoming,
        outgoing,
        ranks,
        rank_by_id,
        foundational,
    ) = _graph_metrics(db)
    selected_ids = set(candidates)
    if paper_id is not None:
        if paper_id not in candidates:
            raise AppError(
                "论文不存在。",
                code="PAPER_NOT_FOUND",
                status_code=404,
            )
        selected_ids = {paper_id}
        selected_ids.update(
            edge.cited_paper_id
            for edge in edges
            if edge.citing_paper_id == paper_id
        )
        selected_ids.update(
            edge.citing_paper_id
            for edge in edges
            if edge.cited_paper_id == paper_id
        )
    if not include_isolated:
        selected_ids = {
            candidate_id
            for candidate_id in selected_ids
            if incoming.get(candidate_id, 0) or outgoing.get(candidate_id, 0)
        }
    selected_ids = {
        candidate_id
        for candidate_id in selected_ids
        if incoming.get(candidate_id, 0) >= min_citations
    }
    ordered_ids = sorted(
        selected_ids,
        key=lambda candidate_id: (
            ranks.get(candidate_id, 0.0),
            incoming.get(candidate_id, 0),
            -candidate_id,
        ),
        reverse=True,
    )[: max(1, min(limit, 1000))]
    allowed = set(ordered_ids)

    nodes = []
    for candidate_id in ordered_ids:
        candidate = candidates[candidate_id]
        representative = next(
            (
                paper
                for paper in candidate.papers
                if paper.id == candidate_id
            ),
            candidate.papers[0],
        )
        reasons = []
        if candidate_id in foundational:
            reasons = [
                f"PageRank 排名第 {rank_by_id[candidate_id]}",
                f"本地论文入度 {incoming.get(candidate_id, 0)}",
                "位于当前本地引用网络核心组",
            ]
        nodes.append(
            {
                "id": str(candidate_id),
                "paper_id": candidate_id,
                "title": representative.title,
                "year": representative.publication_year,
                "authors": _split_authors(representative.authors),
                "in_degree": incoming.get(candidate_id, 0),
                "out_degree": outgoing.get(candidate_id, 0),
                "pagerank": round(ranks.get(candidate_id, 0.0), 8),
                "rank": rank_by_id.get(candidate_id, len(candidates) + 1),
                "is_foundational": candidate_id in foundational,
                "foundational_reasons": reasons,
            }
        )
    graph_edges = [
        {
            "id": edge.id,
            "source": str(edge.citing_paper_id),
            "target": str(edge.cited_paper_id),
            "reference_id": edge.citation_reference_id,
            "raw_text": edge.reference.raw_text,
            "source_page": edge.reference.source_page_number,
            "match_method": edge.match_method,
            "match_confidence": edge.match_confidence,
        }
        for edge in edges
        if edge.citing_paper_id in allowed and edge.cited_paper_id in allowed
    ]
    stats = citation_statistics(db)
    stats.update(
        {
            "paper_count": len(candidates),
            "displayed_node_count": len(nodes),
            "displayed_edge_count": len(graph_edges),
            "independent_paper_count": len(candidates),
            "foundational_paper_count": len(foundational),
        }
    )
    return {
        "nodes": nodes,
        "edges": graph_edges,
        "stats": stats,
        "disclaimer": GRAPH_DISCLAIMER,
    }


def get_citation_paper_detail(db: Session, paper_id: int) -> dict:
    (
        candidates,
        edges,
        incoming,
        outgoing,
        ranks,
        rank_by_id,
        foundational,
    ) = _graph_metrics(db)
    if paper_id not in candidates:
        raise AppError(
            "论文不存在。",
            code="PAPER_NOT_FOUND",
            status_code=404,
        )
    candidate = candidates[paper_id]
    representative = candidate.papers[0]
    outgoing_relations = []
    incoming_relations = []
    for edge in edges:
        if edge.citing_paper_id == paper_id:
            target = candidates.get(edge.cited_paper_id)
            if target is None:
                continue
            target_paper = target.papers[0]
            outgoing_relations.append(
                {
                    "paper_id": target.canonical_id,
                    "title": target_paper.title,
                    "year": target_paper.publication_year,
                    "reference_id": edge.citation_reference_id,
                    "raw_reference": edge.reference.raw_text,
                    "source_page": edge.reference.source_page_number,
                    "match_method": edge.match_method,
                    "match_confidence": edge.match_confidence,
                }
            )
        if edge.cited_paper_id == paper_id:
            source = candidates.get(edge.citing_paper_id)
            if source is None:
                continue
            source_paper = source.papers[0]
            incoming_relations.append(
                {
                    "paper_id": source.canonical_id,
                    "title": source_paper.title,
                    "year": source_paper.publication_year,
                    "reference_id": edge.citation_reference_id,
                    "raw_reference": edge.reference.raw_text,
                    "source_page": edge.reference.source_page_number,
                    "match_method": edge.match_method,
                    "match_confidence": edge.match_confidence,
                }
            )
    return {
        "paper_id": paper_id,
        "title": representative.title,
        "year": representative.publication_year,
        "in_degree": incoming.get(paper_id, 0),
        "out_degree": outgoing.get(paper_id, 0),
        "pagerank": round(ranks.get(paper_id, 0.0), 8),
        "rank": rank_by_id.get(paper_id, len(candidates) + 1),
        "is_foundational": paper_id in foundational,
        "foundational_reasons": (
            [
                f"PageRank 排名第 {rank_by_id[paper_id]}",
                f"本地论文入度 {incoming.get(paper_id, 0)}",
                "位于当前本地引用网络核心组",
            ]
            if paper_id in foundational
            else []
        ),
        "outgoing": outgoing_relations,
        "incoming": incoming_relations,
    }
