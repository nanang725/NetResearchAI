import io
import json
import time

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


def _make_ieee_title_pdf_bytes() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 32),
        "Date of publication 2024 00, 0000, date of current version 2024 00, 0000.",
        fontsize=8,
    )
    page.insert_textbox(
        fitz.Rect(72, 90, 530, 180),
        "Computing in Integrated Terrestrial and Non-Terrestrial Networks: "
        "A Comprehensive Survey",
        fontsize=18,
    )
    document.set_metadata(
        {"title": "Date of publication 2024 00, 0000, date of current version 2024 00, 0000."}
    )
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


def _make_blank_pdf_bytes() -> bytes:
    document = fitz.open()
    document.new_page()
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


def _make_table_pdf_bytes() -> bytes:
    document = fitz.open()
    page = document.new_page()
    xs = [72, 220, 380]
    ys = [100, 130, 160]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), width=1)
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), width=1)
    for row, y in enumerate((120, 150)):
        page.insert_text((80, y), ["Method", "BGE-M3"][row], fontsize=10)
        page.insert_text((230, y), ["Score", "0.92"][row], fontsize=10)
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


def _make_vector_only_pdf_bytes() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.draw_rect(fitz.Rect(72, 72, 200, 200))
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


def _make_scanned_pdf_bytes() -> bytes:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 32, 32), 0)
    pixmap.clear_with(255)
    image = pixmap.tobytes("png")

    document = fitz.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=image)
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


def _make_encrypted_pdf_bytes() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Encrypted paper")
    buffer = io.BytesIO()
    document.save(
        buffer,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-password",
        user_pw="user-password",
    )
    document.close()
    return buffer.getvalue()


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
    assert paper["parse_status"] == "parsed"
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


def test_upload_persists_title_extracted_from_ieee_first_page(isolated_environment):
    response = _upload(
        isolated_environment["client"],
        _make_ieee_title_pdf_bytes(),
        "ieee-survey.pdf",
    )

    assert response.status_code == 201, response.text
    assert (
        response.json()["data"]["title"]
        == "Computing in Integrated Terrestrial and Non-Terrestrial Networks: "
        "A Comprehensive Survey"
    )


def test_upload_persists_pages_across_page_batch_flush(isolated_environment):
    client = isolated_environment["client"]
    pages = [f"Page {number}\nNetwork research content." for number in range(1, 106)]
    response = _upload(client, _make_pdf_bytes(pages=pages), "long-network-paper.pdf")

    assert response.status_code == 201, response.text
    paper = response.json()["data"]
    assert paper["page_count"] == 105
    assert len(paper["pages"]) == 105
    assert paper["pages"][0]["page_number"] == 1
    assert paper["pages"][-1]["page_number"] == 105
    assert "Page 105" in paper["pages"][-1]["text"]


def test_upload_persists_table_structure_and_search_chunk(isolated_environment):
    response = _upload(
        isolated_environment["client"],
        _make_table_pdf_bytes(),
        "table-paper.pdf",
    )

    assert response.status_code == 201, response.text
    paper = response.json()["data"]
    table = next(
        element
        for element in paper["pages"][0]["elements"]
        if element["element_type"] == "table"
    )
    assert table["data"]["rows"] == [["Method", "Score"], ["BGE-M3", "0.92"]]

    with isolated_environment["session_factory"]() as db:
        chunks = (
            db.query(PaperChunk)
            .filter_by(paper_id=paper["id"], content_type="table")
            .all()
        )
        assert len(chunks) == 1
        assert chunks[0].element_id == table["id"]
        assert "BGE-M3 | 0.92" in chunks[0].text


def test_upload_preserves_extractable_chinese_text(client: TestClient):
    pdf_bytes, expected_text = _make_chinese_pdf_bytes()
    response = _upload(client, pdf_bytes, "chinese-networks.pdf")

    assert response.status_code == 201, response.text
    paper = response.json()["data"]
    extracted_text = paper["pages"][0]["text"]
    assert paper["parse_status"] == "parsed"
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


