from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import fitz
import pytest

from app.config import get_settings
from app.services import pdf_service
from scripts import backfill_paper_elements as backfill


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
MANIFEST_PATH = (
    BACKEND_ROOT / "tests" / "fixtures" / "real_pdf_manifest.json"
)


HISTORICAL_SCHEMA = """
CREATE TABLE papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title VARCHAR(512) NOT NULL,
    file_hash VARCHAR(64) NOT NULL UNIQUE,
    original_filename VARCHAR(512) NOT NULL,
    stored_filename VARCHAR(255) NOT NULL UNIQUE,
    file_path VARCHAR(1024) NOT NULL,
    file_size INTEGER NOT NULL,
    content_type VARCHAR(128) NOT NULL,
    page_count INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    authors VARCHAR(1024),
    pdf_metadata TEXT,
    parse_status VARCHAR(32) NOT NULL,
    index_status VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE paper_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    parse_status VARCHAR(32) NOT NULL,
    ocr_executed BOOLEAN NOT NULL DEFAULT 0,
    ocr_error TEXT,
    UNIQUE(paper_id, page_number)
);
CREATE TABLE paper_elements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    page_id INTEGER NOT NULL REFERENCES paper_pages(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    element_index INTEGER NOT NULL,
    element_type VARCHAR(32) NOT NULL,
    text TEXT NOT NULL,
    data TEXT,
    bbox TEXT,
    section_path TEXT,
    extraction_status VARCHAR(32) NOT NULL,
    confidence FLOAT,
    UNIQUE(page_id, element_index)
);
CREATE TABLE paper_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_type VARCHAR(24) NOT NULL DEFAULT 'paragraph',
    element_id INTEGER REFERENCES paper_elements(id) ON DELETE SET NULL
);
"""


def _pdf_bytes(
    *,
    title: str,
    page_count: int = 2,
    include_references: bool = True,
) -> bytes:
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), title, fontsize=16)
    first.insert_text(
        (72, 110),
        "Stable network systems research evidence.",
        fontsize=11,
    )
    if include_references:
        first.insert_text((72, 180), "References", fontsize=13)
        first.insert_text(
            (72, 210),
            "[1] Alice Smith. Safe Maintenance. 2024.",
            fontsize=11,
        )
        first.insert_text(
            (72, 235),
            "[2] Bob Jones. Reliable Backfill. 2025.",
            fontsize=11,
        )
    for page_number in range(2, page_count + 1):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Page {page_number} contains stable research content.",
            fontsize=11,
        )
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _build_chroma_fixture(chroma_dir: Path) -> Path:
    chroma_dir.mkdir(parents=True)
    database_path = chroma_dir / "chroma.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                embedding_id TEXT NOT NULL,
                seq_id INTEGER NOT NULL
            );
            CREATE TABLE embedding_metadata (
                id INTEGER NOT NULL,
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO embeddings (id, embedding_id, seq_id) "
            "VALUES (1, 'chunk_1', 1)"
        )
        connection.executemany(
            "INSERT INTO embedding_metadata "
            "(id, key, string_value, int_value, float_value, bool_value) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "paper_id", None, 1, None, None),
                (1, "chunk_id", None, 1, None, None),
                (1, "index_fingerprint", "fixture-fingerprint", None, None, None),
                (1, "content_hash", "fixture-content-hash", None, None, None),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return database_path


def _snapshot_tables(database_path: Path) -> dict[str, list[dict[str, Any]]]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return {
            table: [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY id"
                )
            ]
            for table in (
                "papers",
                "paper_pages",
                "paper_chunks",
                "paper_elements",
            )
        }
    finally:
        connection.close()


def _table_exists(database_path: Path, table_name: str) -> bool:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone() is not None
    finally:
        connection.close()


def _set_paper_file_path(
    database_path: Path,
    paper_id: int,
    file_path: Path,
) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE papers SET file_path=? WHERE id=?",
            (str(file_path), paper_id),
        )
        connection.commit()
    finally:
        connection.close()


