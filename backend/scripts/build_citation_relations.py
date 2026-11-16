from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models  # noqa: E402,F401
from app.database import ensure_additive_schema  # noqa: E402
from app.models.citation import CitationEdge, CitationReference
from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage  # noqa: E402
from app.services import citation_service  # noqa: E402


REPORT_VERSION = "citation-relations-staging-v1"
REFERENCE_FIELDS = (
    "doi",
    "arxiv_id",
    "title",
    "authors",
    "year",
)
PAPER_FIELDS = (
    "doi",
    "arxiv_id",
    "publication_year",
    "title",
    "authors",
    "venue",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _table_names(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _build_engine(db_path: Path):
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _base_content_snapshot(db: Session) -> dict[str, Any]:
    papers = [
        {
            "id": paper.id,
            "title": paper.title,
            "file_hash": paper.file_hash,
            "page_count": paper.page_count,
        }
        for paper in db.query(Paper).order_by(Paper.id).all()
    ]
    pages = [
        {
            "id": page.id,
            "paper_id": page.paper_id,
            "page_number": page.page_number,
            "text_sha256": hashlib.sha256(page.text.encode("utf-8")).hexdigest(),
        }
        for page in db.query(PaperPage).order_by(PaperPage.id).all()
    ]
    elements = [
        {
            "id": element.id,
            "paper_id": element.paper_id,
            "page_id": element.page_id,
            "page_number": element.page_number,
            "element_index": element.element_index,
            "element_type": element.element_type,
            "text_sha256": hashlib.sha256(
                element.text.encode("utf-8")
            ).hexdigest(),
        }
        for element in db.query(PaperElement).order_by(PaperElement.id).all()
    ]
    chunks = [
        {
            "id": chunk.id,
            "paper_id": chunk.paper_id,
            "page_number": chunk.page_number,
            "page_chunk_index": chunk.page_chunk_index,
            "chunk_index": chunk.chunk_index,
            "element_id": chunk.element_id,
            "text_sha256": hashlib.sha256(
                chunk.text.encode("utf-8")
            ).hexdigest(),
        }
        for chunk in db.query(PaperChunk).order_by(PaperChunk.id).all()
    ]
    return {
        "paper_count": len(papers),
        "page_count": len(pages),
        "element_count": len(elements),
        "reference_element_count": sum(
            element["element_type"] == "reference" for element in elements
        ),
        "chunk_count": len(chunks),
        "papers_sha256": hashlib.sha256(
            json.dumps(papers, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "pages_sha256": hashlib.sha256(
            json.dumps(pages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "elements_sha256": hashlib.sha256(
            json.dumps(elements, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "chunks_sha256": hashlib.sha256(
            json.dumps(chunks, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _field_present(value: Any) -> bool:
    return value is not None and value != "" and value != []


def _reference_snapshot(db: Session) -> list[dict[str, Any]]:
    references = (
        db.query(CitationReference)
        .order_by(
            CitationReference.source_paper_id,
            CitationReference.source_page_number,
            CitationReference.source_element_id,
            CitationReference.source_entry_index,
        )
        .all()
    )
    return [
        {
            "source_paper_id": reference.source_paper_id,
            "source_element_id": reference.source_element_id,
            "source_entry_index": reference.source_entry_index,
            "raw_text_sha256": hashlib.sha256(
                reference.raw_text.encode("utf-8")
            ).hexdigest(),
            "title": reference.title,
            "authors": reference.authors,
            "year": reference.year,
            "doi": reference.doi,
            "arxiv_id": reference.arxiv_id,
            "target_paper_id": reference.target_paper_id,
            "match_status": reference.match_status,
            "match_method": reference.match_method,
            "match_confidence": reference.match_confidence,
        }
        for reference in references
    ]


def _edge_snapshot(db: Session) -> list[dict[str, Any]]:
    edges = db.query(CitationEdge).order_by(CitationEdge.id).all()
    return [
        {
            "citing_paper_id": edge.citing_paper_id,
            "cited_paper_id": edge.cited_paper_id,
            "source_element_id": edge.reference.source_element_id,
            "source_entry_index": edge.reference.source_entry_index,
            "match_method": edge.match_method,
            "match_confidence": edge.match_confidence,
        }
        for edge in edges
    ]


def _logical_snapshot(db: Session) -> dict[str, Any]:
    references = _reference_snapshot(db)
    edges = _edge_snapshot(db)
    return {
        "references": references,
        "edges": edges,
        "references_sha256": hashlib.sha256(
            json.dumps(
                references,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "edges_sha256": hashlib.sha256(
            json.dumps(edges, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _paper_metadata(db: Session) -> list[dict[str, Any]]:
    papers = db.query(Paper).order_by(Paper.id).all()
    metadata = []
    for paper in papers:
        normalized_doi = citation_service.normalize_doi(paper.doi)
        arxiv_id = citation_service.normalize_arxiv_id(
            citation_service._paper_text_fields(paper)
        )
        values = {
            "doi": normalized_doi,
            "arxiv_id": arxiv_id,
            "publication_year": paper.publication_year,
            "title": paper.title,
            "authors": paper.authors,
            "venue": paper.venue,
        }
        metadata.append(
            {
                "paper_id": paper.id,
                "title": paper.title,
                "original_filename": paper.original_filename,
                "doi": paper.doi,
                "normalized_doi": normalized_doi,
                "arxiv_id": arxiv_id,
                "publication_year": paper.publication_year,
                "authors": paper.authors,
                "venue": paper.venue,
                "field_presence": {
                    field: _field_present(values[field])
                    for field in PAPER_FIELDS
                },
            }
        )
    return metadata


def _extraction_quality(
    db: Session,
    references: list[CitationReference],
) -> dict[str, Any]:
    total = len(references)
    field_counts = {
        field: sum(_field_present(getattr(reference, field)) for reference in references)
        for field in REFERENCE_FIELDS
    }
    raw_counts = Counter(reference.raw_text for reference in references)
    title_lengths = [
        len(reference.title)
        for reference in references
        if reference.title
    ]
    by_paper: dict[str, dict[str, Any]] = {}
    for paper_id in sorted({reference.source_paper_id for reference in references}):
        selected = [
            reference
            for reference in references
            if reference.source_paper_id == paper_id
        ]
        by_paper[str(paper_id)] = {
            "reference_entry_count": len(selected),
            "field_counts": {
                field: sum(
                    _field_present(getattr(reference, field))
                    for reference in selected
                )
                for field in REFERENCE_FIELDS
            },
            "short_raw_under_20_chars": sum(
                len(reference.raw_text.strip()) < 20 for reference in selected
            ),
            "duplicate_raw_extra_rows": sum(
                count - 1
                for count in Counter(
                    reference.raw_text for reference in selected
                ).values()
                if count > 1
            ),
        }
    return {
        "reference_entry_count": total,
        "field_counts": field_counts,
        "field_coverage": {
            field: round(count / total, 4) if total else 0.0
            for field, count in field_counts.items()
        },
        "raw_unique_count": len(raw_counts),
        "duplicate_raw_extra_rows": sum(
            count - 1 for count in raw_counts.values() if count > 1
        ),
        "short_raw_under_20_chars": sum(
            len(reference.raw_text.strip()) < 20 for reference in references
        ),
        "title_length": {
            "min": min(title_lengths) if title_lengths else 0,
            "max": max(title_lengths) if title_lengths else 0,
            "average": (
                round(sum(title_lengths) / len(title_lengths), 1)
                if title_lengths
                else 0
            ),
        },
        "by_paper": by_paper,
        "quality_notes": [
            (
                "字段有值不等于字段语义正确；当前统计只表示非空覆盖率。"
            ),
            (
                "短碎片和重复原文不会被额外过滤，因为本脚本只调用现有 "
                "citation_service，不改变其解析或匹配规则。"
            ),
        ],
    }


def _relation_evidence(
    db: Session,
    references: list[CitationReference],
) -> dict[str, Any]:
    papers = {
        paper.id: paper
        for paper in db.query(Paper).order_by(Paper.id).all()
    }

    def serialize(reference: CitationReference) -> dict[str, Any]:
        target = (
            papers.get(reference.target_paper_id)
            if reference.target_paper_id is not None
            else None
        )
        return {
            "reference_id": reference.id,
            "source_paper_id": reference.source_paper_id,
            "source_title": papers[reference.source_paper_id].title,
            "source_element_id": reference.source_element_id,
            "source_page": reference.source_page_number,
            "source_entry_index": reference.source_entry_index,
            "raw_text": reference.raw_text,
            "extracted": {
                "title": reference.title,
                "authors": reference.authors,
                "year": reference.year,
                "doi": reference.doi,
                "arxiv_id": reference.arxiv_id,
            },
            "match_status": reference.match_status,
            "match_method": reference.match_method,
            "match_confidence": reference.match_confidence,
            "target_paper_id": reference.target_paper_id,
            "target_title": target.title if target is not None else None,
            "target_doi": target.doi if target is not None else None,
            "target_arxiv_id": (
                citation_service.normalize_arxiv_id(
                    citation_service._paper_text_fields(target)
                )
                if target is not None
                else None
            ),
        }

    matched = [
        serialize(reference)
        for reference in references
        if reference.match_status in {"exact", "high_confidence"}
        and reference.target_paper_id is not None
    ]
    ambiguous = [
        serialize(reference)
        for reference in references
        if reference.match_status == "ambiguous"
    ]
    self_references = [
        serialize(reference)
        for reference in references
        if reference.match_status == "self_reference"
    ]
    unmatched = [
        serialize(reference)
        for reference in references
        if reference.match_status == "unmatched"
    ]
    return {
        "matched": matched,
        "ambiguous": ambiguous,
        "self_references": self_references,
        "unmatched": unmatched,
    }


def _run_build(db: Session) -> dict[str, Any]:
    before = _base_content_snapshot(db)
    service_statistics = citation_service.rebuild_citations(db)
    references = (
        db.query(CitationReference)
        .order_by(
            CitationReference.source_paper_id,
            CitationReference.source_page_number,
            CitationReference.source_element_id,
            CitationReference.source_entry_index,
        )
        .all()
    )
    edges = db.query(CitationEdge).order_by(CitationEdge.id).all()
    after = _base_content_snapshot(db)
    status_counts = Counter(reference.match_status for reference in references)
    statistics = {
        "reference_count": len(references),
        "matched_reference_count": sum(
            reference.match_status in {"exact", "high_confidence"}
            and reference.target_paper_id is not None
            for reference in references
        ),
        "unmatched_reference_count": status_counts.get("unmatched", 0),
        "ambiguous_reference_count": status_counts.get("ambiguous", 0),
        "self_reference_count": status_counts.get("self_reference", 0),
        "edge_count": len(edges),
        "reference_status_counts": dict(sorted(status_counts.items())),
        "service_statistics": service_statistics,
        "statistics_note": (
            "unmatched_reference_count 按 match_status='unmatched' 严格计数；"
            "service_statistics.unmatched_reference_count 是服务返回的残差桶，"
            "其中包含 self_reference。"
        ),
    }
    return {
        "statistics": statistics,
        "extraction_quality": _extraction_quality(db, references),
        "relations": _relation_evidence(db, references),
        "edges": [
            {
                "edge_id": edge.id,
                "citing_paper_id": edge.citing_paper_id,
                "cited_paper_id": edge.cited_paper_id,
                "citation_reference_id": edge.citation_reference_id,
                "source_element_id": edge.reference.source_element_id,
                "source_page": edge.reference.source_page_number,
                "source_entry_index": edge.reference.source_entry_index,
                "raw_text": edge.reference.raw_text,
                "match_method": edge.match_method,
                "match_confidence": edge.match_confidence,
            }
            for edge in edges
        ],
        "base_content_unchanged": before == after,
        "base_content_before": before,
        "base_content_after": after,
    }


def _verify_database_integrity(db: Session) -> dict[str, Any]:
    integrity = db.execute(text("PRAGMA integrity_check")).scalar()
    foreign_keys = [tuple(row) for row in db.execute(text("PRAGMA foreign_key_check"))]
    reference_count = db.query(CitationReference).count()
    edge_count = db.query(CitationEdge).count()
    duplicate_edge_pairs = [
        tuple(row)
        for row in db.execute(
            text(
                "SELECT citing_paper_id, cited_paper_id, COUNT(*) "
                "FROM citation_edges "
                "GROUP BY citing_paper_id, cited_paper_id "
                "HAVING COUNT(*) > 1"
            )
        )
    ]
    linked_targets = sum(
        reference.target_paper_id is not None
        for reference in db.query(CitationReference).all()
    )
    return {
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
        "reference_count": reference_count,
        "linked_target_count": linked_targets,
        "edge_count": edge_count,
        "duplicate_edge_pairs": duplicate_edge_pairs,
    }


def build_citation_relations(
    *,
    source_db: Path,
    target_db: Path,
    report_path: Path,
    reuse_target: bool = False,
) -> dict[str, Any]:
    source_db = _resolved(source_db)
    target_db = _resolved(target_db)
    report_path = _resolved(report_path)
    if not source_db.is_file():
        raise FileNotFoundError(f"Source database does not exist: {source_db}")
    if source_db == target_db:
        raise ValueError("Target database must differ from the source database")
    if target_db.exists() and not reuse_target:
        raise FileExistsError(
            f"Target database already exists: {target_db}; pass --reuse-target "
            "only when intentionally rerunning against this citation copy"
        )
    if report_path in {source_db, target_db}:
        raise ValueError("Report path must not overwrite a database")
    if not report_path.parent.is_dir():
        raise FileNotFoundError(
            f"Report directory does not exist: {report_path.parent}"
        )

    source_hash_before = _sha256(source_db)
    target_existed = target_db.exists()
    if not target_existed:
        target_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_db, target_db)
    target_hash_before = _sha256(target_db)

    source_tables = _table_names(source_db)
    target_tables_before = _table_names(target_db)
    engine = _build_engine(target_db)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    try:
        CitationReference.__table__.create(bind=engine, checkfirst=True)
        CitationEdge.__table__.create(bind=engine, checkfirst=True)
        ensure_additive_schema(engine)
        with session_factory() as db:
            initial_content = _base_content_snapshot(db)
            if initial_content["reference_element_count"] != 143:
                raise ValueError(
                    "Expected 143 PaperElement(reference) rows, got "
                    f"{initial_content['reference_element_count']}"
                )

            first = _run_build(db)
            first_snapshot = _logical_snapshot(db)
            second = _run_build(db)
            second_snapshot = _logical_snapshot(db)
            integrity = _verify_database_integrity(db)
            final_content = _base_content_snapshot(db)
            paper_metadata = _paper_metadata(db)

        source_hash_after = _sha256(source_db)
        target_hash_after = _sha256(target_db)
        source_unchanged = source_hash_before == source_hash_after
        logical_idempotent = (
            first_snapshot["references_sha256"]
            == second_snapshot["references_sha256"]
            and first_snapshot["edges_sha256"] == second_snapshot["edges_sha256"]
        )
        idempotency = {
            "passed": (
                logical_idempotent
                and first["statistics"] == second["statistics"]
                and integrity["integrity_check"] == "ok"
                and not integrity["foreign_key_violations"]
                and not integrity["duplicate_edge_pairs"]
            ),
            "first_statistics": first["statistics"],
            "second_statistics": second["statistics"],
            "first_references_sha256": first_snapshot["references_sha256"],
            "second_references_sha256": second_snapshot["references_sha256"],
            "first_edges_sha256": first_snapshot["edges_sha256"],
            "second_edges_sha256": second_snapshot["edges_sha256"],
            "logical_rows_identical_after_rebuild": logical_idempotent,
            "physical_ids_note": (
                "citation_service 会重建引用和边，因此自增主键可能变化；"
                "幂等性按业务内容、关联依据、统计和唯一性约束验证。"
            ),
        }
        report = {
            "report_version": REPORT_VERSION,
            "source_database": str(source_db),
            "target_database": str(target_db),
            "source_database_sha256_before": source_hash_before,
            "source_database_sha256_after": source_hash_after,
            "source_database_unchanged": source_unchanged,
            "target_database_reused": reuse_target and target_existed,
            "target_database_sha256_before_build": target_hash_before,
            "target_database_sha256_after_build": target_hash_after,
            "source_existing_tables": sorted(source_tables),
            "target_existing_tables_before_schema_update": sorted(
                target_tables_before
            ),
            "target_tables_after_schema_update": sorted(
                _table_names(target_db)
            ),
            "papers": paper_metadata,
            "initial_base_content": initial_content,
            "final_base_content": final_content,
            "base_content_unchanged": initial_content == final_content,
            "first_build": first,
            "second_build": {
                "statistics": second["statistics"],
                "base_content_unchanged": second["base_content_unchanged"],
            },
            "idempotency": idempotency,
            "integrity": integrity,
        }
    finally:
        engine.dispose()

    source_hash_after = _sha256(source_db)
    report["source_database_sha256_after"] = source_hash_after
    report["source_database_unchanged"] = (
        report["source_database_sha256_before"] == source_hash_after
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build and audit citation relations in an independent staging "
            "database using the existing citation_service."
        )
    )
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--target-db", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--reuse-target",
        action="store_true",
        help="Reuse an existing target copy for an intentional repeated run",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = build_citation_relations(
            source_db=args.source_db,
            target_db=args.target_db,
            report_path=args.report,
            reuse_target=args.reuse_target,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1

    statistics = report["first_build"]["statistics"]
    print(
        json.dumps(
            {
                "success": True,
                "source_database": report["source_database"],
                "target_database": report["target_database"],
                "report": str(_resolved(args.report)),
                "statistics": statistics,
                "idempotency_passed": report["idempotency"]["passed"],
                "source_database_unchanged": report[
                    "source_database_unchanged"
                ],
                "base_content_unchanged": report["base_content_unchanged"],
                "integrity": report["integrity"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