def _mock_failed_index_for_empty_text(monkeypatch) -> None:
    import app.api.papers as papers_api

    def fail_index(db, paper_id, force=False):
        paper = db.query(Paper).filter_by(id=paper_id).one()
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() == 0
        paper.index_status = "failed"
        db.commit()
        return {
            "paper_id": paper_id,
            "indexed_count": 0,
            "status": "failed",
            "message": "no extractable text",
        }

    monkeypatch.setattr(papers_api, "index_paper", fail_index)


def test_blank_pdf_is_saved_with_empty_parse_status(isolated_environment, monkeypatch):
    _mock_failed_index_for_empty_text(monkeypatch)
    response = _upload(isolated_environment["client"], _make_blank_pdf_bytes(), "blank.pdf")

    assert response.status_code == 201
    paper = response.json()["data"]
    assert paper["parse_status"] == "empty"
    assert paper["index_status"] == "failed"
    assert paper["char_count"] == 0
    assert paper["pages"][0]["text"] == ""


def test_vector_content_without_text_is_not_classified_as_blank(
    isolated_environment,
    monkeypatch,
):
    _mock_failed_index_for_empty_text(monkeypatch)
    response = _upload(
        isolated_environment["client"],
        _make_vector_only_pdf_bytes(),
        "vector-only.pdf",
    )

    assert response.status_code == 201
    paper = response.json()["data"]
    assert paper["parse_status"] == "no_text"
    assert paper["index_status"] == "failed"
    assert paper["char_count"] == 0


def test_scanned_pdf_without_tesseract_is_marked_for_ocr(
    isolated_environment,
    monkeypatch,
):
    _mock_failed_index_for_empty_text(monkeypatch)
    import app.services.pdf_service as pdf_service

    monkeypatch.setattr(pdf_service.shutil, "which", lambda _name: None)
    response = _upload(
        isolated_environment["client"],
        _make_scanned_pdf_bytes(),
        "scanned.pdf",
    )

    assert response.status_code == 201
    paper = response.json()["data"]
    assert paper["parse_status"] == "needs_ocr"
    assert paper["index_status"] == "failed"
    assert paper["char_count"] == 0
    assert paper["pages"][0]["text"] == ""
    assert paper["pages"][0]["parse_status"] == "ocr_unavailable"
    assert paper["pages"][0]["ocr_executed"] is False
    assert "Tesseract" in paper["pages"][0]["ocr_error"]


def test_scanned_pdf_uses_mocked_ocr_when_available(
    isolated_environment,
    monkeypatch,
):
    import app.api.papers as papers_api
    import app.services.pdf_service as pdf_service

    monkeypatch.setattr(papers_api, "index_paper", lambda db, paper_id, force=False: {
        "paper_id": paper_id,
        "status": "indexed",
        "indexed_count": 1,
    })
    monkeypatch.setattr(pdf_service.shutil, "which", lambda _name: "tesseract.exe")
    monkeypatch.setattr(pdf_service, "_run_page_ocr", lambda _page, _settings: "OCR 中文结果")

    response = _upload(
        isolated_environment["client"],
        _make_scanned_pdf_bytes(),
        "scanned-ocr.pdf",
    )
    assert response.status_code == 201
    page = response.json()["data"]["pages"][0]
    assert page["text"] == "OCR 中文结果"
    assert page["parse_status"] == "ocr_extracted"
    assert page["ocr_executed"] is True
    assert page["ocr_error"] is None