def _path_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_historical_database(
    root: Path,
    samples: list[tuple[str, bytes]],
) -> tuple[Path, Path, Path, list[int]]:
    pdf_root = root / "uploads"
    pdf_root.mkdir()
    database_path = root / "historical.db"
    chroma_dir = root / "chroma_db"
    _build_chroma_fixture(chroma_dir)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(HISTORICAL_SCHEMA)
        connection.execute("PRAGMA foreign_keys=ON")
        paper_ids = []
        for filename, content in samples:
            pdf_path = pdf_root / filename
            pdf_path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            document = fitz.open(stream=content, filetype="pdf")
            try:
                page_rows = [
                    page.get_text("text") or ""
                    for page in document
                ]
            finally:
                document.close()
            cursor = connection.execute(
                "INSERT INTO papers ("
                "title, file_hash, original_filename, stored_filename, "
                "file_path, file_size, content_type, page_count, "
                "char_count, authors, parse_status, index_status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    Path(filename).stem,
                    digest,
                    filename,
                    filename,
                    str(pdf_path),
                    len(content),
                    "application/pdf",
                    len(page_rows),
                    sum(len(text) for text in page_rows),
                    "Legacy Author",
                    "parsed",
                    "indexed",
                ),
            )
            paper_id = int(cursor.lastrowid)
            paper_ids.append(paper_id)
            page_ids = []
            for page_number, text in enumerate(page_rows, start=1):
                page_cursor = connection.execute(
                    "INSERT INTO paper_pages ("
                    "paper_id, page_number, text, char_count, parse_status"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        paper_id,
                        page_number,
                        text,
                        len(text),
                        "text_extracted",
                    ),
                )
                page_ids.append(int(page_cursor.lastrowid))
            for page_number, text in enumerate(page_rows, start=1):
                connection.execute(
                    "INSERT INTO paper_chunks ("
                    "paper_id, page_number, chunk_index, text, "
                    "content_type, element_id"
                    ") VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        paper_id,
                        page_number,
                        page_number - 1,
                        text[:500],
                        "paragraph",
                    ),
                )
        connection.commit()
    finally:
        connection.close()
    return database_path, pdf_root, chroma_dir, paper_ids


def _run(
    database_path: Path,
    pdf_root: Path,
    chroma_dir: Path,
    *,
    dry_run: bool,
    paper_ids: list[int] | None = None,
    expected_elements: int | None = None,
    expected_references: int | None = None,
    insert_elements=None,
) -> dict[str, Any]:
    kwargs = {}
    if insert_elements is not None:
        kwargs["insert_elements"] = insert_elements
    try:
        return backfill.backfill_paper_elements(
            db_path=database_path,
            pdf_root=pdf_root,
            chroma_dir=chroma_dir,
            paper_ids=paper_ids,
            dry_run=dry_run,
            require_unchanged_chunks=True,
            expected_elements=expected_elements,
            expected_references=expected_references,
            **kwargs,
        )
    except Exception as exc:
        return {
            "success": False,
            "mode": "dry-run" if dry_run else "apply",
            "papers": [],
            "errors": [{"stage": "fatal", "error": str(exc)}],
        }


def _cli_args(
    env: dict[str, Any],
    json_path: Path,
    *,
    dry_run: bool = True,
) -> list[str]:
    return [
        "--db",
        str(env["db"]),
        "--pdf-root",
        str(env["pdf_root"]),
        "--chroma-dir",
        str(env["chroma"]),
        "--paper-ids",
        str(env["paper_ids"][0]),
        "--dry-run" if dry_run else "--apply",
        "--json",
        str(json_path),
    ]


@pytest.fixture()
def isolated_backfill_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()
    content = _pdf_bytes(title="Historical Maintenance Paper")
    database_path, pdf_root, chroma_dir, paper_ids = (
        _create_historical_database(
            tmp_path,
            [("historical.pdf", content)],
        )
    )
    yield {
        "db": database_path,
        "pdf_root": pdf_root,
        "chroma": chroma_dir,
        "paper_ids": paper_ids,
        "content": content,
    }
    get_settings.cache_clear()


def test_dry_run_does_not_write_database(isolated_backfill_env):
    env = isolated_backfill_env
    before = _snapshot_tables(env["db"])
    database_hash = _path_sha256(env["db"])
    chroma_hash = _path_sha256(env["chroma"] / "chroma.sqlite3")

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=True,
    )

    assert report["success"] is True
    assert report["papers"][0]["status"] == "planned"
    assert report["papers"][0]["pending_insert_count"] > 0
    assert _snapshot_tables(env["db"]) == before
    assert _path_sha256(env["db"]) == database_hash
    assert _path_sha256(env["chroma"] / "chroma.sqlite3") == chroma_hash
    assert not _table_exists(
        env["db"],
        backfill.BACKFILL_STATE_TABLE,
    )


