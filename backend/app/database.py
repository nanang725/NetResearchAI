from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _build_engine():
    settings = get_settings()
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        future=True,
    )

    if settings.database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


_PAGE_CHUNK_ORDER_MIGRATION = "paper_chunks_page_chunk_index_v1"


def _apply_page_chunk_order_migration(bind, column_added: bool) -> None:
    """Backfill page-local order once, with a resumable migration marker."""
    with bind.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name VARCHAR(128) PRIMARY KEY, "
                "completed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
        )
        completed = connection.execute(
            text(
                "SELECT 1 FROM schema_migrations WHERE name=:name"
            ),
            {"name": _PAGE_CHUNK_ORDER_MIGRATION},
        ).first()
        if completed is not None:
            return

        if column_added:
            connection.execute(
                text(
                    "WITH ranked AS ("
                    "SELECT id, ROW_NUMBER() OVER ("
                    "PARTITION BY paper_id, page_number "
                    "ORDER BY chunk_index ASC, id ASC"
                    ") - 1 AS page_order "
                    "FROM paper_chunks"
                    ") "
                    "UPDATE paper_chunks SET page_chunk_index = ("
                    "SELECT page_order FROM ranked "
                    "WHERE ranked.id = paper_chunks.id"
                    ")"
                )
            )
        else:
            # If a previous run committed ALTER TABLE but stopped before the
            # marker, repair only ambiguous all-zero multi-chunk pages. Correct
            # newly generated pages contain at least one non-zero local index.
            connection.execute(
                text(
                    "WITH ranked AS ("
                    "SELECT id, "
                    "ROW_NUMBER() OVER ("
                    "PARTITION BY paper_id, page_number "
                    "ORDER BY chunk_index ASC, id ASC"
                    ") - 1 AS page_order, "
                    "COUNT(*) OVER (PARTITION BY paper_id, page_number) AS page_size, "
                    "MAX(page_chunk_index) OVER ("
                    "PARTITION BY paper_id, page_number"
                    ") AS max_page_index "
                    "FROM paper_chunks"
                    ") "
                    "UPDATE paper_chunks SET page_chunk_index = ("
                    "SELECT page_order FROM ranked "
                    "WHERE ranked.id = paper_chunks.id"
                    ") WHERE id IN ("
                    "SELECT id FROM ranked "
                    "WHERE page_size > 1 AND max_page_index = 0"
                    ")"
                )
            )

        connection.execute(
            text(
                "INSERT OR IGNORE INTO schema_migrations (name) VALUES (:name)"
            ),
            {"name": _PAGE_CHUNK_ORDER_MIGRATION},
        )


