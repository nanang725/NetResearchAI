from __future__ import annotations

import io
import time

import fitz
import pytest

from app.core.exceptions import AppError
from app.models.paper import (
    Paper,
    PaperChunk,
    PaperImportJob,
    PaperPage,
)
from app.services import embedding_service, paper_service, pdf_service
from app.services.paper_service import (
    create_or_resume_paper,
    get_paper_parse_completeness,
)


def _mixed_pdf() -> bytes:
    document = fitz.open()
    first = document.new_page()
    first.insert_text(
        (72, 72),
        "Page one contains extractable network routing evidence.",
        fontsize=11,
    )
    second = document.new_page()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 32, 32), 0)
    pixmap.clear_with(255)
    second.insert_image(second.rect, stream=pixmap.tobytes("png"))
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _one_page_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Complete network systems evidence.", fontsize=11)
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _create_paper(factory, content: bytes) -> int:
    with factory() as db:
        paper = create_or_resume_paper(
            db=db,
            filename="closure.pdf",
            content=content,
            content_type="application/pdf",
        )
        return paper.id


def _wait_for_job(client, job_id: str):
    for _ in range(120):
        job = client.get(f"/api/papers/import-jobs/{job_id}").json()["data"]
        if job["status"] in {"completed", "partial", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError("import job did not reach a terminal state")


class _FakeIndexCollection:
    def __init__(self):
        self.records = {}

    def get(self, ids=None, where=None, include=None, **kwargs):
        include = include or []
        selected = [
            vector_id
            for vector_id, record in self.records.items()
            if (
                (ids is not None and vector_id in ids)
                or (
                    ids is None
                    and (
                        where is None
                        or all(
                            record["metadata"].get(key) == value
                            for key, value in where.items()
                        )
                    )
                )
            )
        ]
        result = {"ids": selected}
        if "documents" in include:
            result["documents"] = [
                self.records[vector_id]["document"] for vector_id in selected
            ]
        if "metadatas" in include:
            result["metadatas"] = [
                dict(self.records[vector_id]["metadata"]) for vector_id in selected
            ]
        if "embeddings" in include:
            result["embeddings"] = [
                self.records[vector_id]["embedding"] for vector_id in selected
            ]
        return result

    def upsert(self, ids, embeddings, documents, metadatas):
        for vector_id, embedding, document, metadata in zip(
            ids,
            embeddings,
            documents,
            metadatas,
        ):
            self.records[vector_id] = {
                "embedding": list(embedding),
                "document": document,
                "metadata": dict(metadata),
            }

    def delete(self, ids=None, where=None):
        if ids is not None:
            for vector_id in ids:
                self.records.pop(vector_id, None)

    def count(self):
        return len(self.records)


@pytest.mark.parametrize("page_status", ["failed", "processing", "pending"])
def test_incomplete_checkpoint_blocks_index_paper(
    isolated_environment,
    monkeypatch,
    page_status,
):
    paper_id = _create_paper(
        isolated_environment["session_factory"],
        _one_page_pdf(),
    )
    with isolated_environment["session_factory"]() as db:
        page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
        page.checkpoint_status = page_status
        db.commit()
        assert get_paper_parse_completeness(db, paper_id).complete is False

        monkeypatch.setattr(
            embedding_service,
            "get_chroma_collection",
            lambda: (_ for _ in ()).throw(
                AssertionError("incomplete parse must stop before Chroma")
            ),
        )
        with pytest.raises(AppError) as error:
            embedding_service.index_paper(db, paper_id)
        assert error.value.code == "PDF_PARSE_INCOMPLETE"

    with isolated_environment["session_factory"]() as db:
        assert db.query(Paper).filter_by(id=paper_id).one().index_status == "failed"


def test_complete_checkpoint_allows_indexing(
    isolated_environment,
    monkeypatch,
):
    paper_id = _create_paper(
        isolated_environment["session_factory"],
        _one_page_pdf(),
    )

    class FakeCollection:
        def __init__(self):
            self.records = {}

        def get(self, **kwargs):
            ids = kwargs.get("ids")
            include = kwargs.get("include", [])
            if ids is None:
                return {"ids": []}
            return {
                "ids": [],
                **({"documents": []} if "documents" in include else {}),
                **({"metadatas": []} if "metadatas" in include else {}),
                **({"embeddings": []} if "embeddings" in include else {}),
            }

        def upsert(self, ids, embeddings, documents, metadatas):
            for values in zip(ids, embeddings, documents, metadatas):
                self.records[values[0]] = values

        def count(self):
            return len(self.records)

    collection = FakeCollection()
    monkeypatch.setattr(
        embedding_service,
        "get_chroma_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        embedding_service,
        "encode_texts",
        lambda texts: [[1.0, float(index)] for index, _ in enumerate(texts)],
    )
    with isolated_environment["session_factory"]() as db:
        result = embedding_service.index_paper(db, paper_id)
    assert result["status"] == "indexed"
    assert collection.count() > 0


def test_partial_chunks_do_not_make_import_task_succeed(
    isolated_environment,
    monkeypatch,
):
    import app.services.ingestion_service as ingestion_service

    monkeypatch.setattr(pdf_service.shutil, "which", lambda _name: None)
    indexed_papers = []
    monkeypatch.setattr(
        ingestion_service,
        "index_paper",
        lambda **kwargs: indexed_papers.append(kwargs["paper_id"]),
    )
    client = isolated_environment["client"]
    response = client.post(
        "/api/papers/import-jobs",
        files={
            "files": (
                "mixed.pdf",
                _mixed_pdf(),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 202
    job = _wait_for_job(client, response.json()["data"]["id"])

    assert job["status"] == "failed"
    item = job["items"][0]
    assert item["status"] == "failed"
    assert item["parse_status"] == "needs_ocr"
    assert "PDF 解析未完成" in item["error_message"]
    assert "未完成页码：2" in item["error_message"]
    assert indexed_papers == []
    with isolated_environment["session_factory"]() as db:
        paper = db.query(Paper).one()
        assert paper.index_status == "pending"
        assert db.query(PaperChunk).filter_by(paper_id=paper.id).count() > 0
        assert (
            db.query(PaperPage)
            .filter_by(paper_id=paper.id, checkpoint_status="failed")
            .count()
            == 1
        )


def test_retry_parses_only_failed_page_then_indexes(
    isolated_environment,
    monkeypatch,
):
    import app.services.ingestion_service as ingestion_service

    monkeypatch.setattr(pdf_service.shutil, "which", lambda _name: None)
    client = isolated_environment["client"]
    response = client.post(
        "/api/papers/import-jobs",
        files={
            "files": (
                "retry-mixed.pdf",
                _mixed_pdf(),
                "application/pdf",
            )
        },
    )
    job_id = response.json()["data"]["id"]
    failed_job = _wait_for_job(client, job_id)
    item_id = failed_job["items"][0]["id"]

    started_pages = []
    original_extract_pdf = paper_service.extract_pdf

    def record_resume(*args, **kwargs):
        started_pages.append(kwargs["start_page"])
        return original_extract_pdf(*args, **kwargs)

    monkeypatch.setattr(paper_service, "extract_pdf", record_resume)
    monkeypatch.setattr(
        pdf_service.shutil,
        "which",
        lambda _name: "tesseract.exe",
    )
    monkeypatch.setattr(
        pdf_service,
        "_run_page_ocr",
        lambda _page, _settings: "OCR page two network evidence.",
    )

    indexed_papers = []

    def fake_index(db, paper_id, force=False, progress_callback=None):
        indexed_papers.append(paper_id)
        paper = db.query(Paper).filter_by(id=paper_id).one()
        paper.index_status = "indexed"
        db.commit()
        return {"paper_id": paper_id, "status": "indexed", "indexed_count": 1}

    monkeypatch.setattr(ingestion_service, "index_paper", fake_index)
    retry = client.post(
        f"/api/papers/import-jobs/{job_id}/items/{item_id}/retry"
    )
    assert retry.status_code == 200
    completed_job = _wait_for_job(client, job_id)

    assert completed_job["status"] == "completed"
    assert completed_job["items"][0]["status"] == "succeeded"
    assert started_pages == [2]
    with isolated_environment["session_factory"]() as db:
        paper = db.query(Paper).one()
        assert get_paper_parse_completeness(db, paper.id).complete is True
        assert indexed_papers == [paper.id]


def test_index_audit_distinguishes_parse_incomplete_and_vector_missing(
    isolated_environment,
    monkeypatch,
):
    monkeypatch.setattr(pdf_service.shutil, "which", lambda _name: None)
    paper_id = _create_paper(
        isolated_environment["session_factory"],
        _mixed_pdf(),
    )

    class EmptyCollection:
        def get(self, ids=None, include=None, **kwargs):
            include = include or []
            return {
                "ids": [],
                **({"documents": []} if "documents" in include else {}),
                **({"metadatas": []} if "metadatas" in include else {}),
            }

        def count(self):
            return 0

    monkeypatch.setattr(
        embedding_service,
        "get_chroma_collection",
        lambda: EmptyCollection(),
    )
    with isolated_environment["session_factory"]() as db:
        audit = embedding_service.audit_index_consistency(db)

    assert audit["parse_incomplete_page_count"] == 1
    assert audit["parse_affected_paper_ids"] == [paper_id]
    assert audit["missing_vector_count"] > 0
    assert audit["consistent"] is False
    assert audit["parse_issue_details"][0]["failed_page_count"] == 1


def test_repair_endpoint_cannot_bypass_parse_incomplete(
    isolated_environment,
    monkeypatch,
):
    monkeypatch.setattr(pdf_service.shutil, "which", lambda _name: None)
    paper_id = _create_paper(
        isolated_environment["session_factory"],
        _mixed_pdf(),
    )
    collection = type(
        "EmptyCollection",
        (),
        {
            "get": lambda self, ids=None, include=None, **kwargs: {
                "ids": [],
                **({"documents": []} if include and "documents" in include else {}),
                **({"metadatas": []} if include and "metadatas" in include else {}),
            },
            "count": lambda self: 0,
        },
    )()
    monkeypatch.setattr(
        embedding_service,
        "get_chroma_collection",
        lambda: collection,
    )
    with isolated_environment["session_factory"]() as db:
        before_jobs = db.query(PaperImportJob).count()
    response = isolated_environment["client"].post("/api/papers/index/rebuild")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PDF_PARSE_INCOMPLETE"
    with isolated_environment["session_factory"]() as db:
        assert db.query(PaperImportJob).count() == before_jobs
        assert get_paper_parse_completeness(db, paper_id).complete is False


@pytest.mark.parametrize(
    ("legacy_parse_status", "expected_complete"),
    [
        ("parsed", True),
        ("no_text", True),
        ("empty", True),
        ("partial_ocr", False),
        ("needs_ocr", False),
        ("ocr_failed", False),
    ],
)
def test_legacy_parse_status_completeness_whitelist(
    isolated_environment,
    monkeypatch,
    legacy_parse_status,
    expected_complete,
):
    content = _one_page_pdf()
    paper_id = _create_paper(
        isolated_environment["session_factory"],
        content,
    )
    with isolated_environment["session_factory"]() as db:
        paper = db.query(Paper).filter_by(id=paper_id).one()
        paper.parse_checkpoint_version = 0
        paper.parse_status = legacy_parse_status
        page = db.query(PaperPage).filter_by(paper_id=paper_id).one()
        page.checkpoint_status = "pending"
        db.commit()

        completeness = get_paper_parse_completeness(db, paper_id)
        assert completeness.complete is expected_complete

    collection = _FakeIndexCollection()
    monkeypatch.setattr(
        embedding_service,
        "get_chroma_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        embedding_service,
        "encode_texts",
        lambda texts: [[1.0, float(index)] for index, _ in enumerate(texts)],
    )
    with isolated_environment["session_factory"]() as db:
        audit = embedding_service.audit_index_consistency(db)
        assert (
            audit["parse_incomplete_page_count"] == 0
            if expected_complete
            else audit["parse_incomplete_page_count"] > 0
        )
        if expected_complete:
            assert paper_id not in audit["parse_affected_paper_ids"]
            result = embedding_service.index_paper(db, paper_id)
            assert result["status"] == "indexed"
        else:
            assert paper_id in audit["parse_affected_paper_ids"]
            with pytest.raises(AppError) as error:
                embedding_service.index_paper(db, paper_id)
            assert error.value.code == "PDF_PARSE_INCOMPLETE"

    if not expected_complete:
        reparsed_pages = []
        with isolated_environment["session_factory"]() as db:
            create_or_resume_paper(
                db=db,
                filename="legacy-recovery.pdf",
                content=content,
                content_type="application/pdf",
                page_observer=reparsed_pages.append,
            )
            assert get_paper_parse_completeness(db, paper_id).complete is True
        assert reparsed_pages == [1]