def test_apply_preserves_pages_chunks_pdf_and_chroma(
    isolated_backfill_env,
):
    env = isolated_backfill_env
    before = _snapshot_tables(env["db"])
    pdf_hash = _path_sha256(env["pdf_root"] / "historical.pdf")
    chroma_hash = _path_sha256(env["chroma"] / "chroma.sqlite3")

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )
    after = _snapshot_tables(env["db"])

    assert report["success"] is True
    assert report["invariants"]["page_ids_numbers_text_unchanged"]["passed"]
    assert report["invariants"][
        "chunk_ids_content_order_element_id_unchanged"
    ]["passed"]
    assert report["invariants"]["chroma_directory_unchanged"]["passed"]
    assert after["papers"] == before["papers"]
    assert after["paper_pages"] == before["paper_pages"]
    assert after["paper_chunks"] == before["paper_chunks"]
    assert len(after["paper_elements"]) > 0
    assert (
        sum(
            element["element_type"] == "reference"
            for element in after["paper_elements"]
        )
        > 0
    )
    assert _path_sha256(env["pdf_root"] / "historical.pdf") == pdf_hash
    assert _path_sha256(env["chroma"] / "chroma.sqlite3") == chroma_hash


def test_page_id_mapping_is_exact(isolated_backfill_env):
    env = isolated_backfill_env
    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )
    assert report["success"] is True

    connection = sqlite3.connect(env["db"])
    connection.row_factory = sqlite3.Row
    try:
        elements = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM paper_elements ORDER BY page_number, element_index"
            )
        ]
        pages = {
            (int(row["paper_id"]), int(row["page_number"])): int(row["id"])
            for row in connection.execute(
                "SELECT id, paper_id, page_number FROM paper_pages"
            )
        }
    finally:
        connection.close()

    assert elements
    assert all(
        element["page_id"]
        == pages[(element["paper_id"], element["page_number"])]
        for element in elements
    )


def test_repeated_apply_is_idempotent(isolated_backfill_env):
    env = isolated_backfill_env
    first = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )
    first_elements = _snapshot_tables(env["db"])["paper_elements"]
    second = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )
    second_elements = _snapshot_tables(env["db"])["paper_elements"]

    assert first["success"] is True
    assert second["success"] is True
    assert first["papers"][0]["inserted_elements"] > 0
    assert second["papers"][0]["inserted_elements"] == 0
    assert second_elements == first_elements


def test_missing_pdf_fails_without_writes(isolated_backfill_env):
    env = isolated_backfill_env
    (env["pdf_root"] / "historical.pdf").unlink()
    before = _snapshot_tables(env["db"])

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )

    assert report["success"] is False
    assert report["errors"][0]["stage"] == "fatal"
    assert _snapshot_tables(env["db"]) == before


def test_external_absolute_path_maps_to_hashed_staging_file(
    isolated_backfill_env,
):
    env = isolated_backfill_env
    outside_dir = env["db"].parent / "outside_original"
    outside_dir.mkdir()
    outside_path = outside_dir / "historical.pdf"
    outside_path.write_bytes(_pdf_bytes(title="External Original"))
    _set_paper_file_path(
        env["db"],
        env["paper_ids"][0],
        outside_path,
    )

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=True,
    )

    assert report["success"] is True
    staged_path = Path(report["papers"][0]["pdf_path"]).resolve()
    staged_path.relative_to(env["pdf_root"].resolve())
    assert staged_path.name == outside_path.name
    assert _path_sha256(staged_path) == hashlib.sha256(env["content"]).hexdigest()
    assert _path_sha256(outside_path) != _path_sha256(staged_path)