def ensure_additive_schema(bind) -> None:
    """Apply safe additive SQLite changes needed by newer model versions."""
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    additions = {
        "paper_pages": {
            "parse_status": (
                "ALTER TABLE paper_pages ADD COLUMN parse_status "
                "VARCHAR(32) NOT NULL DEFAULT 'text_extracted'"
            ),
            "ocr_executed": (
                "ALTER TABLE paper_pages ADD COLUMN ocr_executed "
                "BOOLEAN NOT NULL DEFAULT 0"
            ),
            "ocr_error": "ALTER TABLE paper_pages ADD COLUMN ocr_error TEXT",
            "checkpoint_status": (
                "ALTER TABLE paper_pages ADD COLUMN checkpoint_status "
                "VARCHAR(16) NOT NULL DEFAULT 'pending'"
            ),
            "checkpoint_generation": (
                "ALTER TABLE paper_pages ADD COLUMN checkpoint_generation "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "checkpoint_worker": (
                "ALTER TABLE paper_pages ADD COLUMN checkpoint_worker VARCHAR(36)"
            ),
            "checkpoint_error": "ALTER TABLE paper_pages ADD COLUMN checkpoint_error TEXT",
            "checkpoint_section_path": (
                "ALTER TABLE paper_pages ADD COLUMN checkpoint_section_path TEXT"
            ),
            "checkpoint_in_references": (
                "ALTER TABLE paper_pages ADD COLUMN checkpoint_in_references "
                "BOOLEAN NOT NULL DEFAULT 0"
            ),
            "checkpoint_updated_at": (
                "ALTER TABLE paper_pages ADD COLUMN checkpoint_updated_at DATETIME"
            ),
        },
        "papers": {
            "doi": "ALTER TABLE papers ADD COLUMN doi VARCHAR(255)",
            "normalized_title": "ALTER TABLE papers ADD COLUMN normalized_title VARCHAR(1024)",
            "publication_year": "ALTER TABLE papers ADD COLUMN publication_year INTEGER",
            "venue": "ALTER TABLE papers ADD COLUMN venue VARCHAR(512)",
            "normalized_venue": "ALTER TABLE papers ADD COLUMN normalized_venue VARCHAR(512)",
            "parse_checkpoint_version": (
                "ALTER TABLE papers ADD COLUMN parse_checkpoint_version "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "parse_source_hash": "ALTER TABLE papers ADD COLUMN parse_source_hash VARCHAR(64)",
            "parse_source_size": (
                "ALTER TABLE papers ADD COLUMN parse_source_size "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "parse_source_page_count": (
                "ALTER TABLE papers ADD COLUMN parse_source_page_count "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "parse_layout_title": (
                "ALTER TABLE papers ADD COLUMN parse_layout_title VARCHAR(512)"
            ),
        },
        "paper_chunks": {
            "page_chunk_index": (
                "ALTER TABLE paper_chunks ADD COLUMN page_chunk_index "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "content_type": (
                "ALTER TABLE paper_chunks ADD COLUMN content_type "
                "VARCHAR(24) NOT NULL DEFAULT 'paragraph'"
            ),
            "element_id": "ALTER TABLE paper_chunks ADD COLUMN element_id INTEGER",
        },
        "paper_import_items": {
            "lease_owner": "ALTER TABLE paper_import_items ADD COLUMN lease_owner VARCHAR(36)",
            "lease_expires_at": "ALTER TABLE paper_import_items ADD COLUMN lease_expires_at DATETIME",
            "lease_generation": (
                "ALTER TABLE paper_import_items ADD COLUMN lease_generation "
                "INTEGER NOT NULL DEFAULT 0"
            ),
        },
        "research_agent_runs": {
            "max_replans": (
                "ALTER TABLE research_agent_runs ADD COLUMN max_replans "
                "INTEGER NOT NULL DEFAULT 1"
            ),
            "lease_owner": (
                "ALTER TABLE research_agent_runs ADD COLUMN lease_owner VARCHAR(36)"
            ),
            "lease_expires_at": (
                "ALTER TABLE research_agent_runs ADD COLUMN lease_expires_at DATETIME"
            ),
            "lease_generation": (
                "ALTER TABLE research_agent_runs ADD COLUMN lease_generation "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "heartbeat_at": (
                "ALTER TABLE research_agent_runs ADD COLUMN heartbeat_at DATETIME"
            ),
            "resume_count": (
                "ALTER TABLE research_agent_runs ADD COLUMN resume_count "
                "INTEGER NOT NULL DEFAULT 0"
            ),
        },
    }

    missing_columns: list[str] = []
    for table_name, table_additions in additions.items():
        if table_name not in tables:
            continue
        existing_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        missing_columns.extend(
            statement
            for name, statement in table_additions.items()
            if name not in existing_columns
        )
    if not missing_columns:
        statements = []
    else:
        statements = missing_columns
    if statements and bind.dialect.name != "sqlite":
        raise RuntimeError(
            "Automatic additive schema updates are supported only for SQLite; "
            "apply an equivalent migration before starting this database."
        )

    page_chunk_index_added = False
    if statements:
        page_chunk_index_added = any(
            "ADD COLUMN page_chunk_index" in statement
            for statement in statements
        )
        with bind.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    if "paper_chunks" in tables:
        _apply_page_chunk_order_migration(
            bind,
            page_chunk_index_added,
        )

    if "papers" in tables:
        with bind.begin() as connection:
            for name in (
                "doi",
                "normalized_title",
                "publication_year",
                "normalized_venue",
            ):
                connection.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS ix_papers_{name} "
                        f"ON papers ({name})"
                    )
                )
    if "paper_import_items" in tables:
        with bind.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_paper_import_items_lease_expires_at "
                    "ON paper_import_items (lease_expires_at)"
                )
            )
    if "research_agent_runs" in tables:
        with bind.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_research_agent_runs_lease_expires_at "
                    "ON research_agent_runs (lease_expires_at)"
                )
            )
    if "paper_pages" in tables:
        with bind.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_paper_pages_checkpoint_status "
                    "ON paper_pages (checkpoint_status)"
                )
            )
    if "paper_chunks" in tables:
        with bind.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_paper_chunks_page_order "
                    "ON paper_chunks (paper_id, page_number, page_chunk_index)"
                )
            )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
