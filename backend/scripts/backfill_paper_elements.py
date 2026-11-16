from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import quote


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.pdf_service import extract_pdf  # noqa: E402


ELEMENT_BACKFILL_VERSION = "paper-elements-backfill-v1"
BACKFILL_STATE_TABLE = "paper_element_backfill_state"

_PAPER_COLUMNS = (
    "id",
    "title",
    "file_hash",
    "file_path",
    "original_filename",
)
_PAGE_COLUMNS = (
    "id",
    "paper_id",
    "page_number",
    "text",
)
_ELEMENT_COLUMNS = (
    "id",
    "paper_id",
    "page_id",
    "page_number",
    "element_index",
    "element_type",
    "text",
    "data",
    "bbox",
    "section_path",
    "extraction_status",
    "confidence",
)


class BackfillError(RuntimeError):
    """Raised when a safe PaperElement-only backfill cannot continue."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(payload)


def _normalize_chunk_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return normalized.replace("\r\n", "\n").replace("\r", "\n").strip()


def _sqlite_uri(path: Path, mode: str) -> str:
    return f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode={mode}"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_sqlite_uri(path, "ro"), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _connect_read_write(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_sqlite_uri(path, "rw"), uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> list[str]:
    return [
        str(row["name"])
        for row in connection.execute(
            f'PRAGMA table_info("{table_name}")'
        )
    ]


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "papers": _PAPER_COLUMNS,
        "paper_pages": _PAGE_COLUMNS,
        "paper_elements": _ELEMENT_COLUMNS,
        "paper_chunks": (
            "id",
            "paper_id",
            "page_number",
            "chunk_index",
            "text",
            "content_type",
            "element_id",
        ),
    }
    tables = _table_names(connection)
    missing_tables = sorted(set(required) - tables)
    if missing_tables:
        raise BackfillError(
            f"Database is missing required tables: {missing_tables}"
        )

    missing_columns: dict[str, list[str]] = {}
    for table_name, expected_columns in required.items():
        actual_columns = set(_table_columns(connection, table_name))
        missing = sorted(set(expected_columns) - actual_columns)
        if missing:
            missing_columns[table_name] = missing
    if missing_columns:
        raise BackfillError(
            "Database is missing required columns: "
            f"{json.dumps(missing_columns, sort_keys=True)}"
        )


def _parse_paper_ids(value: str | None) -> list[int] | None:
    if value is None:
        return None
    paper_ids: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            paper_id = int(token)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"Invalid paper id: {token!r}"
            ) from exc
        if paper_id <= 0:
            raise argparse.ArgumentTypeError(
                f"Paper id must be positive: {paper_id}"
            )
        paper_ids.append(paper_id)
    if not paper_ids:
        raise argparse.ArgumentTypeError("--paper-ids cannot be empty")
    return sorted(set(paper_ids))


def _load_papers(
    connection: sqlite3.Connection,
    paper_ids: list[int] | None,
) -> list[dict[str, Any]]:
    query = (
        "SELECT id, title, file_hash, file_path, original_filename "
        "FROM papers"
    )
    parameters: list[Any] = []
    if paper_ids is not None:
        placeholders = ",".join("?" for _ in paper_ids)
        query += f" WHERE id IN ({placeholders})"
        parameters.extend(paper_ids)
    query += " ORDER BY id"
    papers = [dict(row) for row in connection.execute(query, parameters)]
    if paper_ids is not None:
        found_ids = {int(paper["id"]) for paper in papers}
        missing_ids = sorted(set(paper_ids) - found_ids)
        if missing_ids:
            raise BackfillError(
                f"Requested papers do not exist: {missing_ids}"
            )
    return papers


def _resolve_pdf_path(
    stored_path: str,
    pdf_root: Path,
    expected_hash: str,
) -> Path:
    root = pdf_root.resolve()
    stored = Path(stored_path)
    candidates: list[Path] = []
    if stored.is_absolute():
        resolved_stored = stored.resolve()
        if _path_is_within(resolved_stored, root):
            candidates.append(resolved_stored)
        else:
            candidates.append(root / stored.name)
    else:
        candidates.extend(
            (
                root / stored,
                root / stored.name,
            )
        )

    seen_candidates: set[Path] = set()
    hash_mismatch_paths: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved in seen_candidates:
            continue
        seen_candidates.add(resolved)
        if not resolved.is_file():
            continue
        actual_hash = _sha256_bytes(resolved.read_bytes())
        if actual_hash == expected_hash:
            return resolved
        hash_mismatch_paths.append(resolved)

    if hash_mismatch_paths:
        raise BackfillError(
            "PDF SHA-256 mismatch inside --pdf-root for "
            f"{stored_path}: expected={expected_hash} "
            f"candidates={[str(path) for path in hash_mismatch_paths]}"
        )
    raise BackfillError(
        f"PDF does not exist inside --pdf-root: {stored_path}"
    )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_report_output_path(
    report_path: Path | None,
    db_path: Path,
    pdf_root: Path,
    chroma_dir: Path,
) -> None:
    if report_path is None:
        return
    resolved_report = report_path.resolve()
    resolved_db = db_path.resolve()
    resolved_pdf_root = pdf_root.resolve()
    resolved_chroma_dir = chroma_dir.resolve()

    if resolved_report == resolved_db:
        raise BackfillError(
            "--json must not overwrite the --db SQLite database"
        )
    if _path_is_within(resolved_report, resolved_pdf_root):
        raise BackfillError(
            "--json must not be located inside --pdf-root"
        )
    if _path_is_within(resolved_report, resolved_chroma_dir):
        raise BackfillError(
            "--json must not be located inside --chroma-dir"
        )


def _load_pdf_inputs(
    connection: sqlite3.Connection,
    papers: list[dict[str, Any]],
    pdf_root: Path,
) -> dict[int, dict[str, Any]]:
    inputs: dict[int, dict[str, Any]] = {}
    for paper in papers:
        paper_id = int(paper["id"])
        expected_hash = str(paper["file_hash"])
        pdf_path = _resolve_pdf_path(
            str(paper["file_path"]),
            pdf_root,
            expected_hash,
        )
        content = pdf_path.read_bytes()
        actual_hash = _sha256_bytes(content)
        if actual_hash != expected_hash:
            raise BackfillError(
                f"PDF SHA-256 mismatch for paper_id={paper_id}: "
                f"expected={expected_hash} actual={actual_hash}"
            )
        page_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id, page_number, text FROM paper_pages "
                "WHERE paper_id=? ORDER BY page_number",
                (paper_id,),
            )
        ]
        if not page_rows:
            raise BackfillError(
                f"paper_id={paper_id} has no PaperPage rows"
            )
        inputs[paper_id] = {
            "pdf_path": pdf_path,
            "content": content,
            "actual_hash": actual_hash,
            "page_rows": page_rows,
        }
    return inputs


def _serialize_element(
    paper_id: int,
    page_id: int,
    page_number: int,
    element_index: int,
    element: dict[str, Any],
) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "page_id": page_id,
        "page_number": page_number,
        "element_index": element_index,
        "element_type": str(element["element_type"]),
        "text": str(element.get("text", "")),
        "data": (
            json.dumps(element["data"], ensure_ascii=False, sort_keys=True)
            if element.get("data") is not None
            else None
        ),
        "bbox": (
            json.dumps(element["bbox"], sort_keys=True)
            if element.get("bbox") is not None
            else None
        ),
        "section_path": element.get("section_path"),
        "extraction_status": str(
            element.get("extraction_status", "extracted")
        ),
        "confidence": element.get("confidence"),
    }


def _element_identity(element: dict[str, Any]) -> tuple[int, int]:
    return (int(element["page_id"]), int(element["element_index"]))


def _element_content(element: dict[str, Any]) -> dict[str, Any]:
    return {
        key: element[key]
        for key in _ELEMENT_COLUMNS
        if key not in {"id", "paper_id"}
    }


def _build_plan(
    paper: dict[str, Any],
    pdf_input: dict[str, Any],
) -> dict[str, Any]:
    paper_id = int(paper["id"])
    content = pdf_input["content"]
    pdf_path = Path(pdf_input["pdf_path"])
    extraction = extract_pdf(
        content,
        pdf_path.name,
        collect_pages=True,
    )
    extracted_pages = extraction.get("pages", [])
    database_pages = pdf_input["page_rows"]
    page_id_by_number = {
        int(page["page_number"]): int(page["id"])
        for page in database_pages
    }
    extracted_numbers = [
        int(page["page_number"])
        for page in extracted_pages
    ]
    if extracted_numbers != sorted(page_id_by_number):
        raise BackfillError(
            f"PaperPage coverage mismatch for paper_id={paper_id}: "
            f"database={sorted(page_id_by_number)} "
            f"pdf={extracted_numbers}"
        )

    elements: list[dict[str, Any]] = []
    reference_count = 0
    for page in extracted_pages:
        page_number = int(page["page_number"])
        page_id = page_id_by_number[page_number]
        for element_index, element in enumerate(page.get("elements", [])):
            serialized = _serialize_element(
                paper_id,
                page_id,
                page_number,
                element_index,
                element,
            )
            elements.append(serialized)
            if serialized["element_type"] == "reference":
                reference_count += 1

    return {
        "paper_id": paper_id,
        "title": paper["title"],
        "original_filename": paper["original_filename"],
        "pdf_path": str(pdf_path),
        "pdf_sha256": pdf_input["actual_hash"],
        "page_count": len(database_pages),
        "expected_elements": len(elements),
        "expected_references": reference_count,
        "element_types": dict(
            sorted(Counter(
                element["element_type"] for element in elements
            ).items())
        ),
        "element_set_hash": _sha256_json(elements),
        "elements": elements,
    }


def _load_existing_elements(
    connection: sqlite3.Connection,
    paper_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT id, paper_id, page_id, page_number, element_index, "
            "element_type, text, data, bbox, section_path, "
            "extraction_status, confidence "
            "FROM paper_elements WHERE paper_id=? "
            "ORDER BY page_number, element_index",
            (paper_id,),
        )
    ]


def _validate_existing_elements(
    existing: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_by_identity = {
        _element_identity(element): element
        for element in expected
    }
    existing_identities: set[tuple[int, int]] = set()
    for row in existing:
        identity = _element_identity(row)
        if identity in existing_identities:
            raise BackfillError(
                f"Duplicate existing element identity: {identity}"
            )
        existing_identities.add(identity)
        expected_row = expected_by_identity.get(identity)
        if expected_row is None:
            raise BackfillError(
                "Existing PaperElement is not present in the current "
                f"parser output: {identity}"
            )
        if _element_content(row) != _element_content(expected_row):
            raise BackfillError(
                "Existing PaperElement content differs from the current "
                f"parser output at identity {identity}"
            )

    return [
        element
        for element in expected
        if _element_identity(element) not in existing_identities
    ]


def _insert_new_elements(
    connection: sqlite3.Connection,
    elements: list[dict[str, Any]],
) -> int:
    if not elements:
        return 0
    connection.executemany(
        "INSERT INTO paper_elements ("
        "paper_id, page_id, page_number, element_index, element_type, "
        "text, data, bbox, section_path, extraction_status, confidence"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                element["paper_id"],
                element["page_id"],
                element["page_number"],
                element["element_index"],
                element["element_type"],
                element["text"],
                element["data"],
                element["bbox"],
                element["section_path"],
                element["extraction_status"],
                element["confidence"],
            )
            for element in elements
        ],
    )
    return len(elements)


def _ensure_backfill_state_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        f'CREATE TABLE IF NOT EXISTS "{BACKFILL_STATE_TABLE}" ('
        "paper_id INTEGER PRIMARY KEY, "
        "parser_version TEXT NOT NULL, "
        "source_hash TEXT NOT NULL, "
        "element_count INTEGER NOT NULL, "
        "reference_count INTEGER NOT NULL, "
        "element_set_hash TEXT NOT NULL, "
        "completed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )


def _read_backfill_state(
    connection: sqlite3.Connection,
    paper_id: int,
) -> dict[str, Any] | None:
    if BACKFILL_STATE_TABLE not in _table_names(connection):
        return None
    row = connection.execute(
        f'SELECT * FROM "{BACKFILL_STATE_TABLE}" WHERE paper_id=?',
        (paper_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _validate_backfill_state(
    state: dict[str, Any] | None,
    plan: dict[str, Any],
) -> None:
    if state is None:
        return
    if state["parser_version"] != ELEMENT_BACKFILL_VERSION:
        raise BackfillError(
            f"paper_id={plan['paper_id']} has backfill parser version "
            f"{state['parser_version']!r}, expected "
            f"{ELEMENT_BACKFILL_VERSION!r}"
        )
    if state["source_hash"] != plan["pdf_sha256"]:
        raise BackfillError(
            f"paper_id={plan['paper_id']} backfill source hash differs "
            "from the current PDF"
        )
    if state["element_set_hash"] != plan["element_set_hash"]:
        raise BackfillError(
            f"paper_id={plan['paper_id']} backfill element set changed "
            "without a parser version update"
        )


def _write_backfill_state(
    connection: sqlite3.Connection,
    plan: dict[str, Any],
) -> None:
    connection.execute(
        f'INSERT INTO "{BACKFILL_STATE_TABLE}" ('
        "paper_id, parser_version, source_hash, element_count, "
        "reference_count, element_set_hash, completed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(paper_id) DO UPDATE SET "
        "parser_version=excluded.parser_version, "
        "source_hash=excluded.source_hash, "
        "element_count=excluded.element_count, "
        "reference_count=excluded.reference_count, "
        "element_set_hash=excluded.element_set_hash, "
        "completed_at=excluded.completed_at",
        (
            plan["paper_id"],
            ELEMENT_BACKFILL_VERSION,
            plan["pdf_sha256"],
            plan["expected_elements"],
            plan["expected_references"],
            plan["element_set_hash"],
        ),
    )


def _paper_page_chunk_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    page_rows = [
        {
            "id": int(row["id"]),
            "paper_id": int(row["paper_id"]),
            "page_number": int(row["page_number"]),
            "text_sha256": _sha256_text(str(row["text"])),
        }
        for row in connection.execute(
            "SELECT id, paper_id, page_number, text "
            "FROM paper_pages ORDER BY paper_id, page_number, id"
        )
    ]
    chunk_columns = set(_table_columns(connection, "paper_chunks"))
    page_chunk_expression = (
        "page_chunk_index"
        if "page_chunk_index" in chunk_columns
        else "NULL AS page_chunk_index"
    )
    chunk_rows = []
    for row in connection.execute(
        "SELECT id, paper_id, page_number, chunk_index, "
        f"{page_chunk_expression}, text, content_type, element_id "
        "FROM paper_chunks "
        "ORDER BY paper_id, page_number, chunk_index, id"
    ):
        text = str(row["text"])
        chunk_rows.append(
            {
                "id": int(row["id"]),
                "paper_id": int(row["paper_id"]),
                "page_number": int(row["page_number"]),
                "chunk_index": int(row["chunk_index"]),
                "page_chunk_index": (
                    int(row["page_chunk_index"])
                    if row["page_chunk_index"] is not None
                    else None
                ),
                "content_hash": _sha256_text(
                    _normalize_chunk_text(text)
                ),
                "text_sha256": _sha256_text(text),
                "content_type": row["content_type"],
                "element_id": (
                    int(row["element_id"])
                    if row["element_id"] is not None
                    else None
                ),
            }
        )
    return {
        "papers": [
            dict(row)
            for row in connection.execute(
                "SELECT id, title, file_hash, file_path "
                "FROM papers ORDER BY id"
            )
        ],
        "pages": page_rows,
        "chunks": chunk_rows,
        "page_snapshot_hash": _sha256_json(page_rows),
        "chunk_snapshot_hash": _sha256_json(chunk_rows),
    }


def _element_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    rows = [
        {
            "id": int(row["id"]),
            "paper_id": int(row["paper_id"]),
            "page_id": int(row["page_id"]),
            "page_number": int(row["page_number"]),
            "element_index": int(row["element_index"]),
            "element_type": row["element_type"],
        }
        for row in connection.execute(
            "SELECT id, paper_id, page_id, page_number, element_index, "
            "element_type FROM paper_elements "
            "ORDER BY paper_id, page_number, element_index, id"
        )
    ]
    return {
        "count": len(rows),
        "reference_count": sum(
            row["element_type"] == "reference" for row in rows
        ),
        "element_types": dict(
            sorted(Counter(
                row["element_type"] for row in rows
            ).items())
        ),
        "snapshot_hash": _sha256_json(rows),
    }


def _database_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    paper_page_chunk = _paper_page_chunk_snapshot(connection)
    elements = _element_snapshot(connection)
    return {
        **paper_page_chunk,
        "paper_count": len(paper_page_chunk["papers"]),
        "page_count": len(paper_page_chunk["pages"]),
        "chunk_count": len(paper_page_chunk["chunks"]),
        "element_count": elements["count"],
        "reference_count": elements["reference_count"],
        "element_types": elements["element_types"],
        "element_snapshot_hash": elements["snapshot_hash"],
    }


def _read_chroma_snapshot(chroma_dir: Path) -> dict[str, Any]:
    resolved_chroma_dir = chroma_dir.resolve()
    database_path = resolved_chroma_dir / "chroma.sqlite3"
    if not database_path.is_file():
        raise BackfillError(
            f"Chroma SQLite database does not exist: {database_path}"
        )
    files = [
        {
            "path": str(path.relative_to(resolved_chroma_dir)),
            "size": path.stat().st_size,
            "sha256": _sha256_bytes(path.read_bytes()),
        }
        for path in sorted(resolved_chroma_dir.rglob("*"))
        if path.is_file()
    ]
    connection = _connect_read_only(database_path)
    try:
        tables = _table_names(connection)
        if "embeddings" not in tables:
            raise BackfillError(
                "Chroma database is missing the embeddings table"
            )
        embeddings = [
            {
                "id": int(row["id"]),
                "embedding_id": str(row["embedding_id"]),
            }
            for row in connection.execute(
                "SELECT id, embedding_id FROM embeddings "
                "ORDER BY embedding_id, id"
            )
        ]
        metadata: dict[str, dict[str, Any]] = {
            embedding["embedding_id"]: {}
            for embedding in embeddings
        }
        if "embedding_metadata" in tables:
            for row in connection.execute(
                "SELECT e.embedding_id, m.key, m.string_value, "
                "m.int_value, m.float_value, m.bool_value "
                "FROM embeddings e "
                "JOIN embedding_metadata m ON m.id=e.id "
                "ORDER BY e.embedding_id, m.key"
            ):
                embedding_id = str(row["embedding_id"])
                metadata.setdefault(embedding_id, {})
                value = next(
                    (
                        row[key]
                        for key in (
                            "string_value",
                            "int_value",
                            "float_value",
                            "bool_value",
                        )
                        if row[key] is not None
                    ),
                    None,
                )
                metadata[embedding_id][str(row["key"])] = value

        vector_fingerprints = {
            embedding_id: metadata.get(embedding_id, {})
            for embedding_id in sorted(metadata)
        }
        return {
            "path": str(database_path),
            "vector_count": len(embeddings),
            "stored_index_fingerprints": {
                embedding_id: values.get("index_fingerprint")
                for embedding_id, values in vector_fingerprints.items()
                if values.get("index_fingerprint") is not None
            },
            "metadata_fingerprint": _sha256_json(vector_fingerprints),
            "directory_fingerprint": _sha256_json(files),
            "vector_ids": [
                embedding["embedding_id"] for embedding in embeddings
            ],
        }
    finally:
        connection.close()


def _pdf_hash_snapshot(
    inputs: dict[int, dict[str, Any]],
) -> dict[str, str]:
    return {
        str(paper_id): _sha256_bytes(
            Path(pdf_input["pdf_path"]).read_bytes()
        )
        for paper_id, pdf_input in sorted(inputs.items())
    }


def _expected_counts_match(
    plans: list[dict[str, Any]],
    expected_elements: int | None,
    expected_references: int | None,
) -> dict[str, Any]:
    actual_elements = sum(plan["expected_elements"] for plan in plans)
    actual_references = sum(plan["expected_references"] for plan in plans)
    checks = {
        "elements": {
            "expected": expected_elements,
            "actual": actual_elements,
            "passed": (
                expected_elements is None
                or expected_elements == actual_elements
            ),
        },
        "references": {
            "expected": expected_references,
            "actual": actual_references,
            "passed": (
                expected_references is None
                or expected_references == actual_references
            ),
        },
    }
    return {
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
    }


def _summarize_paper_outcomes(report: dict[str, Any]) -> None:
    committed_ids = [
        int(paper["paper_id"])
        for paper in report.get("papers", [])
        if paper.get("status") == "completed"
    ]
    failed_ids = [
        int(paper["paper_id"])
        for paper in report.get("papers", [])
        if paper.get("status") == "failed"
    ]
    unprocessed_ids = [
        int(paper["paper_id"])
        for paper in report.get("papers", [])
        if paper.get("status") not in {"completed", "failed", "planned"}
    ]
    report["committed_paper_ids"] = committed_ids
    report["failed_paper_ids"] = failed_ids
    report["unprocessed_paper_ids"] = unprocessed_ids
    report["partial_commit_possible"] = bool(
        committed_ids and (failed_ids or unprocessed_ids)
    )
    report["overall_transaction_rolled_back"] = False


def _validate_element_links(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    invalid = connection.execute(
        "SELECT COUNT(*) FROM paper_elements pe "
        "LEFT JOIN paper_pages pp ON pp.id=pe.page_id "
        "LEFT JOIN papers p ON p.id=pe.paper_id "
        "WHERE pp.id IS NULL OR p.id IS NULL "
        "OR pe.paper_id != pp.paper_id "
        "OR pe.page_number != pp.page_number"
    ).fetchone()[0]
    return {
        "invalid_count": int(invalid),
        "passed": int(invalid) == 0,
    }


def _invariant_report(
    before: dict[str, Any],
    after: dict[str, Any],
    chroma_before: dict[str, Any],
    chroma_after: dict[str, Any],
    pdf_before: dict[str, str],
    pdf_after: dict[str, str],
    element_links: dict[str, Any],
    expected_counts: dict[str, Any],
    mode: str,
) -> tuple[dict[str, Any], bool]:
    checks = {
        "paper_count_unchanged": {
            "before": before["paper_count"],
            "after": after["paper_count"],
            "passed": before["paper_count"] == after["paper_count"],
        },
        "page_count_unchanged": {
            "before": before["page_count"],
            "after": after["page_count"],
            "passed": before["page_count"] == after["page_count"],
        },
        "page_ids_numbers_text_unchanged": {
            "before": before["page_snapshot_hash"],
            "after": after["page_snapshot_hash"],
            "passed": (
                before["page_snapshot_hash"]
                == after["page_snapshot_hash"]
            ),
        },
        "chunk_count_unchanged": {
            "before": before["chunk_count"],
            "after": after["chunk_count"],
            "passed": before["chunk_count"] == after["chunk_count"],
        },
        "chunk_ids_content_order_element_id_unchanged": {
            "before": before["chunk_snapshot_hash"],
            "after": after["chunk_snapshot_hash"],
            "passed": (
                before["chunk_snapshot_hash"]
                == after["chunk_snapshot_hash"]
            ),
        },
        "pdf_sha256_unchanged": {
            "before": pdf_before,
            "after": pdf_after,
            "passed": pdf_before == pdf_after,
        },
        "chroma_vector_count_unchanged": {
            "before": chroma_before["vector_count"],
            "after": chroma_after["vector_count"],
            "passed": (
                chroma_before["vector_count"]
                == chroma_after["vector_count"]
            ),
        },
        "chroma_fingerprint_unchanged": {
            "before": chroma_before["metadata_fingerprint"],
            "after": chroma_after["metadata_fingerprint"],
            "passed": (
                chroma_before["metadata_fingerprint"]
                == chroma_after["metadata_fingerprint"]
            ),
        },
        "chroma_directory_unchanged": {
            "before": chroma_before["directory_fingerprint"],
            "after": chroma_after["directory_fingerprint"],
            "passed": (
                chroma_before["directory_fingerprint"]
                == chroma_after["directory_fingerprint"]
            ),
        },
        "paper_element_links_valid": element_links,
        "expected_counts_match": expected_counts,
    }
    if mode == "dry-run":
        checks["dry_run_database_unchanged"] = {
            "page_unchanged": (
                before["page_snapshot_hash"]
                == after["page_snapshot_hash"]
            ),
            "chunk_unchanged": (
                before["chunk_snapshot_hash"]
                == after["chunk_snapshot_hash"]
            ),
            "element_unchanged": (
                before["element_snapshot_hash"]
                == after["element_snapshot_hash"]
            ),
            "passed": (
                before["page_snapshot_hash"]
                == after["page_snapshot_hash"]
                and before["chunk_snapshot_hash"]
                == after["chunk_snapshot_hash"]
                and before["element_snapshot_hash"]
                == after["element_snapshot_hash"]
            ),
        }
    return checks, all(
        bool(check["passed"])
        for check in checks.values()
    )


def _per_paper_snapshot(
    snapshot: dict[str, Any],
    paper_id: int,
) -> dict[str, Any]:
    return {
        "page_count": sum(
            page["paper_id"] == paper_id
            for page in snapshot["pages"]
        ),
        "page_snapshot_hash": _sha256_json(
            [
                page
                for page in snapshot["pages"]
                if page["paper_id"] == paper_id
            ]
        ),
        "chunk_count": sum(
            chunk["paper_id"] == paper_id
            for chunk in snapshot["chunks"]
        ),
        "chunk_snapshot_hash": _sha256_json(
            [
                chunk
                for chunk in snapshot["chunks"]
                if chunk["paper_id"] == paper_id
            ]
        ),
    }


@contextmanager
def _paper_write_transaction(
    connection: sqlite3.Connection,
) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def backfill_paper_elements(
    *,
    db_path: Path,
    pdf_root: Path,
    chroma_dir: Path,
    paper_ids: list[int] | None,
    dry_run: bool,
    require_unchanged_chunks: bool,
    expected_elements: int | None = None,
    expected_references: int | None = None,
    insert_elements: Callable[
        [sqlite3.Connection, list[dict[str, Any]]],
        int,
    ] = _insert_new_elements,
) -> dict[str, Any]:
    started_at = _utc_now()
    report: dict[str, Any] = {
        "success": False,
        "mode": "dry-run" if dry_run else "apply",
        "started_at": started_at,
        "element_backfill_version": ELEMENT_BACKFILL_VERSION,
        "database": str(db_path.resolve()),
        "pdf_root": str(pdf_root.resolve()),
        "chroma_dir": str(chroma_dir.resolve()),
        "requested_paper_ids": paper_ids,
        "require_unchanged_chunks": require_unchanged_chunks,
        "papers": [],
        "errors": [],
    }
    if not db_path.is_file():
        raise BackfillError(f"Database does not exist: {db_path}")
    if not pdf_root.is_dir():
        raise BackfillError(f"PDF root does not exist: {pdf_root}")

    read_connection = _connect_read_only(db_path)
    try:
        _require_schema(read_connection)
        papers = _load_papers(read_connection, paper_ids)
        if not papers:
            raise BackfillError("No papers were selected")
        report["papers"] = [
            {
                "paper_id": int(paper["id"]),
                "title": paper["title"],
                "original_filename": paper["original_filename"],
                "status": "unprocessed",
            }
            for paper in papers
        ]
        pdf_inputs = _load_pdf_inputs(
            read_connection,
            papers,
            pdf_root,
        )
        before = _database_snapshot(read_connection)
        existing_by_paper = {
            int(paper["id"]): _load_existing_elements(
                read_connection,
                int(paper["id"]),
            )
            for paper in papers
        }
        states_by_paper = {
            int(paper["id"]): _read_backfill_state(
                read_connection,
                int(paper["id"]),
            )
            for paper in papers
        }
    finally:
        read_connection.close()

    chroma_before = _read_chroma_snapshot(chroma_dir)
    pdf_before = _pdf_hash_snapshot(pdf_inputs)
    report["before"] = before
    report["chroma_before"] = chroma_before

    plans: list[dict[str, Any]] = []
    try:
        for paper in papers:
            paper_id = int(paper["id"])
            plan = _build_plan(paper, pdf_inputs[paper_id])
            pending = _validate_existing_elements(
                existing_by_paper[paper_id],
                plan["elements"],
            )
            _validate_backfill_state(states_by_paper[paper_id], plan)
            plan["existing_elements"] = len(existing_by_paper[paper_id])
            plan["pending_insert_count"] = len(pending)
            plan["pending_elements"] = pending
            plans.append(plan)
    except Exception as exc:
        report["errors"].append(
            {"stage": "planning", "error": str(exc)}
        )
        _summarize_paper_outcomes(report)
        report["finished_at"] = _utc_now()
        return report

    expected_counts = _expected_counts_match(
        plans,
        expected_elements,
        expected_references,
    )
    report["expected_counts"] = expected_counts
    if not expected_counts["passed"]:
        report["errors"].append(
            {
                "stage": "expected-count-validation",
                "error": "Extracted counts do not match expected values",
            }
        )
        _summarize_paper_outcomes(report)
        report["finished_at"] = _utc_now()
        return report

    if not dry_run:
        report["papers"] = [
            {
                "paper_id": plan["paper_id"],
                "title": plan["title"],
                "original_filename": plan["original_filename"],
                "pdf_path": plan["pdf_path"],
                "pdf_sha256": plan["pdf_sha256"],
                "page_count": plan["page_count"],
                "expected_elements": plan["expected_elements"],
                "expected_references": plan["expected_references"],
                "existing_elements_before": plan["existing_elements"],
                "inserted_elements": 0,
                "element_set_hash": plan["element_set_hash"],
                "element_types": plan["element_types"],
                "status": "unprocessed",
            }
            for plan in plans
        ]
        paper_reports = {
            int(paper_report["paper_id"]): paper_report
            for paper_report in report["papers"]
        }
        write_connection: sqlite3.Connection | None = None
        current_paper_id: int | None = None
        try:
            write_connection = _connect_read_write(db_path)
            for plan in plans:
                paper_id = int(plan["paper_id"])
                current_paper_id = paper_id
                paper_report = paper_reports[paper_id]
                try:
                    with _paper_write_transaction(write_connection):
                        _ensure_backfill_state_table(write_connection)
                        current_existing = _load_existing_elements(
                            write_connection,
                            paper_id,
                        )
                        pending = _validate_existing_elements(
                            current_existing,
                            plan["elements"],
                        )
                        inserted = insert_elements(
                            write_connection,
                            pending,
                        )
                        _write_backfill_state(
                            write_connection,
                            plan,
                        )
                    paper_report["inserted_elements"] = inserted
                    paper_report["status"] = "completed"
                except Exception as exc:
                    paper_report["status"] = "failed"
                    paper_report["error"] = str(exc)
                    report["errors"].append(
                        {
                            "stage": "apply",
                            "paper_id": paper_id,
                            "error": str(exc),
                        }
                    )
            current_paper_id = None
        except Exception as exc:
            if current_paper_id is not None:
                paper_report = paper_reports.get(current_paper_id)
                if (
                    paper_report is not None
                    and paper_report["status"] == "unprocessed"
                ):
                    paper_report["status"] = "failed"
                    paper_report["error"] = str(exc)
            report["errors"].append(
                {
                    "stage": "apply-outer",
                    "error": str(exc),
                }
            )
        finally:
            if write_connection is not None:
                try:
                    write_connection.close()
                except Exception as exc:
                    report["errors"].append(
                        {
                            "stage": "apply-close",
                            "error": str(exc),
                        }
                    )
    else:
        report["papers"] = [
            {
                "paper_id": plan["paper_id"],
                "title": plan["title"],
                "original_filename": plan["original_filename"],
                "pdf_path": plan["pdf_path"],
                "pdf_sha256": plan["pdf_sha256"],
                "page_count": plan["page_count"],
                "expected_elements": plan["expected_elements"],
                "expected_references": plan["expected_references"],
                "existing_elements_before": plan["existing_elements"],
                "pending_insert_count": plan["pending_insert_count"],
                "element_set_hash": plan["element_set_hash"],
                "element_types": plan["element_types"],
                "status": "planned",
            }
            for plan in plans
        ]

    try:
        after_connection = _connect_read_only(db_path)
        try:
            after = _database_snapshot(after_connection)
            element_links = _validate_element_links(after_connection)
        finally:
            after_connection.close()
        chroma_after = _read_chroma_snapshot(chroma_dir)
        pdf_after = _pdf_hash_snapshot(pdf_inputs)

        report["after"] = after
        report["chroma_after"] = chroma_after
        report["pdf_sha256_before"] = pdf_before
        report["pdf_sha256_after"] = pdf_after
        report["invariants"], invariants_passed = _invariant_report(
            before,
            after,
            chroma_before,
            chroma_after,
            pdf_before,
            pdf_after,
            element_links,
            expected_counts,
            report["mode"],
        )
        for paper in report["papers"]:
            snapshot = _per_paper_snapshot(
                before,
                int(paper["paper_id"]),
            )
            paper["page_chunk_before"] = snapshot
            if dry_run:
                paper["page_chunk_after"] = snapshot
            else:
                paper["page_chunk_after"] = _per_paper_snapshot(
                    after,
                    int(paper["paper_id"]),
                )
    except Exception as exc:
        report["errors"].append(
            {
                "stage": "verification",
                "error": str(exc),
            }
        )
        _summarize_paper_outcomes(report)
        report["success"] = False
        report["finished_at"] = _utc_now()
        return report

    paper_success = all(
        paper["status"] in {"completed", "planned"}
        for paper in report["papers"]
    )
    report["success"] = (
        paper_success
        and invariants_passed
        and not report["errors"]
    )
    _summarize_paper_outcomes(report)
    report["finished_at"] = _utc_now()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill PaperElement rows from original PDFs without "
            "modifying PaperPage, PaperChunk, PDF, or Chroma."
        )
    )
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="SQLite database path",
    )
    parser.add_argument(
        "--pdf-root",
        required=True,
        type=Path,
        help="Root directory containing the original PDFs",
    )
    parser.add_argument(
        "--chroma-dir",
        type=Path,
        default=BACKEND_ROOT / "data" / "chroma_db",
        help="Read-only Chroma persistence directory",
    )
    parser.add_argument(
        "--paper-ids",
        type=_parse_paper_ids,
        default=None,
        help="Comma-separated paper IDs; defaults to all papers",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and report without writing the database",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Insert missing PaperElement rows",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Write the complete JSON report to this path",
    )
    parser.add_argument(
        "--require-unchanged-chunks",
        action="store_true",
        help="Require an exact before/after PaperChunk snapshot match",
    )
    parser.add_argument(
        "--expected-elements",
        type=int,
        default=None,
        help="Expected total extracted PaperElement count",
    )
    parser.add_argument(
        "--expected-references",
        type=int,
        default=None,
        help="Expected total extracted reference element count",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output_path_validated = False
    try:
        _validate_report_output_path(
            args.json,
            args.db,
            args.pdf_root,
            args.chroma_dir,
        )
        output_path_validated = True
        report = backfill_paper_elements(
            db_path=args.db,
            pdf_root=args.pdf_root,
            chroma_dir=args.chroma_dir,
            paper_ids=args.paper_ids,
            dry_run=bool(args.dry_run),
            require_unchanged_chunks=bool(
                args.require_unchanged_chunks
            ),
            expected_elements=args.expected_elements,
            expected_references=args.expected_references,
        )
    except Exception as exc:
        report = {
            "success": False,
            "mode": "dry-run" if args.dry_run else "apply",
            "started_at": _utc_now(),
            "finished_at": _utc_now(),
            "element_backfill_version": ELEMENT_BACKFILL_VERSION,
            "database": str(args.db.resolve()),
            "pdf_root": str(args.pdf_root.resolve()),
            "chroma_dir": str(args.chroma_dir.resolve()),
            "requested_paper_ids": args.paper_ids,
            "papers": [],
            "committed_paper_ids": [],
            "failed_paper_ids": [],
            "unprocessed_paper_ids": [],
            "partial_commit_possible": False,
            "overall_transaction_rolled_back": False,
            "errors": [{"stage": "fatal", "error": str(exc)}],
        }

    payload = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    if args.json is not None and output_path_validated:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
