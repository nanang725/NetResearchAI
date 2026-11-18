from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Base, ensure_additive_schema
from app.models import (  # noqa: F401
    CitationEdge,
    CitationReference,
    KnowledgeEdge,
    KnowledgeExtractionRun,
    KnowledgeNode,
    Paper,
    PaperChunk,
    PaperElement,
    PaperImportItem,
    PaperImportJob,
    PaperPage,
)
from app.services.knowledge_extraction_service import rebuild_knowledge_graph
from app.services.knowledge_graph_service import get_knowledge_graph


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_inventory(root: Path, pattern: str) -> list[dict]:
    if not root.exists():
        return []
    return [
        {
            "path": path.relative_to(PROJECT_ROOT).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(root.rglob(pattern))
        if path.is_file()
    ]


def _database_snapshot(db) -> dict:
    papers = [
        {
            "id": paper.id,
            "file_hash": paper.file_hash,
            "stored_filename": paper.stored_filename,
            "file_path": paper.file_path,
            "page_count": paper.page_count,
            "parse_status": paper.parse_status,
            "index_status": paper.index_status,
        }
        for paper in db.query(Paper).order_by(Paper.id.asc()).all()
    ]
    return {
        "paper_count": len(papers),
        "page_count": db.query(PaperPage).count(),
        "element_count": db.query(PaperElement).count(),
        "chunk_count": db.query(PaperChunk).count(),
        "import_job_count": db.query(PaperImportJob).count(),
        "import_item_count": db.query(PaperImportItem).count(),
        "citation_reference_count": db.query(CitationReference).count(),
        "citation_edge_count": db.query(CitationEdge).count(),
        "papers": papers,
    }


def _sqlite_backup_snapshot(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "integrity_check": integrity,
            "paper_count": connection.execute(
                "SELECT COUNT(*) FROM papers"
            ).fetchone()[0],
            "page_count": connection.execute(
                "SELECT COUNT(*) FROM paper_pages"
            ).fetchone()[0],
            "chunk_count": connection.execute(
                "SELECT COUNT(*) FROM paper_chunks"
            ).fetchone()[0],
        }


def _missing_provenance_count(db) -> int:
    chunk_ids = {chunk_id for (chunk_id,) in db.query(PaperChunk.id).all()}
    return sum(
        1
        for edge in db.query(KnowledgeEdge).all()
        if (
            edge.paper_id is None
            or edge.page_number <= 0
            or edge.chunk_id not in chunk_ids
            or not (edge.evidence_text or "").strip()
        )
    )


def main() -> int:
    source = (BACKEND_ROOT / "data" / "netresearch.db").resolve()
    if not source.is_file():
        raise SystemExit(f"Main database not found: {source}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        PROJECT_ROOT
        / "maintenance"
        / "backups"
        / "v0.5.0-final-acceptance"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    backup = output_dir / f"netresearch-pre-v0.5-rebuild-{timestamp}.db"
    report_path = output_dir / f"main-database-acceptance-{timestamp}.json"

    pdf_inventory_before = _file_inventory(
        BACKEND_ROOT / "data",
        "*.pdf",
    )
    chroma_inventory_before = _file_inventory(
        BACKEND_ROOT / "data" / "chroma_db",
        "*",
    )

    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(backup) as backup_connection:
            source_connection.backup(backup_connection)
    source_before_hash = _sha256(source)
    backup_hash = _sha256(backup)
    source_backup_snapshot = _sqlite_backup_snapshot(source)
    backup_snapshot = _sqlite_backup_snapshot(backup)
    backup_verified = (
        source_backup_snapshot == backup_snapshot
        and backup_snapshot["integrity_check"] == "ok"
    )
    if not backup_verified:
        raise SystemExit("SQLite backup integrity verification failed.")

    engine = create_engine(
        f"sqlite:///{source.as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    db = session_factory()
    try:
        snapshot_before = _database_snapshot(db)
        Base.metadata.create_all(bind=engine)
        ensure_additive_schema(engine)
        table_names = set(inspect(engine).get_table_names())
        required_tables = {
            "knowledge_nodes",
            "knowledge_edges",
            "knowledge_extraction_runs",
        }
        missing_tables = sorted(required_tables - table_names)
        if missing_tables:
            raise RuntimeError(
                f"Knowledge graph tables were not created: {missing_tables}"
            )
        rebuild = rebuild_knowledge_graph(db)
        db.expire_all()
        snapshot_after = _database_snapshot(db)
        graph = get_knowledge_graph(db, limit=1000)
        latest_run = (
            db.query(KnowledgeExtractionRun)
            .order_by(KnowledgeExtractionRun.started_at.desc())
            .first()
        )
        missing_provenance = _missing_provenance_count(db)
        node_types = Counter(
            node_type
            for (node_type,) in db.query(KnowledgeNode.node_type).all()
        )
        relation_types = Counter(
            relation_type
            for (relation_type,) in db.query(KnowledgeEdge.relation_type).all()
        )
    finally:
        db.close()
        engine.dispose()

    pdf_inventory_after = _file_inventory(BACKEND_ROOT / "data", "*.pdf")
    chroma_inventory_after = _file_inventory(
        BACKEND_ROOT / "data" / "chroma_db",
        "*",
    )
    source_after_hash = _sha256(source)
    paper_data_unchanged = (
        snapshot_before["papers"] == snapshot_after["papers"]
    )
    table_counts_unchanged = all(
        snapshot_before[key] == snapshot_after[key]
        for key in (
            "paper_count",
            "page_count",
            "element_count",
            "chunk_count",
            "import_job_count",
            "import_item_count",
            "citation_reference_count",
            "citation_edge_count",
        )
    )
    pdf_unchanged = pdf_inventory_before == pdf_inventory_after
    chroma_unchanged = chroma_inventory_before == chroma_inventory_after
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_database": str(source),
        "backup_database": str(backup),
        "source_hash_before": source_before_hash,
        "backup_hash": backup_hash,
        "source_hash_after": source_after_hash,
        "backup_verified": backup_verified,
        "backup_snapshot": backup_snapshot,
        "schema_migration": {
            "required_tables": sorted(required_tables),
            "missing_tables": missing_tables,
        },
        "before": snapshot_before,
        "after": snapshot_after,
        "rebuild": rebuild,
        "latest_rebuild": {
            "run_id": latest_run.id if latest_run else None,
            "status": latest_run.status if latest_run else None,
            "completed_at": (
                latest_run.completed_at.isoformat()
                if latest_run and latest_run.completed_at
                else None
            ),
        },
        "graph_stats": graph["stats"],
        "node_type_counts": dict(node_types),
        "relation_type_counts": dict(relation_types),
        "missing_provenance_edge_count": missing_provenance,
        "data_safety": {
            "paper_data_unchanged": paper_data_unchanged,
            "table_counts_unchanged": table_counts_unchanged,
            "pdf_inventory_unchanged": pdf_unchanged,
            "pdf_file_count": len(pdf_inventory_before),
            "chroma_inventory_unchanged": chroma_unchanged,
            "chroma_file_count": len(chroma_inventory_before),
        },
        "valid": (
            not missing_tables
            and rebuild["status"] == "completed"
            and missing_provenance == 0
            and paper_data_unchanged
            and table_counts_unchanged
            and pdf_unchanged
            and chroma_unchanged
        ),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
