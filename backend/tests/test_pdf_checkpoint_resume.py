from __future__ import annotations

import hashlib
import io

import fitz
import pytest
from sqlalchemy import event

from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage
from app.services import pdf_service
from app.services.paper_service import (
    PDF_CHECKPOINT_SCHEMA_VERSION,
    create_or_resume_paper,
)


def _checkpoint_pdf(page_count: int, marker: str = "page") -> bytes:
    document = fitz.open()
    for page_number in range(1, page_count + 1):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"{marker} {page_number}: reliable network systems research.",
            fontsize=11,
        )
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _create(factory, content: bytes, **kwargs):
    with factory() as db:
        paper = create_or_resume_paper(
            db=db,
            filename="checkpoint.pdf",
            content=content,
            content_type="application/pdf",
            **kwargs,
        )
        return paper.id


def _checkpoint_page_numbers(factory, paper_id: int, *, completed_only=False):
    with factory() as db:
        query = db.query(PaperPage).filter(PaperPage.paper_id == paper_id)
        if completed_only:
            query = query.filter(PaperPage.checkpoint_status == "completed")
        return [
            page.page_number
            for page in query.order_by(PaperPage.page_number.asc()).all()
        ]


def test_hundred_page_pdf_persists_all_completed_checkpoints(
    isolated_environment,
):
    parsed_pages = []
    paper_id = _create(
        isolated_environment["session_factory"],
        _checkpoint_pdf(100),
        page_observer=parsed_pages.append,
    )

    assert parsed_pages == list(range(1, 101))
    assert _checkpoint_page_numbers(
        isolated_environment["session_factory"],
        paper_id,
        completed_only=True,
    ) == list(range(1, 101))
    with isolated_environment["session_factory"]() as db:
        paper = db.query(Paper).filter_by(id=paper_id).one()
        assert paper.parse_checkpoint_version == PDF_CHECKPOINT_SCHEMA_VERSION
        assert paper.parse_source_page_count == 100
        assert paper.page_count == 100
        assert paper.parse_status == "parsed"
        assert db.query(PaperElement).filter_by(paper_id=paper_id).count() == 100
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() >= 100


def test_page_31_failure_keeps_first_30_and_resumes_from_31(
    isolated_environment,
    monkeypatch,
):
    content = _checkpoint_pdf(40)
    original_extract_elements = pdf_service._extract_page_elements
    failed = {"value": False}

    def fail_page_31(page, page_text, section_path, in_references):
        if page.number == 30 and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("simulated page 31 parse failure")
        return original_extract_elements(
            page,
            page_text,
            section_path,
            in_references,
        )

    monkeypatch.setattr(pdf_service, "_extract_page_elements", fail_page_31)
    first_attempt_pages = []
    with isolated_environment["session_factory"]() as db:
        with pytest.raises(AppError) as error:
            create_or_resume_paper(
                db=db,
                filename="checkpoint.pdf",
                content=content,
                content_type="application/pdf",
                page_observer=first_attempt_pages.append,
            )
        assert "page 31 parse failure" in str(error.value.details)
        paper_id = db.query(Paper).filter_by(file_hash=hashlib.sha256(content).hexdigest()).one().id

    assert first_attempt_pages == list(range(1, 32))
    assert _checkpoint_page_numbers(
        isolated_environment["session_factory"],
        paper_id,
        completed_only=True,
    ) == list(range(1, 31))
    with isolated_environment["session_factory"]() as db:
        failed_page = (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, page_number=31)
            .one()
        )
        assert failed_page.checkpoint_status == "failed"
        assert "page 31 parse failure" in failed_page.checkpoint_error
        completed_element_ids = {
            element.id
            for element in db.query(PaperElement)
            .filter(
                PaperElement.paper_id == paper_id,
                PaperElement.page_number <= 30,
            )
            .all()
        }
        completed_chunk_ids = {
            chunk.id
            for chunk in db.query(PaperChunk)
            .filter(
                PaperChunk.paper_id == paper_id,
                PaperChunk.page_number <= 30,
            )
            .all()
        }
        completed_element_count = len(completed_element_ids)
        completed_chunk_count = len(completed_chunk_ids)

    retried_pages = []
    _create(
        isolated_environment["session_factory"],
        content,
        page_observer=retried_pages.append,
    )

    assert retried_pages == list(range(31, 41))
    with isolated_environment["session_factory"]() as db:
        current_completed_element_ids = {
            element.id
            for element in db.query(PaperElement)
            .filter(
                PaperElement.paper_id == paper_id,
                PaperElement.page_number <= 30,
            )
            .all()
        }
        current_completed_chunk_ids = {
            chunk.id
            for chunk in db.query(PaperChunk)
            .filter(
                PaperChunk.paper_id == paper_id,
                PaperChunk.page_number <= 30,
            )
            .all()
        }
        assert current_completed_element_ids == completed_element_ids
        assert current_completed_chunk_ids == completed_chunk_ids
        assert len(current_completed_element_ids) == completed_element_count
        assert len(current_completed_chunk_ids) == completed_chunk_count
        assert (
            db.query(PaperElement)
            .filter_by(paper_id=paper_id)
            .count()
            == 40
        )
        assert db.query(PaperPage).filter_by(paper_id=paper_id).count() == 40
        assert (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, checkpoint_status="completed")
            .count()
            == 40
        )


