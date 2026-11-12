import io

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    upload_dir = tmp_path / "uploads"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    get_settings.cache_clear()
    upload_dir.mkdir(parents=True, exist_ok=True)

    test_engine = create_engine(
        f"sqlite:///{db_file.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    TestingSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _make_pdf_bytes(title: str = "Computer Networks Survey", pages: list[str] | None = None) -> bytes:
    document = fitz.open()
    texts = pages or [
        f"{title}\nAlice Network Researcher\nThis paper studies congestion control in computer networks.",
        "Section 2\nTCP Cubic and BBR are compared on high bandwidth delay product paths.",
    ]
    for text in texts:
        page = document.new_page()
        page.insert_text((72, 72), text)
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


def test_health(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"


def test_upload_list_detail_delete(client: TestClient):
    pdf_bytes = _make_pdf_bytes()
    response = client.post(
        "/api/papers/upload",
        files={"file": ("networks.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["success"] is True
    paper = body["data"]
    assert paper["page_count"] == 2
    assert paper["pages"][0]["page_number"] == 1
    assert "congestion control" in paper["pages"][0]["text"]
    paper_id = paper["id"]

    listed = client.get("/api/papers")
    assert listed.status_code == 200
    items = listed.json()["data"]
    assert len(items) == 1
    assert items[0]["id"] == paper_id
    assert "pages" not in items[0]

    detail = client.get(f"/api/papers/{paper_id}")
    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert len(detail_data["pages"]) == 2
    assert detail_data["pages"][1]["page_number"] == 2

    deleted = client.delete(f"/api/papers/{paper_id}")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True

    missing = client.get(f"/api/papers/{paper_id}")
    assert missing.status_code == 404
    assert missing.json()["success"] is False


def test_reject_non_pdf(client: TestClient):
    response = client.post(
        "/api/papers/upload",
        files={"file": ("notes.txt", b"not a pdf", "text/plain")},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] in {"INVALID_FILE_TYPE", "INVALID_PDF"}
