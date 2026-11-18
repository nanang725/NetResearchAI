from sqlalchemy import create_engine, inspect, text

import app.database as database_module
from app.database import ensure_additive_schema


def test_additive_schema_migration_adds_ocr_page_columns_only():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE paper_pages ("
                "id INTEGER PRIMARY KEY, paper_id INTEGER, page_number INTEGER, "
                "text TEXT, char_count INTEGER)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO paper_pages (id, paper_id, page_number, text, char_count) "
                "VALUES (1, 7, 1, 'existing text', 13)"
            )
        )

    ensure_additive_schema(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("paper_pages")}
    assert {
        "parse_status",
        "ocr_executed",
        "ocr_error",
        "checkpoint_status",
        "checkpoint_generation",
        "checkpoint_worker",
        "checkpoint_error",
        "checkpoint_section_path",
        "checkpoint_in_references",
        "checkpoint_updated_at",
    } <= columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT text, parse_status, ocr_executed, ocr_error, "
                "checkpoint_status, checkpoint_generation, "
                "checkpoint_in_references FROM paper_pages WHERE id=1"
            )
        ).one()
    assert row.text == "existing text"
    assert row.parse_status == "text_extracted"
    assert row.ocr_executed in (False, 0)
    assert row.ocr_error is None
    assert row.checkpoint_status == "pending"
    assert row.checkpoint_generation == 0
    assert row.checkpoint_in_references in (False, 0)
    engine.dispose()


def test_additive_schema_adds_worker_lease_columns_idempotently():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE paper_import_items ("
                "id VARCHAR(36) PRIMARY KEY, status VARCHAR(24) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO paper_import_items (id, status) "
                "VALUES ('item-1', 'queued')"
            )
        )

    ensure_additive_schema(engine)
    ensure_additive_schema(engine)

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("paper_import_items")
    }
    assert {"lease_owner", "lease_expires_at", "lease_generation"} <= columns
    with engine.connect() as connection:
        generation = connection.execute(
            text(
                "SELECT lease_generation FROM paper_import_items WHERE id='item-1'"
            )
        ).scalar_one()
    assert generation == 0
    indexes = {
        index["name"] for index in inspect(engine).get_indexes("paper_import_items")
    }
    assert "ix_paper_import_items_lease_expires_at" in indexes
    engine.dispose()


def test_additive_schema_adds_research_agent_lease_columns_idempotently():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE research_agent_runs ("
                "id VARCHAR(36) PRIMARY KEY, status VARCHAR(32) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO research_agent_runs (id, status) "
                "VALUES ('run-1', 'pending')"
            )
        )

    ensure_additive_schema(engine)
    ensure_additive_schema(engine)

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("research_agent_runs")
    }
    assert {
        "max_replans",
        "lease_owner",
        "lease_expires_at",
        "lease_generation",
        "heartbeat_at",
        "resume_count",
    } <= columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, max_replans, lease_generation, resume_count "
                "FROM research_agent_runs WHERE id='run-1'"
            )
        ).one()
    assert row.status == "pending"
    assert row.max_replans == 1
    assert row.lease_generation == 0
    assert row.resume_count == 0
    indexes = {
        index["name"]
        for index in inspect(engine).get_indexes("research_agent_runs")
    }
    assert "ix_research_agent_runs_lease_expires_at" in indexes
    engine.dispose()


def test_additive_schema_adds_pdf_checkpoint_identity_columns():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE papers ("
                "id INTEGER PRIMARY KEY, file_hash VARCHAR(64), file_size INTEGER)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO papers (id, file_hash, file_size) "
                "VALUES (1, 'existing-hash', 123)"
            )
        )

    ensure_additive_schema(engine)

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("papers")
    }
    assert {
        "parse_checkpoint_version",
        "parse_source_hash",
        "parse_source_size",
        "parse_source_page_count",
        "parse_layout_title",
    } <= columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT file_hash, parse_checkpoint_version, "
                "parse_source_size, parse_source_page_count "
                "FROM papers WHERE id=1"
            )
        ).one()
    assert row.file_hash == "existing-hash"
    assert row.parse_checkpoint_version == 0
    assert row.parse_source_size == 0
    assert row.parse_source_page_count == 0
    engine.dispose()