def test_scanned_pdf_records_ocr_failure_reason(
    isolated_environment,
    monkeypatch,
):
    _mock_failed_index_for_empty_text(monkeypatch)
    import app.services.pdf_service as pdf_service

    monkeypatch.setattr(pdf_service.shutil, "which", lambda _name: "tesseract.exe")
    monkeypatch.setattr(
        pdf_service,
        "_run_page_ocr",
        lambda _page, _settings: (_ for _ in ()).throw(RuntimeError("simulated OCR failure")),
    )
    response = _upload(
        isolated_environment["client"],
        _make_scanned_pdf_bytes(),
        "ocr-failure.pdf",
    )

    assert response.status_code == 201
    paper = response.json()["data"]
    assert paper["parse_status"] == "ocr_failed"
    assert paper["pages"][0]["ocr_executed"] is True
    assert paper["pages"][0]["parse_status"] == "ocr_failed"
    assert "simulated OCR failure" in paper["pages"][0]["ocr_error"]


def test_encrypted_pdf_is_rejected_with_specific_error(client: TestClient):
    response = _upload(client, _make_encrypted_pdf_bytes(), "encrypted.pdf")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PDF_ENCRYPTED"
    assert "加密" in response.json()["error"]["message"]


def test_exact_duplicate_upload_is_rejected(client: TestClient):
    content = _make_pdf_bytes()
    assert _upload(client, content).status_code == 201
    duplicate = _upload(client, content, "same-content.pdf")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_PAPER"


def test_semantically_similar_upload_is_kept_for_manual_review(isolated_environment):
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

    assert second.status_code == 201
    assert second.json()["data"]["id"] != first.json()["data"]["id"]
    assert len(list(isolated_environment["upload_dir"].glob("*.pdf"))) == 2


def test_paper_version_registration_diff_retention_and_delete(isolated_environment):
    client = isolated_environment["client"]
    first = _upload(client, _make_pdf_bytes("Network Paper Version One")).json()["data"]
    second = _upload(
        client,
        _make_pdf_bytes(
            "Network Paper Version Two",
            pages=["Network Paper Version Two\nChanged congestion-control method."],
        ),
        "version-two.pdf",
    ).json()["data"]

    registered = client.post(
        f"/api/papers/{second['id']}/versions",
        json={"based_on_id": first["id"], "source": "manual"},
    )
    assert registered.status_code == 200, registered.text
    assert len(registered.json()["data"]) == 2

    diff = client.get(f"/api/papers/{first['id']}/versions/{second['id']}/diff")
    assert diff.status_code == 200
    assert "Changed congestion-control method" in diff.json()["data"]["diff"]

    retained = client.post(f"/api/papers/{first['id']}/versions/{second['id']}/retain")
    assert retained.status_code == 200
    assert next(item for item in retained.json()["data"] if item["paper_id"] == second["id"])["retained"]
    protected = client.delete(f"/api/papers/{second['id']}")
    assert protected.status_code == 409
    assert protected.json()["error"]["code"] == "PAPER_VERSION_RETAINED"

    removed = client.delete(f"/api/papers/{first['id']}/versions/{first['id']}")
    assert removed.status_code == 200
    with isolated_environment["session_factory"]() as db:
        assert db.query(Paper).filter_by(id=second["id"]).count() == 1