def test_mid_page_database_failure_rolls_back_partial_elements(
    isolated_environment,
):
    content = _checkpoint_pdf(35)
    with isolated_environment["session_factory"]() as db:
        def fail_page_31_elements(session, _flush_context, _instances):
            if any(
                isinstance(item, PaperElement) and item.page_number == 31
                for item in session.new
            ):
                raise RuntimeError("simulated element persistence failure")

        event.listen(db, "before_flush", fail_page_31_elements)
        with pytest.raises(Exception, match="element persistence failure"):
            create_or_resume_paper(
                db=db,
                filename="checkpoint.pdf",
                content=content,
                content_type="application/pdf",
            )
        event.remove(db, "before_flush", fail_page_31_elements)
        paper_id = db.query(Paper).filter_by(file_hash=hashlib.sha256(content).hexdigest()).one().id

        failed_page = (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, page_number=31)
            .one()
        )
        assert failed_page.checkpoint_status == "failed"
        assert (
            db.query(PaperElement)
            .filter_by(paper_id=paper_id, page_number=31)
            .count()
            == 0
        )
        assert (
            db.query(PaperChunk)
            .filter_by(paper_id=paper_id, page_number=31)
            .count()
            == 0
        )


def test_progress_update_failure_does_not_lose_completed_page(
    isolated_environment,
):
    progress_calls = []

    def fail_progress(completed, total):
        progress_calls.append((completed, total))
        raise RuntimeError("simulated task progress persistence failure")

    paper_id = _create(
        isolated_environment["session_factory"],
        _checkpoint_pdf(8),
        progress_callback=fail_progress,
    )

    assert progress_calls
    assert _checkpoint_page_numbers(
        isolated_environment["session_factory"],
        paper_id,
        completed_only=True,
    ) == list(range(1, 9))


def test_newer_worker_generation_cannot_be_overwritten_by_old_worker(
    isolated_environment,
    monkeypatch,
):
    content = _checkpoint_pdf(35)
    original_extract_elements = pdf_service._extract_page_elements
    failed = {"value": False}

    def fail_once(page, page_text, section_path, in_references):
        if page.number == 30 and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("simulated generation failure")
        return original_extract_elements(
            page,
            page_text,
            section_path,
            in_references,
        )

    monkeypatch.setattr(pdf_service, "_extract_page_elements", fail_once)
    with isolated_environment["session_factory"]() as db:
        with pytest.raises(AppError) as error:
            create_or_resume_paper(
                db=db,
                filename="checkpoint.pdf",
                content=content,
                content_type="application/pdf",
                worker_owner="worker-2",
                worker_generation=2,
            )
        assert "generation failure" in str(error.value.details)
        paper_id = db.query(Paper).filter_by(file_hash=hashlib.sha256(content).hexdigest()).one().id

    with isolated_environment["session_factory"]() as db:
        page_before = (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, page_number=31)
            .one()
        )
        status_before = page_before.checkpoint_status
        generation_before = page_before.checkpoint_generation

    with isolated_environment["session_factory"]() as db:
        with pytest.raises(RuntimeError, match="current worker is 1"):
            create_or_resume_paper(
                db=db,
                filename="checkpoint.pdf",
                content=content,
                content_type="application/pdf",
                worker_owner="worker-1",
                worker_generation=1,
            )

    with isolated_environment["session_factory"]() as db:
        page_after = (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, page_number=31)
            .one()
        )
        assert page_after.checkpoint_generation == generation_before
        assert page_after.checkpoint_status == status_before


def test_source_hash_change_invalidates_old_checkpoint(
    isolated_environment,
    monkeypatch,
):
    first_content = _checkpoint_pdf(20, "old")
    original_extract_elements = pdf_service._extract_page_elements
    failed = {"value": False}

    def fail_once(page, page_text, section_path, in_references):
        if page.number == 5 and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("simulated source change failure")
        return original_extract_elements(
            page,
            page_text,
            section_path,
            in_references,
        )

    monkeypatch.setattr(pdf_service, "_extract_page_elements", fail_once)
    with isolated_environment["session_factory"]() as db:
        with pytest.raises(AppError) as error:
            create_or_resume_paper(
                db=db,
                filename="same-name.pdf",
                content=first_content,
                content_type="application/pdf",
            )
        assert "source change failure" in str(error.value.details)
        old_paper_id = db.query(Paper).one().id

    monkeypatch.setattr(pdf_service, "_extract_page_elements", original_extract_elements)
    parsed_pages = []
    new_paper_id = _create(
        isolated_environment["session_factory"],
        _checkpoint_pdf(20, "new"),
        page_observer=parsed_pages.append,
    )

    assert new_paper_id != old_paper_id
    assert parsed_pages == list(range(1, 21))
    with isolated_environment["session_factory"]() as db:
        old_paper = db.query(Paper).filter_by(id=old_paper_id).one()
        new_paper = db.query(Paper).filter_by(id=new_paper_id).one()
        assert old_paper.file_hash != new_paper.file_hash