def test_additive_schema_adds_page_local_chunk_order_with_safe_default():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE paper_chunks ("
                "id INTEGER PRIMARY KEY, paper_id INTEGER, page_number INTEGER, "
                "chunk_index INTEGER, text TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO paper_chunks "
                "(id, paper_id, page_number, chunk_index, text) "
                "VALUES (1, 1, 2, 7, 'legacy chunk')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO paper_chunks "
                "(id, paper_id, page_number, chunk_index, text) "
                "VALUES (2, 1, 2, 8, 'legacy chunk two')"
            )
        )

    ensure_additive_schema(engine)

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("paper_chunks")
    }
    assert "page_chunk_index" in columns
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT page_number, chunk_index, page_chunk_index "
                "FROM paper_chunks ORDER BY id"
            )
        ).all()
    assert rows[0].page_number == 2
    assert rows[0].chunk_index == 7
    assert rows[0].page_chunk_index == 0
    assert rows[1].page_number == 2
    assert rows[1].chunk_index == 8
    assert rows[1].page_chunk_index == 1
    indexes = {
        index["name"] for index in inspect(engine).get_indexes("paper_chunks")
    }
    assert "ix_paper_chunks_page_order" in indexes
    with engine.connect() as connection:
        migration = connection.execute(
            text(
                "SELECT 1 FROM schema_migrations WHERE name=:name"
            ),
            {"name": database_module._PAGE_CHUNK_ORDER_MIGRATION},
        ).first()
    assert migration is not None

    ensure_additive_schema(engine)
    with engine.connect() as connection:
        repeated = connection.execute(
            text(
                "SELECT page_chunk_index FROM paper_chunks ORDER BY id"
            )
        ).scalars().all()
    assert repeated == [0, 1]
    engine.dispose()


def test_page_chunk_order_migration_resumes_after_interruption(
    monkeypatch,
):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE paper_chunks ("
                "id INTEGER PRIMARY KEY, paper_id INTEGER, page_number INTEGER, "
                "chunk_index INTEGER, text TEXT)"
            )
        )
        for row in (
            (1, 1, 1, 10, "page1 chunk1"),
            (2, 1, 1, 11, "page1 chunk2"),
            (3, 1, 1, 12, "page1 chunk3"),
            (4, 2, 2, 50, "page2 new first"),
            (5, 2, 2, 40, "page2 new second"),
        ):
            connection.execute(
                text(
                    "INSERT INTO paper_chunks "
                    "(id, paper_id, page_number, chunk_index, text) "
                    "VALUES (:id, :paper_id, :page, :chunk, :text)"
                ),
                {
                    "id": row[0],
                    "paper_id": row[1],
                    "page": row[2],
                    "chunk": row[3],
                    "text": row[4],
                },
            )

    original_apply = database_module._apply_page_chunk_order_migration

    def fail_before_backfill(*_args, **_kwargs):
        raise RuntimeError("simulated migration interruption")

    monkeypatch.setattr(
        database_module,
        "_apply_page_chunk_order_migration",
        fail_before_backfill,
    )
    try:
        ensure_additive_schema(engine)
    except RuntimeError as exc:
        assert "migration interruption" in str(exc)
    else:
        raise AssertionError("migration interruption was not simulated")

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("paper_chunks")
    }
    assert "page_chunk_index" in columns

    # Simulate an already-correct page produced by the new parser after the
    # interrupted legacy migration.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE paper_chunks SET page_chunk_index=0 WHERE id=4"
            )
        )
        connection.execute(
            text(
                "UPDATE paper_chunks SET page_chunk_index=1 WHERE id=5"
            )
        )

    monkeypatch.setattr(
        database_module,
        "_apply_page_chunk_order_migration",
        original_apply,
    )
    ensure_additive_schema(engine)
    ensure_additive_schema(engine)

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, page_chunk_index FROM paper_chunks ORDER BY id"
            )
        ).all()
        marker = connection.execute(
            text(
                "SELECT 1 FROM schema_migrations WHERE name=:name"
            ),
            {"name": database_module._PAGE_CHUNK_ORDER_MIGRATION},
        ).first()
    assert [(row.id, row.page_chunk_index) for row in rows] == [
        (1, 0),
        (2, 1),
        (3, 2),
        (4, 0),
        (5, 1),
    ]
    assert marker is not None
    engine.dispose()