def test_same_name_staging_file_with_wrong_hash_is_rejected(
    isolated_backfill_env,
):
    env = isolated_backfill_env
    outside_dir = env["db"].parent / "outside_original"
    outside_dir.mkdir()
    outside_path = outside_dir / "historical.pdf"
    outside_path.write_bytes(env["content"])
    _set_paper_file_path(
        env["db"],
        env["paper_ids"][0],
        outside_path,
    )
    (env["pdf_root"] / "historical.pdf").write_bytes(
        _pdf_bytes(title="Wrong Staging Copy")
    )
    before = _snapshot_tables(env["db"])

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=True,
    )

    assert report["success"] is False
    assert "PDF SHA-256 mismatch inside --pdf-root" in (
        report["errors"][0]["error"]
    )
    assert _snapshot_tables(env["db"]) == before


def test_missing_staging_file_does_not_fallback_to_external_original(
    isolated_backfill_env,
):
    env = isolated_backfill_env
    outside_dir = env["db"].parent / "outside_original"
    outside_dir.mkdir()
    outside_path = outside_dir / "historical.pdf"
    outside_path.write_bytes(env["content"])
    _set_paper_file_path(
        env["db"],
        env["paper_ids"][0],
        outside_path,
    )
    (env["pdf_root"] / "historical.pdf").unlink()
    before = _snapshot_tables(env["db"])

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=True,
    )

    assert report["success"] is False
    assert "PDF does not exist inside --pdf-root" in (
        report["errors"][0]["error"]
    )
    assert outside_path.is_file()
    assert _snapshot_tables(env["db"]) == before


def test_external_absolute_path_without_staging_file_is_rejected(
    isolated_backfill_env,
):
    env = isolated_backfill_env
    outside_dir = env["db"].parent / "outside_original"
    outside_dir.mkdir()
    outside_path = outside_dir / "different-name.pdf"
    outside_path.write_bytes(env["content"])
    _set_paper_file_path(
        env["db"],
        env["paper_ids"][0],
        outside_path,
    )
    before = _snapshot_tables(env["db"])

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=True,
    )

    assert report["success"] is False
    assert "PDF does not exist inside --pdf-root" in (
        report["errors"][0]["error"]
    )
    assert _snapshot_tables(env["db"]) == before


def test_pdf_hash_mismatch_fails_without_writes(isolated_backfill_env):
    env = isolated_backfill_env
    pdf_path = env["pdf_root"] / "historical.pdf"
    pdf_path.write_bytes(_pdf_bytes(title="Different PDF"))
    before = _snapshot_tables(env["db"])

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )

    assert report["success"] is False
    assert "SHA-256 mismatch" in report["errors"][0]["error"]
    assert _snapshot_tables(env["db"]) == before


def test_mid_apply_exception_rolls_back_current_paper(
    isolated_backfill_env,
):
    env = isolated_backfill_env

    def insert_then_fail(connection, elements):
        backfill._insert_new_elements(connection, elements)
        raise RuntimeError("simulated insertion failure")

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
        insert_elements=insert_then_fail,
    )

    assert report["success"] is False
    assert report["papers"][0]["status"] == "failed"
    assert _snapshot_tables(env["db"])["paper_elements"] == []
    connection = sqlite3.connect(env["db"])
    try:
        marker_count = (
            connection.execute(
                f"SELECT COUNT(*) FROM {backfill.BACKFILL_STATE_TABLE}"
            ).fetchone()[0]
            if _table_exists(env["db"], backfill.BACKFILL_STATE_TABLE)
            else 0
        )
    finally:
        connection.close()
    assert marker_count == 0


def test_papers_can_be_processed_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()
    database_path, pdf_root, chroma_dir, paper_ids = (
        _create_historical_database(
            tmp_path,
            [
                ("first.pdf", _pdf_bytes(title="First Paper")),
                ("second.pdf", _pdf_bytes(title="Second Paper")),
            ],
        )
    )
    first_report = _run(
        database_path,
        pdf_root,
        chroma_dir,
        dry_run=False,
        paper_ids=[paper_ids[0]],
    )
    connection = sqlite3.connect(database_path)
    try:
        first_counts = dict(
            connection.execute(
                "SELECT paper_id, COUNT(*) FROM paper_elements "
                "GROUP BY paper_id ORDER BY paper_id"
            ).fetchall()
        )
    finally:
        connection.close()

    second_report = _run(
        database_path,
        pdf_root,
        chroma_dir,
        dry_run=False,
        paper_ids=[paper_ids[1]],
    )
    connection = sqlite3.connect(database_path)
    try:
        final_counts = dict(
            connection.execute(
                "SELECT paper_id, COUNT(*) FROM paper_elements "
                "GROUP BY paper_id ORDER BY paper_id"
            ).fetchall()
        )
    finally:
        connection.close()

    assert first_report["success"] is True
    assert first_counts == {paper_ids[0]: first_report["papers"][0]["expected_elements"]}
    assert second_report["success"] is True
    assert final_counts[paper_ids[0]] == first_counts[paper_ids[0]]
    assert (
        final_counts[paper_ids[1]]
        == second_report["papers"][0]["expected_elements"]
    )
    get_settings.cache_clear()


