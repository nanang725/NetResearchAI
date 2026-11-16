import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def pytest_addoption(parser):
    parser.addoption(
        "--real-pdf-id",
        action="append",
        default=[],
        help=(
            "Run only the selected real PDF sample ID. May be repeated. "
            "Without this option, all available manifest samples run."
        ),
    )
    parser.addoption(
        "--real-pdf-report",
        action="store",
        default=None,
        help=(
            "Write the real PDF regression JSON report to this path. "
            "Defaults to backend/.pytest_cache/real_pdf_regression_report.json."
        ),
    )
    parser.addoption(
        "--run-real-embedding",
        action="store_true",
        default=False,
        help=(
            "Use the existing local BGE-M3 model for real PDF indexing. "
            "The default suite uses deterministic test embeddings."
        ),
    )


def _wait_for_import_worker_stop(_session_factory, stop_event):
    stop_event.wait()


@contextmanager
def _build_isolated_environment(tmp_path, monkeypatch, *, start_worker: bool):
    import app.api.papers as papers_api
    import app.main as main_module
    from app.config import get_settings
    from app.database import Base, get_db

    db_file = tmp_path / "test.db"
    upload_dir = tmp_path / "uploads"
    chroma_dir = tmp_path / "chroma_db"
    upload_dir.mkdir(parents=True)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("CHROMA_DIR", str(chroma_dir))
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    monkeypatch.setenv("OCR_ENABLED", "true")
    get_settings.cache_clear()

    test_engine = create_engine(
        f"sqlite:///{db_file.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    testing_session = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    Base.metadata.create_all(bind=test_engine)

    vector_state = {"deleted": [], "fail_for": set()}

    def delete_vectors(paper_id: int) -> None:
        if paper_id in vector_state["fail_for"]:
            raise RuntimeError("simulated vector cleanup failure")
        vector_state["deleted"].append(paper_id)

    monkeypatch.setattr(main_module, "engine", test_engine)
    monkeypatch.setattr(main_module, "SessionLocal", testing_session)
    monkeypatch.setattr(main_module, "delete_paper_vectors", delete_vectors)
    monkeypatch.setattr(papers_api, "delete_paper_vectors", delete_vectors)
    def mock_index_paper(db, paper_id: int, force: bool = False):
        from app.models.paper import Paper

        paper = db.query(Paper).filter_by(id=paper_id).one()
        paper.index_status = "indexed"
        db.commit()
        return {
            "paper_id": paper_id,
            "status": "indexed",
            "indexed_count": 1,
        }
    monkeypatch.setattr(papers_api, "index_paper", mock_index_paper)
    monkeypatch.setattr(papers_api, "search_chunks_hybrid", lambda **kwargs: [])
    monkeypatch.setattr(papers_api, "filter_relevant_contexts", lambda **kwargs: [])
    monkeypatch.setattr(papers_api, "generate_rag_answer", lambda **kwargs: "mock answer")

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    main_module.app.dependency_overrides[get_db] = override_get_db
    leaked_workers: list[threading.Thread] = []
    try:
        active_workers_before = [
            worker
            for worker in threading.enumerate()
            if worker.name == "paper-import-worker" and worker.is_alive()
        ]
        if active_workers_before:
            raise AssertionError(
                "A paper-import-worker was already alive before starting "
                f"the isolated environment: {active_workers_before!r}"
            )

        if not start_worker:
            monkeypatch.setattr(
                main_module,
                "run_import_worker",
                _wait_for_import_worker_stop,
            )

        with TestClient(main_module.app) as test_client:
            yield {
                "client": test_client,
                "engine": test_engine,
                "session_factory": testing_session,
                "upload_dir": upload_dir,
                "chroma_dir": chroma_dir,
                "vector_state": vector_state,
            }
    finally:
        leaked_workers = [
            worker
            for worker in threading.enumerate()
            if worker.name == "paper-import-worker" and worker.is_alive()
        ]
        main_module.app.dependency_overrides.clear()
        test_engine.dispose()
        get_settings.cache_clear()
    if leaked_workers:
        raise AssertionError(
            "paper-import-worker did not exit after lifespan shutdown: "
            f"{leaked_workers!r}"
        )


@pytest.fixture()
def isolated_environment(tmp_path, monkeypatch):
    with _build_isolated_environment(
        tmp_path,
        monkeypatch,
        start_worker=True,
    ) as environment:
        yield environment


@pytest.fixture()
def isolated_environment_no_worker(tmp_path, monkeypatch):
    with _build_isolated_environment(
        tmp_path,
        monkeypatch,
        start_worker=False,
    ) as environment:
        yield environment


@pytest.fixture()
def client(isolated_environment):
    return isolated_environment["client"]
