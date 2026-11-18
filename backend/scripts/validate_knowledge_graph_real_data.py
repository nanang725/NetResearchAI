from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Base
from app.models import (  # noqa: F401
    CitationEdge,
    CitationReference,
    KnowledgeEdge,
    KnowledgeNode,
    Paper,
    PaperChunk,
    PaperElement,
    PaperPage,
)
from app.services.knowledge_extraction_service import rebuild_knowledge_graph
from app.services.knowledge_graph_service import (
    get_core_papers,
    get_knowledge_graph,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate v0.5 knowledge-graph extraction against a temporary copy "
            "of the real NetResearch SQLite database."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=BACKEND_ROOT / "data" / "netresearch.db",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        raise SystemExit(f"Source database not found: {source}")

    with tempfile.TemporaryDirectory(prefix="netresearch-kg-v05-") as workdir:
        target = Path(workdir) / "knowledge-graph-validation.db"
        shutil.copy2(source, target)
        engine = create_engine(
            f"sqlite:///{target.as_posix()}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        session_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            future=True,
        )
        Base.metadata.create_all(bind=engine)
        db = session_factory()
        try:
            paper_count = db.query(Paper).count()
            chunk_count = db.query(PaperChunk).count()
            citation_count = db.query(CitationEdge).count()
            rebuild = rebuild_knowledge_graph(db)
            graph = get_knowledge_graph(db, limit=1000)
            core = get_core_papers(db, limit=10)
            missing_provenance = (
                db.query(KnowledgeEdge)
                .filter(
                    (KnowledgeEdge.paper_id.is_(None))
                    | (KnowledgeEdge.page_number <= 0)
                    | (KnowledgeEdge.chunk_id.is_(None))
                    | (KnowledgeEdge.evidence_text == "")
                )
                .count()
            )
            payload = {
                "source_modified": False,
                "source_database": str(source),
                "source_paper_count": paper_count,
                "source_chunk_count": chunk_count,
                "source_citation_edge_count": citation_count,
                "rebuild": rebuild,
                "graph_stats": graph["stats"],
                "node_type_counts": graph["stats"]["node_type_counts"],
                "relation_type_counts": graph["stats"]["relation_type_counts"],
                "core_papers": [
                    {
                        "paper_id": paper["paper_id"],
                        "title": paper["label"],
                        "pagerank": paper["paper_pagerank"],
                    }
                    for paper in core["papers"]
                ],
                "missing_provenance_edge_count": missing_provenance,
                "valid": (
                    rebuild["status"] == "completed"
                    and missing_provenance == 0
                    and graph["stats"]["edge_count"] == rebuild["edge_count"]
                ),
            }
        finally:
            db.close()
            engine.dispose()

    output = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if payload["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