def test_failed_paper_does_not_invalidate_completed_paper(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()
    database_path, pdf_root, chroma_dir, paper_ids = (
        _create_historical_database(
            tmp_path,
            [
                ("first.pdf", _pdf_bytes(title="First Paper")),
                ("second.pdf", _pdf_bytes(title="Second Paper")),
            ],
        )
    )
    before = _snapshot_tables(database_path)
    failure_state = {"failed": False}

    def fail_second(connection, elements):
        if (
            elements
            and elements[0]["paper_id"] == paper_ids[1]
            and not failure_state["failed"]
        ):
            failure_state["failed"] = True
            raise RuntimeError("simulated second-paper failure")
        return backfill._insert_new_elements(connection, elements)

    first_report = _run(
        database_path,
        pdf_root,
        chroma_dir,
        dry_run=False,
        insert_elements=fail_second,
    )
    after_first = _snapshot_tables(database_path)

    assert first_report["success"] is False
    assert [paper["status"] for paper in first_report["papers"]] == [
        "completed",
        "failed",
    ]
    assert first_report["committed_paper_ids"] == [paper_ids[0]]
    assert first_report["failed_paper_ids"] == [paper_ids[1]]
    assert first_report["unprocessed_paper_ids"] == []
    assert first_report["partial_commit_possible"] is True
    assert first_report["overall_transaction_rolled_back"] is False
    assert after_first["papers"] == before["papers"]
    assert after_first["paper_pages"] == before["paper_pages"]
    assert after_first["paper_chunks"] == before["paper_chunks"]
    assert any(
        element["paper_id"] == paper_ids[0]
        for element in after_first["paper_elements"]
    )
    assert not any(
        element["paper_id"] == paper_ids[1]
        for element in after_first["paper_elements"]
    )
    connection = sqlite3.connect(database_path)
    try:
        marker_ids = {
            int(row[0])
            for row in connection.execute(
                f"SELECT paper_id FROM {backfill.BACKFILL_STATE_TABLE}"
            )
        }
    finally:
        connection.close()
    assert marker_ids == {paper_ids[0]}

    second_report = _run(
        database_path,
        pdf_root,
        chroma_dir,
        dry_run=False,
        insert_elements=fail_second,
    )
    after_second = _snapshot_tables(database_path)
    first_inserted = {
        paper["paper_id"]: paper["inserted_elements"]
        for paper in second_report["papers"]
    }

    assert second_report["success"] is True
    assert first_inserted == {
        paper_ids[0]: 0,
        paper_ids[1]: second_report["papers"][1]["expected_elements"],
    }
    assert second_report["committed_paper_ids"] == paper_ids
    assert second_report["failed_paper_ids"] == []
    assert second_report["unprocessed_paper_ids"] == []
    assert second_report["partial_commit_possible"] is False
    assert after_second["papers"] == before["papers"]
    assert after_second["paper_pages"] == before["paper_pages"]
    assert after_second["paper_chunks"] == before["paper_chunks"]
    assert any(
        element["paper_id"] == paper_ids[1]
        for element in after_second["paper_elements"]
    )
    connection = sqlite3.connect(database_path)
    try:
        marker_ids = {
            int(row[0])
            for row in connection.execute(
                f"SELECT paper_id FROM {backfill.BACKFILL_STATE_TABLE}"
            )
        }
        marker_counts = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                f"SELECT paper_id, element_count "
                f"FROM {backfill.BACKFILL_STATE_TABLE}"
            )
        }
    finally:
        connection.close()
    assert marker_ids == set(paper_ids)
    assert marker_counts == {
        paper["paper_id"]: paper["expected_elements"]
        for paper in second_report["papers"]
    }
    get_settings.cache_clear()