def test_batch_import_job_tracks_partial_failure_and_retry(
    isolated_environment,
    monkeypatch,
):
    import app.services.ingestion_service as ingestion_service

    def fake_index(db, paper_id, progress_callback=None):
        paper = db.query(Paper).filter_by(id=paper_id).one()
        paper.index_status = "indexed"
        db.commit()
        if progress_callback:
            progress_callback(1, 1)
        return {"paper_id": paper_id, "status": "indexed", "indexed_count": 1}

    monkeypatch.setattr(ingestion_service, "index_paper", fake_index)
    client = isolated_environment["client"]
    response = client.post(
        "/api/papers/import-jobs",
        files=[
            ("files", ("ok.pdf", _make_pdf_bytes("Batch Paper"), "application/pdf")),
            ("files", ("broken.pdf", b"%PDF-1.7\nbroken content", "application/pdf")),
        ],
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["id"]

    for _ in range(100):
        job_response = client.get(f"/api/papers/import-jobs/{job_id}")
        job = job_response.json()["data"]
        if job["status"] in {"partial", "failed", "completed"}:
            break
        time.sleep(0.05)

    assert job["status"] == "partial"
    assert job["completed_count"] == 1
    assert job["failed_count"] == 1
    assert {item["status"] for item in job["items"]} == {"succeeded", "failed"}
    assert all(item["parse_status"] != "pending" for item in job["items"])

    retried = client.post(f"/api/papers/import-jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["data"]["status"] in {"queued", "running", "partial", "failed"}

    for _ in range(100):
        job = client.get(f"/api/papers/import-jobs/{job_id}").json()["data"]
        if job["status"] in {"partial", "failed", "completed"}:
            break
        time.sleep(0.05)
    assert job["failed_count"] == 1
    failed_item = next(item for item in job["items"] if item["status"] == "failed")
    assert failed_item["attempts"] == 2
    assert failed_item["error_message"]
    listed_job = next(
        item
        for item in client.get("/api/papers/import-jobs").json()["data"]
        if item["id"] == job_id
    )
    detailed_job = client.get(f"/api/papers/import-jobs/{job_id}").json()["data"]
    assert listed_job["status"] == detailed_job["status"]
    assert listed_job["progress"] == detailed_job["progress"]
    assert listed_job["items"] == detailed_job["items"]


def test_batch_import_keeps_valid_files_when_other_uploads_fail(
    isolated_environment,
    monkeypatch,
):
    import app.services.ingestion_service as ingestion_service

    def fake_index(db, paper_id, progress_callback=None):
        paper = db.query(Paper).filter_by(id=paper_id).one()
        paper.index_status = "indexed"
        db.commit()
        if progress_callback:
            progress_callback(1, 1)
        return {"paper_id": paper_id, "status": "indexed", "indexed_count": 1}

    monkeypatch.setattr(ingestion_service, "index_paper", fake_index)
    client = isolated_environment["client"]
    response = client.post(
        "/api/papers/import-jobs",
        files=[
            ("files", ("valid.pdf", _make_pdf_bytes("Valid Batch Paper"), "text/plain")),
            ("files", ("..\\notes.txt", b"not a PDF", "application/pdf")),
            ("files", ("oversize.pdf", b"%PDF" + b"x" * (1024 * 1024), "application/pdf")),
        ],
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["id"]

    for _ in range(100):
        job = client.get(f"/api/papers/import-jobs/{job_id}").json()["data"]
        if job["status"] in {"partial", "failed", "completed"}:
            break
        time.sleep(0.05)

    assert job["total_count"] == 3
    assert job["completed_count"] == 1
    assert job["failed_count"] == 2
    assert job["queued_count"] == 0
    assert job["running_count"] == 0
    valid_item = next(item for item in job["items"] if item["original_filename"] == "valid.pdf")
    invalid_type = next(
        item for item in job["items"] if item["original_filename"] == "notes.txt"
    )
    oversize = next(item for item in job["items"] if item["original_filename"] == "oversize.pdf")
    assert valid_item["status"] == "succeeded"
    assert invalid_type["status"] == "failed"
    assert "仅支持 PDF" in invalid_type["error_message"]
    assert invalid_type["can_retry"] is False
    assert oversize["status"] == "failed"
    assert "大小超过" in oversize["error_message"]
    assert oversize["can_retry"] is False
    assert not list((isolated_environment["upload_dir"] / ".imports").glob("*.pdf"))


def test_batch_duplicate_isolated_to_its_item_and_retry_is_per_item(
    isolated_environment,
    monkeypatch,
):
    import app.services.ingestion_service as ingestion_service

    def fake_index(db, paper_id, progress_callback=None):
        paper = db.query(Paper).filter_by(id=paper_id).one()
        paper.index_status = "indexed"
        db.commit()
        if progress_callback:
            progress_callback(1, 1)
        return {"paper_id": paper_id, "status": "indexed", "indexed_count": 1}

    monkeypatch.setattr(ingestion_service, "index_paper", fake_index)
    client = isolated_environment["client"]
    pdf = _make_pdf_bytes("Duplicate Batch Paper")
    response = client.post(
        "/api/papers/import-jobs",
        files=[
            ("files", ("first.pdf", pdf, "application/pdf")),
            ("files", ("duplicate.pdf", pdf, "application/pdf")),
        ],
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["id"]

    for _ in range(100):
        job = client.get(f"/api/papers/import-jobs/{job_id}").json()["data"]
        if job["status"] in {"partial", "failed", "completed"}:
            break
        time.sleep(0.05)

    assert job["status"] == "partial"
    assert job["total_count"] == 2
    assert job["completed_count"] == 1
    assert job["failed_count"] == 1
    succeeded = next(item for item in job["items"] if item["status"] == "succeeded")
    failed = next(item for item in job["items"] if item["status"] == "failed")
    assert failed["can_retry"] is True
    assert "完全相同的重复文件" in failed["error_message"]

    retry = client.post(
        f"/api/papers/import-jobs/{job_id}/items/{failed['id']}/retry"
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["data"]["total_count"] == 2
    assert retry.json()["data"]["completed_count"] == 1

    for _ in range(100):
        job = client.get(f"/api/papers/import-jobs/{job_id}").json()["data"]
        if job["status"] in {"partial", "failed", "completed"}:
            break
        time.sleep(0.05)

    assert job["status"] == "partial"
    assert job["completed_count"] == 1
    assert job["failed_count"] == 1
    assert next(item for item in job["items"] if item["id"] == succeeded["id"])["attempts"] == 1
    retried_item = next(item for item in job["items"] if item["id"] == failed["id"])
    assert retried_item["attempts"] == 2
    assert retried_item["can_retry"] is True


def test_import_job_restart_recovery_requeues_existing_stage_file(
    isolated_environment,
):
    from app.models.paper import PaperImportItem, PaperImportJob
    from app.services.ingestion_service import recover_interrupted_jobs

    staged_path = isolated_environment["upload_dir"] / ".imports" / "recovery.pdf"
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(_make_pdf_bytes("Recovery Paper"))
    with isolated_environment["session_factory"]() as db:
        job = PaperImportJob(status="running", total_count=1)
        db.add(job)
        db.flush()
        item = PaperImportItem(
            job_id=job.id,
            original_filename="recovery.pdf",
            staged_path=str(staged_path),
            file_size=staged_path.stat().st_size,
            status="running",
            current_stage="indexing",
        )
        db.add(item)
        db.commit()
        item_id = item.id

    recover_interrupted_jobs(isolated_environment["session_factory"])

    with isolated_environment["session_factory"]() as db:
        item = db.query(PaperImportItem).filter_by(id=item_id).one()
        job = db.query(PaperImportJob).filter_by(id=item.job_id).one()
        assert item.status == "queued"
        assert item.progress == 0
        assert job.status == "queued"


def test_index_rebuild_is_a_persistent_job(isolated_environment, monkeypatch):
    import app.api.papers as papers_api
    import app.services.ingestion_service as ingestion_service

    client = isolated_environment["client"]
    paper = _upload(client, _make_pdf_bytes("Index Rebuild Paper")).json()["data"]

    def fake_index(db, paper_id, force=False, progress_callback=None):
        record = db.query(Paper).filter_by(id=paper_id).one()
        record.index_status = "indexed"
        db.commit()
        if progress_callback:
            progress_callback(1, 1)
        return {"paper_id": paper_id, "status": "indexed", "indexed_count": 1}

    monkeypatch.setattr(ingestion_service, "index_paper", fake_index)
    monkeypatch.setattr(
        ingestion_service,
        "audit_paper_index_consistency",
        lambda **_kwargs: {"consistent": True},
    )
    monkeypatch.setattr(
        ingestion_service,
        "reconcile_index_consistency",
        lambda _db, paper_ids=None, **_kwargs: {"consistent": True},
    )
    monkeypatch.setattr(
        papers_api,
        "audit_index_consistency",
        lambda _db: {
            "affected_paper_ids": [paper["id"]],
            "unverifiable_vector_count": 0,
            "read_error_count": 0,
            "orphan_vector_count": 0,
        },
    )
    response = client.post("/api/papers/index/rebuild")
    assert response.status_code == 202
    job_id = response.json()["data"]["id"]

    for _ in range(100):
        job = client.get(f"/api/papers/import-jobs/{job_id}").json()["data"]
        if job["status"] in {"completed", "partial", "failed"}:
            break
        time.sleep(0.05)

    assert job["kind"] == "index_rebuild"
    assert job["status"] == "completed"
    assert job["completed_count"] == 1
    assert job["items"][0]["paper_id"] == paper["id"]


def test_index_rebuild_endpoint_queues_only_affected_papers(
    isolated_environment,
    monkeypatch,
):
    import app.api.papers as papers_api
    import app.services.ingestion_service as ingestion_service

    client = isolated_environment["client"]
    affected = _upload(
        client,
        _make_pdf_bytes("Affected Index Paper"),
        "affected-index.pdf",
    ).json()["data"]
    untouched = _upload(
        client,
        _make_pdf_bytes("Untouched Index Paper"),
        "untouched-index.pdf",
    ).json()["data"]
    indexed_papers = []

    def fake_index(db, paper_id, force=False, progress_callback=None):
        indexed_papers.append((paper_id, force))
        record = db.query(Paper).filter_by(id=paper_id).one()
        record.index_status = "indexed"
        db.commit()
        if progress_callback:
            progress_callback(1, 1)
        return {"paper_id": paper_id, "status": "indexed", "indexed_count": 1}

    monkeypatch.setattr(ingestion_service, "index_paper", fake_index)
    monkeypatch.setattr(
        ingestion_service,
        "audit_paper_index_consistency",
        lambda **_kwargs: {"consistent": True},
    )
    monkeypatch.setattr(
        ingestion_service,
        "reconcile_index_consistency",
        lambda _db, paper_ids=None, **_kwargs: {"consistent": True},
    )
    monkeypatch.setattr(
        papers_api,
        "audit_index_consistency",
        lambda _db: {
            "affected_paper_ids": [affected["id"]],
            "unverifiable_vector_count": 0,
            "read_error_count": 0,
            "orphan_vector_count": 0,
        },
    )

    response = client.post("/api/papers/index/rebuild")
    assert response.status_code == 202
    job_id = response.json()["data"]["id"]
    for _ in range(100):
        job = client.get(f"/api/papers/import-jobs/{job_id}").json()["data"]
        if job["status"] in {"completed", "partial", "failed"}:
            break
        time.sleep(0.05)

    assert job["status"] == "completed"
    assert [item["paper_id"] for item in job["items"]] == [affected["id"]]
    assert untouched["id"] not in {paper_id for paper_id, _force in indexed_papers}
    assert indexed_papers == [(affected["id"], False)]


def test_upload_database_commit_failure_removes_file_and_records(
    isolated_environment,
    monkeypatch,
):
    import app.api.papers as papers_api

    original_create_paper = papers_api.create_or_resume_paper

    def create_with_commit_failure(
        db,
        filename,
        content,
        content_type,
        **kwargs,
    ):
        def fail_commit(session):
            raise RuntimeError("simulated upload database commit failure")

        event.listen(db, "before_commit", fail_commit, once=True)
        return original_create_paper(
            db,
            filename,
            content,
            content_type,
            **kwargs,
        )

    monkeypatch.setattr(
        papers_api,
        "create_or_resume_paper",
        create_with_commit_failure,
    )
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
