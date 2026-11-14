import io
import json

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk, PaperPage
from app.services.paper_service import delete_paper, retry_pending_deletions


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


def _make_chinese_pdf_bytes() -> tuple[bytes, str]:
    text = "计算机网络论文中文文本测试：拥塞控制与数据包调度。"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontname="china-s", fontsize=12)
    document.set_metadata({"title": "中文网络论文测试", "author": "测试作者"})
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue(), text


def _upload(client: TestClient, content: bytes | None = None, filename: str = "networks.pdf"):
    return client.post(
        "/api/papers/upload",
        files={
            "file": (
                filename,
                _make_pdf_bytes() if content is None else content,
                "application/pdf",
            )
        },
    )


def test_health(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"


def test_upload_list_detail_and_serialization(isolated_environment):
    client = isolated_environment["client"]
    response = _upload(client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["success"] is True
    paper = body["data"]
    assert paper["page_count"] == 2
    assert paper["index_status"] == "indexed"
    assert paper["pages"][0]["page_number"] == 1
    assert "congestion control" in paper["pages"][0]["text"]
    assert isinstance(paper["pdf_metadata"], dict)

    listed = client.get("/api/papers")
    assert listed.status_code == 200
    assert listed.json()["data"][0]["id"] == paper["id"]
    assert "pages" not in listed.json()["data"][0]

    detail = client.get(f"/api/papers/{paper['id']}")
    assert detail.status_code == 200
    assert len(detail.json()["data"]["pages"]) == 2
    assert detail.json()["data"]["pages"][1]["page_number"] == 2
    assert (isolated_environment["upload_dir"] / paper["stored_filename"]).is_file()


def test_upload_preserves_extractable_chinese_text(client: TestClient):
    pdf_bytes, expected_text = _make_chinese_pdf_bytes()
    response = _upload(client, pdf_bytes, "chinese-networks.pdf")

    assert response.status_code == 201, response.text
    paper = response.json()["data"]
    extracted_text = paper["pages"][0]["text"]
    assert expected_text in extracted_text
    assert "\ufffd" not in extracted_text
    assert paper["char_count"] == sum(len(page["text"]) for page in paper["pages"])
    assert paper["title"] == "中文网络论文测试"
    assert paper["authors"] == "测试作者"
    assert paper["pages"][0]["char_count"] == len(extracted_text)


@pytest.mark.parametrize(
    ("filename", "content", "expected_status", "expected_code"),
    [
        ("empty.pdf", b"", 400, "EMPTY_FILE"),
        ("notes.txt", b"not a pdf", 400, "INVALID_FILE_TYPE"),
        ("broken.pdf", b"%PDF-1.7\nnot a valid PDF document", 400, "PDF_PARSE_ERROR"),
    ],
)
def test_upload_rejects_invalid_files(
    client: TestClient,
    filename: str,
    content: bytes,
    expected_status: int,
    expected_code: str,
):
    response = _upload(client, content, filename)
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


def test_upload_rejects_oversize_file(isolated_environment):
    client = isolated_environment["client"]
    response = _upload(client, b"x" * (1024 * 1024 + 1), "large.pdf")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert not list(isolated_environment["upload_dir"].glob("*.pdf"))


def test_exact_duplicate_upload_is_rejected(client: TestClient):
    content = _make_pdf_bytes()
    assert _upload(client, content).status_code == 201
    duplicate = _upload(client, content, "same-content.pdf")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_PAPER"


def test_semantic_duplicate_upload_is_rejected(isolated_environment):
    client = isolated_environment["client"]
    first = _upload(client, _make_pdf_bytes("Computer Networks Survey"))
    assert first.status_code == 201

    similar_content = _make_pdf_bytes(
        "Computer Networks Survey Revision",
        pages=[
            "Computer Networks Survey Revision\n"
            "Alice Network Researcher\n"
            "This paper studies congestion control in computer networks.",
            "Section 2\nTCP Cubic and BBR are compared on high bandwidth delay product paths.",
        ],
    )
    second = _upload(client, similar_content, "revised.pdf")

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DUPLICATE_VERSION"
    assert len(list(isolated_environment["upload_dir"].glob("*.pdf"))) == 1


def test_upload_database_commit_failure_removes_file_and_records(
    isolated_environment,
    monkeypatch,
):
    import app.api.papers as papers_api

    original_create_paper = papers_api.create_paper

    def create_with_commit_failure(db, filename, content, content_type):
        def fail_commit(session):
            raise RuntimeError("simulated upload database commit failure")

        event.listen(db, "before_commit", fail_commit, once=True)
        return original_create_paper(db, filename, content, content_type)

    monkeypatch.setattr(papers_api, "create_paper", create_with_commit_failure)
    response = _upload(isolated_environment["client"])

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "PAPER_SAVE_FAILED"
    assert not list(isolated_environment["upload_dir"].glob("*.pdf"))
    with isolated_environment["session_factory"]() as db:
        assert db.query(Paper).count() == 0
        assert db.query(PaperPage).count() == 0
        assert db.query(PaperChunk).count() == 0


def test_index_failure_is_reported_in_paper_state(isolated_environment, monkeypatch):
    import app.api.papers as papers_api

    def fail_index(db, paper_id, force=False):
        raise RuntimeError("simulated indexing failure")

    monkeypatch.setattr(papers_api, "index_paper", fail_index)
    response = _upload(isolated_environment["client"])

    assert response.status_code == 201
    assert response.json()["data"]["index_status"] == "failed"
    with isolated_environment["session_factory"]() as db:
        paper = db.query(Paper).one()
        assert paper.index_status == "failed"
        assert db.query(PaperPage).filter_by(paper_id=paper.id).count() == 2


def test_delete_removes_database_file_and_vectors(isolated_environment):
    client = isolated_environment["client"]
    uploaded = _upload(client)
    paper = uploaded.json()["data"]
    file_path = isolated_environment["upload_dir"] / paper["stored_filename"]

    with isolated_environment["session_factory"]() as db:
        saved = db.query(Paper).one()
        paper_id = saved.id
        assert db.query(PaperPage).filter_by(paper_id=paper_id).count() == 2
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() >= 1

    deleted = client.delete(f"/api/papers/{paper['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"id": paper["id"], "deleted": True}
    assert not file_path.exists()
    assert isolated_environment["vector_state"]["deleted"] == [paper["id"]]

    with isolated_environment["session_factory"]() as db:
        assert db.query(Paper).filter_by(id=paper_id).count() == 0
        assert db.query(PaperPage).filter_by(paper_id=paper_id).count() == 0
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() == 0


def test_vector_delete_failure_is_retried(isolated_environment):
    client = isolated_environment["client"]
    paper = _upload(client).json()["data"]
    state = isolated_environment["vector_state"]
    state["fail_for"].add(paper["id"])

    deleted = client.delete(f"/api/papers/{paper['id']}")
    assert deleted.status_code == 503
    assert deleted.json()["error"]["code"] == "PAPER_CLEANUP_PENDING"
    journal_dir = isolated_environment["upload_dir"] / ".pending_deletions"
    journal_path = journal_dir / f"paper_{paper['id']}.json"
    assert journal_path.is_file()
    assert not (isolated_environment["upload_dir"] / paper["stored_filename"]).exists()

    state["fail_for"].remove(paper["id"])
    retry_pending_deletions(
        isolated_environment["session_factory"],
        lambda paper_id: state["deleted"].append(paper_id),
        isolated_environment["upload_dir"],
    )

    assert state["deleted"] == [paper["id"]]
    assert not journal_path.exists()


def test_delete_outside_upload_directory_keeps_cleanup_pending(
    isolated_environment,
    tmp_path,
):
    client = isolated_environment["client"]
    paper = _upload(client).json()["data"]
    outside_dir = tmp_path / "outside-uploads"
    outside_dir.mkdir()
    outside_file = outside_dir / paper["stored_filename"]
    outside_file.write_bytes(b"preserve file outside configured upload directory")

    with isolated_environment["session_factory"]() as db:
        saved = db.query(Paper).filter_by(id=paper["id"]).one()
        saved.file_path = str(outside_file)
        db.commit()

    response = client.delete(f"/api/papers/{paper['id']}")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PAPER_CLEANUP_PENDING"
    assert outside_file.is_file()

    journal_path = (
        isolated_environment["upload_dir"]
        / ".pending_deletions"
        / f"paper_{paper['id']}.json"
    )
    assert journal_path.is_file()
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["file_pending"] is True
    assert journal["vector_pending"] is False

    journal["file_pending"] = False
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    retry_pending_deletions(
        isolated_environment["session_factory"],
        lambda _paper_id: None,
        isolated_environment["upload_dir"],
    )

    assert journal_path.is_file()
    assert json.loads(journal_path.read_text(encoding="utf-8"))["file_pending"] is True
    assert outside_file.is_file()


def test_database_delete_failure_preserves_pdf_and_record(isolated_environment):
    client = isolated_environment["client"]
    paper_data = _upload(client).json()["data"]
    file_path = isolated_environment["upload_dir"] / paper_data["stored_filename"]

    with isolated_environment["session_factory"]() as db:
        paper_id = paper_data["id"]

        def fail_commit(session):
            raise RuntimeError("simulated database commit failure")

        event.listen(db, "before_commit", fail_commit, once=True)
        with pytest.raises(AppError) as error:
            delete_paper(db, paper_id)

        assert error.value.code == "PAPER_DELETE_FAILED"
        assert db.query(Paper).filter_by(id=paper_id).count() == 1
        assert file_path.is_file()
        assert not (
            isolated_environment["upload_dir"]
            / ".pending_deletions"
            / f"paper_{paper_id}.json"
        ).exists()


def test_delete_missing_paper_returns_standard_404(client: TestClient):
    response = client.delete("/api/papers/987654")
    assert response.status_code == 404
    assert response.json()["success"] is False
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_repeated_delete_returns_standard_404(client: TestClient):
    paper = _upload(client).json()["data"]
    assert client.delete(f"/api/papers/{paper['id']}").status_code == 200
    repeated = client.delete(f"/api/papers/{paper['id']}")
    assert repeated.status_code == 404
    assert repeated.json()["error"]["code"] == "NOT_FOUND"


def test_agent_chat_keeps_post_query_parameter_contract(isolated_environment):
    client = isolated_environment["client"]
    paper = _upload(client).json()["data"]

    response = client.post(
        "/api/papers/agent/chat",
        params={"query": "congestion control", "paper_id": paper["id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["is_relevant"] is False
    assert body["data"]["contexts"] == []


def test_recovery_cancels_journal_when_database_row_remains(isolated_environment):
    paper = _upload(isolated_environment["client"]).json()["data"]
    journal_dir = isolated_environment["upload_dir"] / ".pending_deletions"
    journal_dir.mkdir()
    journal_path = journal_dir / f"paper_{paper['id']}.json"
    journal_path.write_text(
        json.dumps(
            {
                "paper_id": paper["id"],
                "file_path": str(
                    isolated_environment["upload_dir"] / paper["stored_filename"]
                ),
                "vector_pending": True,
                "file_pending": True,
            }
        ),
        encoding="utf-8",
    )

    retry_pending_deletions(
        isolated_environment["session_factory"],
        lambda paper_id: isolated_environment["vector_state"]["deleted"].append(paper_id),
        isolated_environment["upload_dir"],
    )

    assert not journal_path.exists()
    assert (isolated_environment["upload_dir"] / paper["stored_filename"]).is_file()
    assert isolated_environment["vector_state"]["deleted"] == []