def test_post_apply_verification_failure_preserves_committed_report(
    isolated_backfill_env,
    monkeypatch,
):
    env = isolated_backfill_env
    original_read_chroma = backfill._read_chroma_snapshot
    call_count = {"value": 0}

    def fail_after_apply(chroma_dir):
        call_count["value"] += 1
        if call_count["value"] == 2:
            raise RuntimeError("simulated post-apply verification failure")
        return original_read_chroma(chroma_dir)

    monkeypatch.setattr(
        backfill,
        "_read_chroma_snapshot",
        fail_after_apply,
    )
    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )
    after = _snapshot_tables(env["db"])

    assert report["success"] is False
    assert report["errors"][-1]["stage"] == "verification"
    assert report["papers"][0]["status"] == "completed"
    assert report["committed_paper_ids"] == env["paper_ids"]
    assert report["failed_paper_ids"] == []
    assert report["unprocessed_paper_ids"] == []
    assert report["overall_transaction_rolled_back"] is False
    assert any(
        element["paper_id"] == env["paper_ids"][0]
        for element in after["paper_elements"]
    )


def test_planning_failure_reports_papers_as_unprocessed(
    isolated_backfill_env,
    monkeypatch,
):
    env = isolated_backfill_env
    before = _snapshot_tables(env["db"])

    def fail_plan(*_args, **_kwargs):
        raise RuntimeError("simulated planning failure")

    monkeypatch.setattr(backfill, "_build_plan", fail_plan)
    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )

    assert report["success"] is False
    assert report["committed_paper_ids"] == []
    assert report["failed_paper_ids"] == []
    assert report["unprocessed_paper_ids"] == env["paper_ids"]
    assert report["partial_commit_possible"] is False
    assert report["overall_transaction_rolled_back"] is False
    assert report["papers"][0]["status"] == "unprocessed"
    assert _snapshot_tables(env["db"]) == before


def test_unexpected_existing_element_fails_without_overwrite(
    isolated_backfill_env,
):
    env = isolated_backfill_env
    connection = sqlite3.connect(env["db"])
    try:
        page_id = connection.execute(
            "SELECT id FROM paper_pages ORDER BY page_number LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO paper_elements ("
            "paper_id, page_id, page_number, element_index, element_type, "
            "text, extraction_status"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                env["paper_ids"][0],
                page_id,
                1,
                0,
                "paragraph",
                "unexpected content",
                "extracted",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    before = _snapshot_tables(env["db"])

    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
    )

    assert report["success"] is False
    assert _snapshot_tables(env["db"]) == before


def test_expected_count_mismatch_prevents_writes(
    isolated_backfill_env,
):
    env = isolated_backfill_env
    before = _snapshot_tables(env["db"])
    report = _run(
        env["db"],
        env["pdf_root"],
        env["chroma"],
        dry_run=False,
        expected_elements=999,
    )

    assert report["success"] is False
    assert report["expected_counts"]["passed"] is False
    assert _snapshot_tables(env["db"]) == before


