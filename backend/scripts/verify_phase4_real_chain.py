from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
TERMINAL_STATUSES = {
    "completed",
    "completed_with_warnings",
    "partial",
    "failed",
    "cancelled",
}


def _copy_database(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as source_db:
        with sqlite3.connect(target) as target_db:
            source_db.backup(target_db)


def _write_snapshot(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-db",
        type=Path,
        default=BACKEND_ROOT / "data" / "netresearch.db",
    )
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    temp_dir = Path(tempfile.mkdtemp(prefix="netresearch-phase4-e2e-"))
    database_path = temp_dir / "agent-e2e.db"
    _copy_database(args.source_db, database_path)
    os.environ["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
    os.environ["APP_ENV"] = "test"

    from fastapi.testclient import TestClient

    from app.main import app

    goal = "比较本地论文中的网络方法、实验证据与局限"
    budget = {
        "max_nodes": 3,
        "max_tool_calls": 4,
        "max_llm_calls": 4,
        "max_arxiv_requests": 0,
        "max_replans": 0,
        "max_execution_seconds": 480,
    }
    snapshot: dict = {
        "temporary_database": str(database_path),
        "goal": goal,
        "stages": {},
    }

    try:
        with TestClient(app) as client:
            draft_response = client.post(
                "/api/research-agent/plans",
                json={"goal": goal, "budget": budget},
            )
            snapshot["stages"]["draft"] = {
                "status_code": draft_response.status_code,
                "payload": draft_response.json(),
            }
            if draft_response.status_code != 201:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
                return 1

            draft = draft_response.json()["data"]
            plan = draft["plan"]
            generated_by = plan.get("generated_by")
            nodes = [
                {
                    "id": "search_local",
                    "tool_name": "search_local_papers",
                    "arguments": {
                        "query": "网络 流量 方法 证据",
                        "limit": 6,
                    },
                    "depends_on": [],
                    "reason": "Retrieve real local full-text evidence.",
                    "completion_condition": "Return real local paper IDs.",
                    "retry_limit": 1,
                    "failure_strategy": "retry_then_fail",
                    "critical": True,
                    "fallback_tool": None,
                    "fallback_arguments": {},
                    "input_bindings": {},
                    "output_definition": {
                        "description": "Real local evidence and paper IDs.",
                        "fields": ["result_paper_ids", "sources"],
                    },
                },
                {
                    "id": "compare",
                    "tool_name": "compare_papers",
                    "arguments": {
                        "topic": "网络方法、实验证据与局限比较",
                        "include_future_work": True,
                    },
                    "depends_on": ["search_local"],
                    "reason": "Compare the first two real local paper IDs.",
                    "completion_condition": "Return grounded comparison.",
                    "retry_limit": 0,
                    "failure_strategy": "retry_then_fail",
                    "critical": True,
                    "fallback_tool": None,
                    "fallback_arguments": {},
                    "input_bindings": {
                        "paper_ids": {
                            "source_node_id": "search_local",
                            "output_key": "result_paper_ids",
                            "max_items": 2,
                        }
                    },
                    "output_definition": {
                        "description": "Grounded multi-paper comparison.",
                        "fields": ["selected_paper_ids", "comparison"],
                    },
                },
                {
                    "id": "outline",
                    "tool_name": "generate_research_outline",
                    "arguments": {"topic": "网络方法证据大纲"},
                    "depends_on": ["compare"],
                    "reason": "Generate an evidence-linked outline.",
                    "completion_condition": "Return evidence-backed outline.",
                    "retry_limit": 0,
                    "failure_strategy": "retry_then_fail",
                    "critical": True,
                    "fallback_tool": None,
                    "fallback_arguments": {},
                    "input_bindings": {
                        "paper_id": {
                            "source_node_id": "compare",
                            "output_key": "selected_paper_ids.0",
                            "max_items": None,
                        }
                    },
                    "output_definition": {
                        "description": "Evidence-linked outline.",
                        "fields": ["outline"],
                    },
                },
            ]
            update_response = client.put(
                f"/api/research-agent/plans/{draft['run_id']}",
                json={
                    "goal": goal,
                    "topic": "网络方法、证据与局限",
                    "research_question": (
                        "本地论文中的网络方法、证据与局限分别是什么？"
                    ),
                    "time_range": None,
                    "analysis_dimensions": ["方法", "证据", "局限"],
                    "nodes": nodes,
                    "budget": budget,
                    "change_reason": "Phase 4 real-chain verification plan",
                },
            )
            snapshot["stages"]["edited"] = {
                "status_code": update_response.status_code,
                "payload": update_response.json(),
                "initial_planner_source": generated_by,
            }
            if update_response.status_code != 200:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
                return 1

            confirm_response = client.post(
                f"/api/research-agent/plans/{draft['run_id']}/confirm"
            )
            snapshot["stages"]["confirmed"] = {
                "status_code": confirm_response.status_code,
                "payload": confirm_response.json(),
            }
            if confirm_response.status_code != 202:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
                return 1

            run_id = draft["run_id"]
            deadline = time.monotonic() + args.timeout
            current = None
            while time.monotonic() < deadline:
                response = client.get(f"/api/research-agent/runs/{run_id}")
                if response.status_code != 200:
                    snapshot["poll_error"] = {
                        "status_code": response.status_code,
                        "payload": response.json(),
                    }
                    break
                current = response.json()["data"]
                if current["status"] in TERMINAL_STATUSES:
                    break
                time.sleep(2)
            snapshot["stages"]["terminal"] = current
            if current is None or current["status"] not in TERMINAL_STATUSES:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
                return 1

            report_response = client.get(
                f"/api/research-agent/runs/{run_id}/report"
            )
            snapshot["stages"]["report"] = {
                "status_code": report_response.status_code,
                "payload": report_response.json(),
            }

            all_paper_ids = {
                item.get("paper_id")
                for item in current.get("evidence", [])
                if item.get("paper_id") is not None
            }
            node_statuses = {
                node["node_key"]: node["status"]
                for node in current.get("nodes", [])
            }
            complete = (
                current["status"] in {"completed", "completed_with_warnings"}
                and current.get("report_available") is True
                and node_statuses
                == {
                    "search_local": "completed",
                    "compare": "completed",
                    "outline": "completed",
                }
                and bool(all_paper_ids)
            )
            snapshot["verification"] = {
                "complete_chain": complete,
                "node_statuses": node_statuses,
                "evidence_paper_ids": sorted(all_paper_ids),
                "report_available": current.get("report_available"),
                "llm_diagnostics": current.get("llm_diagnostics", []),
                "usage": current.get("usage"),
            }
            output_path = temp_dir / "real-chain-result.json"
            _write_snapshot(output_path, snapshot)
            snapshot["snapshot_path"] = str(output_path)
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            return 0 if complete else 1
    finally:
        final_path = temp_dir / "real-chain-final.json"
        if not final_path.exists():
            _write_snapshot(final_path, snapshot)


if __name__ == "__main__":
    raise SystemExit(main())