def test_checkpoint_schema_version_change_resets_only_incomplete_paper(
    isolated_environment,
):
    content = _checkpoint_pdf(12)
    paper_id = _create(
        isolated_environment["session_factory"],
        content,
    )
    with isolated_environment["session_factory"]() as db:
        paper = db.query(Paper).filter_by(id=paper_id).one()
        paper.parse_checkpoint_version = 0
        paper.parse_status = "pending"
        db.commit()

    parsed_pages = []
    _create(
        isolated_environment["session_factory"],
        content,
        page_observer=parsed_pages.append,
    )

    assert parsed_pages == list(range(1, 13))
    with isolated_environment["session_factory"]() as db:
        assert db.query(PaperPage).filter_by(paper_id=paper_id).count() == 12
        assert (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, checkpoint_status="completed")
            .count()
            == 12
        )
        first_page_rows = (
            db.query(PaperElement)
            .filter_by(paper_id=paper_id, page_number=1)
            .count()
        )
        assert first_page_rows == 1


def test_repeated_resume_does_not_reparse_or_duplicate_records(
    isolated_environment,
):
    content = _checkpoint_pdf(15)
    paper_id = _create(
        isolated_environment["session_factory"],
        content,
    )
    with isolated_environment["session_factory"]() as db:
        before_pages = db.query(PaperPage).filter_by(paper_id=paper_id).count()
        before_elements = db.query(PaperElement).filter_by(paper_id=paper_id).count()
        before_chunks = db.query(PaperChunk).filter_by(paper_id=paper_id).count()

    parsed_pages = []
    _create(
        isolated_environment["session_factory"],
        content,
        page_observer=parsed_pages.append,
    )

    assert parsed_pages == []
    with isolated_environment["session_factory"]() as db:
        assert db.query(PaperPage).filter_by(paper_id=paper_id).count() == before_pages
        assert db.query(PaperElement).filter_by(paper_id=paper_id).count() == before_elements
        assert db.query(PaperChunk).filter_by(paper_id=paper_id).count() == before_chunks


def test_resume_reuses_section_context_from_completed_pages(
    isolated_environment,
    monkeypatch,
):
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "1 Introduction", fontsize=14)
    second = document.new_page()
    second.insert_text(
        (72, 72),
        "Network reliability evidence continues in the introduction.",
        fontsize=11,
    )
    output = io.BytesIO()
    document.save(output)
    document.close()
    content = output.getvalue()
    original_extract_elements = pdf_service._extract_page_elements
    failed = {"value": False}

    def fail_page_two(page, page_text, section_path, in_references):
        if page.number == 1 and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("simulated context resume failure")
        return original_extract_elements(
            page,
            page_text,
            section_path,
            in_references,
        )

    monkeypatch.setattr(pdf_service, "_extract_page_elements", fail_page_two)
    with isolated_environment["session_factory"]() as db:
        with pytest.raises(AppError):
            create_or_resume_paper(
                db=db,
                filename="section-resume.pdf",
                content=content,
                content_type="application/pdf",
            )

    retried_pages = []
    with isolated_environment["session_factory"]() as db:
        paper = create_or_resume_paper(
            db=db,
            filename="section-resume.pdf",
            content=content,
            content_type="application/pdf",
            page_observer=retried_pages.append,
        )
        paper_id = paper.id
    assert retried_pages == [2]
    with isolated_environment["session_factory"]() as db:
        page_two_elements = (
            db.query(PaperElement)
            .filter_by(paper_id=paper_id, page_number=2)
            .order_by(PaperElement.element_index.asc())
            .all()
        )
        assert page_two_elements
        assert page_two_elements[0].section_path == "1 Introduction"


def test_resume_skips_completed_pages_after_a_failed_gap(
    isolated_environment,
):
    content = _checkpoint_pdf(4)
    paper_id = _create(
        isolated_environment["session_factory"],
        content,
    )
    with isolated_environment["session_factory"]() as db:
        failed_page = (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, page_number=2)
            .one()
        )
        failed_page.checkpoint_status = "failed"
        failed_page.checkpoint_error = "simulated noncontiguous failure"
        db.commit()

    parsed_pages = []
    _create(
        isolated_environment["session_factory"],
        content,
        page_observer=parsed_pages.append,
    )

    assert parsed_pages == [2]
    with isolated_environment["session_factory"]() as db:
        assert (
            db.query(PaperPage)
            .filter_by(paper_id=paper_id, checkpoint_status="completed")
            .count()
            == 4
        )
        assert (
            db.query(PaperElement)
            .filter_by(paper_id=paper_id)
            .count()
            == 4
        )