def test_cli_dry_run_writes_json_report_only(
    isolated_backfill_env,
    capsys,
):
    env = isolated_backfill_env
    report_path = env["db"].parent / "report.json"
    database_hash = _path_sha256(env["db"])

    exit_code = backfill.main(
        [
            "--db",
            str(env["db"]),
            "--pdf-root",
            str(env["pdf_root"]),
            "--chroma-dir",
            str(env["chroma"]),
            "--paper-ids",
            str(env["paper_ids"][0]),
            "--dry-run",
            "--require-unchanged-chunks",
            "--json",
            str(report_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["success"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["success"] is True
    assert report["mode"] == "dry-run"
    assert report["papers"][0]["status"] == "planned"
    assert _path_sha256(env["db"]) == database_hash
    assert _snapshot_tables(env["db"])["paper_elements"] == []


def test_cli_rejects_json_path_equal_to_database(
    isolated_backfill_env,
    capsys,
):
    env = isolated_backfill_env
    database_hash = _path_sha256(env["db"])

    exit_code = backfill.main(_cli_args(env, env["db"]))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "--json must not overwrite" in json.loads(captured.out)["errors"][0]["error"]
    assert _path_sha256(env["db"]) == database_hash
    assert _snapshot_tables(env["db"])["paper_elements"] == []


def test_cli_rejects_json_path_inside_pdf_root(
    isolated_backfill_env,
    capsys,
):
    env = isolated_backfill_env
    report_path = env["pdf_root"] / "dry-run.json"
    database_hash = _path_sha256(env["db"])

    exit_code = backfill.main(_cli_args(env, report_path))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "--json must not be located inside --pdf-root" in (
        json.loads(captured.out)["errors"][0]["error"]
    )
    assert not report_path.exists()
    assert _path_sha256(env["db"]) == database_hash
    assert _snapshot_tables(env["db"])["paper_elements"] == []


def test_cli_rejects_json_path_inside_chroma_dir(
    isolated_backfill_env,
    capsys,
):
    env = isolated_backfill_env
    report_path = env["chroma"] / "dry-run.json"
    database_hash = _path_sha256(env["db"])
    chroma_hash = _path_sha256(env["chroma"] / "chroma.sqlite3")

    exit_code = backfill.main(_cli_args(env, report_path))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "--json must not be located inside --chroma-dir" in (
        json.loads(captured.out)["errors"][0]["error"]
    )
    assert not report_path.exists()
    assert _path_sha256(env["db"]) == database_hash
    assert _path_sha256(env["chroma"] / "chroma.sqlite3") == chroma_hash


@pytest.mark.real_pdf
def test_real_pdf_four_sample_backfill_counts_and_invariants(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    samples = [
        sample
        for sample in manifest["samples"]
        if sample["id"] in {"P02", "P04", "P08", "P09"}
    ]
    contents = []
    for sample in samples:
        path = PROJECT_ROOT / sample["path"]
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == sample["sha256"]
        contents.append((path.name, content))

    database_path, pdf_root, chroma_dir, _paper_ids = (
        _create_historical_database(tmp_path, contents)
    )
    before = _snapshot_tables(database_path)
    chroma_hash = _path_sha256(chroma_dir / "chroma.sqlite3")
    report = _run(
        database_path,
        pdf_root,
        chroma_dir,
        dry_run=False,
        expected_elements=1013,
        expected_references=137,
    )
    after = _snapshot_tables(database_path)

    assert report["success"] is True
    assert report["before"]["element_count"] == 0
    assert report["after"]["element_count"] == 1013
    assert report["after"]["reference_count"] == 137
    assert after["papers"] == before["papers"]
    assert after["paper_pages"] == before["paper_pages"]
    assert after["paper_chunks"] == before["paper_chunks"]
    assert _path_sha256(chroma_dir / "chroma.sqlite3") == chroma_hash
    assert report["invariants"]["chroma_vector_count_unchanged"]["passed"]
    assert report["invariants"]["chroma_fingerprint_unchanged"]["passed"]
    assert report["invariants"]["chroma_directory_unchanged"]["passed"]
    print(
        json.dumps(
            {
                "totals": {
                    "elements": report["after"]["element_count"],
                    "references": report["after"]["reference_count"],
                },
                "papers": [
                    {
                        "paper_id": paper["paper_id"],
                        "filename": paper["original_filename"],
                        "elements": paper["expected_elements"],
                        "references": paper["expected_references"],
                        "pages": paper["page_count"],
                        "chunks_before": paper[
                            "page_chunk_before"
                        ]["chunk_count"],
                    }
                    for paper in report["papers"]
                ],
                "page_chunk_unchanged": (
                    report["invariants"][
                        "page_ids_numbers_text_unchanged"
                    ]["passed"]
                    and report["invariants"][
                        "chunk_ids_content_order_element_id_unchanged"
                    ]["passed"]
                ),
                "chroma_unchanged": (
                    report["invariants"][
                        "chroma_vector_count_unchanged"
                    ]["passed"]
                    and report["invariants"][
                        "chroma_fingerprint_unchanged"
                    ]["passed"]
                    and report["invariants"][
                        "chroma_directory_unchanged"
                    ]["passed"]
                ),
            },
            ensure_ascii=False,
        )
    )
    get_settings.cache_clear()
