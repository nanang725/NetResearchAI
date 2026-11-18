from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models.paper import Paper
from app.models.knowledge_graph import KnowledgeEdge
from app.services.knowledge_graph_service import find_relation_path
from app.services.research_agent.tools import TOOL_REGISTRY, ToolContext


def _source_is_grounded(source: dict) -> bool:
    return (
        source.get("source_type") == "local_full_text"
        and bool(source.get("paper_id"))
        and bool(source.get("page_number"))
        and bool(source.get("chunk_id"))
        and bool(source.get("excerpt"))
    )


def main() -> int:
    database = BACKEND_ROOT / "data" / "netresearch.db"
    engine = create_engine(
        f"sqlite:///{database.as_posix()}",
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
        paper_ids = [
            paper_id
            for (paper_id,) in db.query(Paper.id).order_by(Paper.id.asc()).all()
        ]
        if len(paper_ids) < 2:
            raise RuntimeError("At least two local papers are required.")
        method_paper_ids = [
            paper_id
            for (paper_id,) in (
                db.query(KnowledgeEdge.paper_id)
                .filter(KnowledgeEdge.relation_type == "USES_METHOD")
                .distinct()
                .order_by(KnowledgeEdge.paper_id.asc())
                .all()
            )
        ]
        if len(method_paper_ids) < 2:
            raise RuntimeError("At least two papers with method edges are required.")
        path_pair: tuple[int, int] | None = None
        for source_id in paper_ids:
            for target_id in paper_ids:
                if source_id == target_id:
                    continue
                if find_relation_path(
                    db,
                    source_paper_id=source_id,
                    target_paper_id=target_id,
                    max_depth=5,
                )["path_found"]:
                    path_pair = (source_id, target_id)
                    break
            if path_pair is not None:
                break
        if path_pair is None:
            raise RuntimeError("No grounded relation path was found.")
        context = ToolContext(
            db=db,
            run_id="v0.5-final-acceptance",
            node_id="knowledge-graph-tool-acceptance",
            cancel_check=lambda: False,
        )
        executions = {
            "explore_knowledge_graph": {
                "paper_id": method_paper_ids[0],
                "query": "",
                "limit": 100,
            },
            "compare_knowledge_methods": {
                "paper_ids": method_paper_ids[:2],
            },
            "find_knowledge_path": {
                "source_paper_id": path_pair[0],
                "target_paper_id": path_pair[1],
                "max_depth": 5,
            },
            "explore_research_gaps": {
                "topic": "",
                "limit": 20,
            },
        }
        results = []
        for tool_name, arguments in executions.items():
            result = TOOL_REGISTRY.execute(tool_name, arguments, context)
            sources = result.get("sources", [])
            results.append(
                {
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "success": result.get("success"),
                    "status": result.get("status"),
                    "result_count": result.get("result_count"),
                    "source_count": len(sources),
                    "all_sources_grounded": all(
                        _source_is_grounded(source)
                        for source in sources
                    ),
                    "summary": result.get("summary"),
                }
            )
    finally:
        db.close()
        engine.dispose()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database),
        "paper_ids_used": {
            "explore": method_paper_ids[0],
            "method_comparison": method_paper_ids[:2],
            "path": list(path_pair),
        },
        "results": results,
        "valid": all(
            item["success"] is True
            and item["status"] in {"success", "empty_result"}
            and item["all_sources_grounded"]
            for item in results
        ),
    }
    output_dir = (
        PROJECT_ROOT
        / "maintenance"
        / "backups"
        / "v0.5.0-final-acceptance"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "agent-knowledge-tools-acceptance.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
