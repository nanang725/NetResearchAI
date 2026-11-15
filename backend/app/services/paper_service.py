
import difflib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage, PaperVersion
from app.services.paper_operation_coordinator import paper_operation_coordinator
from app.services.pdf_service import (
    derive_pdf_metadata,
    extract_pdf,
    inspect_pdf_source,
    validate_pdf_bytes,
)

logger = logging.getLogger(__name__)
DELETION_JOURNAL_DIR = ".pending_deletions"
PDF_CHECKPOINT_SCHEMA_VERSION = 1
LEGACY_COMPLETE_PARSE_STATUSES = {
    "parsed",
    "no_text",
    "empty",
}


@dataclass
class PendingPaperDeletion:
    paper_id: int
    file_path: Path
    journal_path: Path
    vector_pending: bool = True
    file_pending: bool = True


@dataclass(frozen=True)
class PaperParseCompleteness:
    paper_id: int
    expected_page_count: int
    recorded_page_count: int
    completed_page_count: int
    failed_page_count: int
    processing_page_count: int
    pending_page_count: int
    missing_page_count: int
    incomplete_page_numbers: tuple[int, ...]
    legacy_assumed_complete: bool = False

    @property
    def complete(self) -> bool:
        if self.legacy_assumed_complete:
            return True
        return (
            self.recorded_page_count == self.expected_page_count
            and self.completed_page_count == self.expected_page_count
            and self.failed_page_count == 0
            and self.processing_page_count == 0
            and self.pending_page_count == 0
            and self.missing_page_count == 0
        )

    @property
    def incomplete_page_count(self) -> int:
        if self.legacy_assumed_complete:
            return 0
        return (
            max(self.expected_page_count - self.completed_page_count, 0)
            + max(self.recorded_page_count - self.expected_page_count, 0)
        )

    def failure_message(self) -> str:
        page_numbers = ", ".join(
            str(number) for number in self.incomplete_page_numbers
        )
        suffix = f" 未完成页码：{page_numbers}。" if page_numbers else ""
        return (
            "PDF 解析未完成，不能建立完整向量索引。"
            f" 预期 {self.expected_page_count} 页，"
            f"已记录 {self.recorded_page_count} 页，"
            f"completed {self.completed_page_count} 页，"
            f"failed {self.failed_page_count} 页，"
            f"processing {self.processing_page_count} 页，"
            f"pending {self.pending_page_count} 页，"
            f"missing {self.missing_page_count} 页。"
            f"{suffix}"
        )


def _upload_dir() -> Path:
    return get_settings().upload_path


def ensure_storage() -> None:
    _upload_dir().mkdir(parents=True, exist_ok=True)


def _journal_path(paper_id: int, upload_dir: Path) -> Path:
    return upload_dir / DELETION_JOURNAL_DIR / f"paper_{paper_id}.json"


def _write_deletion_journal(deletion: PendingPaperDeletion) -> None:
    deletion.journal_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = deletion.journal_path.with_suffix(".tmp")
    payload = {
        "paper_id": deletion.paper_id,
        "file_path": str(deletion.file_path),
        "vector_pending": deletion.vector_pending,
        "file_pending": deletion.file_pending,
    }
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary_path, deletion.journal_path)
    except OSError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "Could not remove temporary deletion journal %s",
                temporary_path,
            )
        raise


def _load_deletion_journal(journal_path: Path) -> PendingPaperDeletion:
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    paper_id = payload["paper_id"]
    if not isinstance(paper_id, int) or paper_id <= 0:
        raise ValueError("Invalid paper_id in deletion journal")
    return PendingPaperDeletion(
        paper_id=paper_id,
        file_path=Path(payload["file_path"]),
        journal_path=journal_path,
        vector_pending=bool(payload["vector_pending"]),
        file_pending=bool(payload["file_pending"]),
    )


def _safe_upload_file_path(file_path: Path, upload_dir: Path) -> Path | None:
    resolved_upload_dir = upload_dir.resolve()
    resolved_file_path = file_path.resolve()
    try:
        resolved_file_path.relative_to(resolved_upload_dir)
    except ValueError:
        return None
    return resolved_file_path


def _persist_deletion_progress(deletion: PendingPaperDeletion) -> None:
    try:
        _write_deletion_journal(deletion)
    except OSError:
        logger.exception(
            "Deletion cleanup progress could not be saved for paper_id=%s; "
            "the existing journal will be retried",
            deletion.paper_id,
        )


def finish_paper_deletion(
    deletion: PendingPaperDeletion,
    delete_vectors: Callable[[int], None],
    upload_dir: Path | None = None,
) -> bool:
    """Best-effort cleanup; return True while durable cleanup remains pending."""
    upload_dir = upload_dir or _upload_dir()

    if deletion.vector_pending:
        try:
            delete_vectors(deletion.paper_id)
        except Exception:
            logger.exception(
                "Vector cleanup failed for paper_id=%s; recovery is queued in %s",
                deletion.paper_id,
                deletion.journal_path,
            )
        else:
            deletion.vector_pending = False
            _persist_deletion_progress(deletion)

    safe_path = _safe_upload_file_path(deletion.file_path, upload_dir)
    if safe_path is None:
        deletion.file_pending = True
        logger.error(
            "Refusing to delete paper file outside the upload directory: "
            "paper_id=%s path=%s",
            deletion.paper_id,
            deletion.file_path,
        )
        _persist_deletion_progress(deletion)
    elif deletion.file_pending:
        try:
            safe_path.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "PDF cleanup failed for paper_id=%s path=%s; recovery is queued",
                deletion.paper_id,
                safe_path,
            )
        else:
            deletion.file_pending = False
            _persist_deletion_progress(deletion)

    if deletion.vector_pending or deletion.file_pending:
        return True

    try:
        deletion.journal_path.unlink(missing_ok=True)
    except OSError:
        logger.exception(
            "Completed deletion journal could not be removed for paper_id=%s",
            deletion.paper_id,
        )
        return True
    return False


def retry_pending_deletions(
    session_factory: Callable[[], Session],
    delete_vectors: Callable[[int], None],
    upload_dir: Path | None = None,
) -> None:
    """Resume cleanup journals without touching records that survived a rollback."""
    upload_dir = upload_dir or _upload_dir()
    journal_dir = upload_dir / DELETION_JOURNAL_DIR
    if not journal_dir.exists():
        return

    try:
        journal_paths = sorted(journal_dir.glob("paper_*.json"))
    except OSError:
        logger.exception("Could not enumerate pending paper deletion journals in %s", journal_dir)
        return

    for journal_path in journal_paths:
        try:
            deletion = _load_deletion_journal(journal_path)
            with session_factory() as db:
                paper_exists = (
                    db.query(Paper.id)
                    .filter(Paper.id == deletion.paper_id)
                    .first()
                    is not None
                )
        except Exception:
            logger.exception(
                "Could not inspect pending deletion journal %s; it will be retried",
                journal_path,
            )
            continue

        if paper_exists:
            try:
                journal_path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Could not cancel stale deletion journal for existing paper_id=%s",
                    deletion.paper_id,
                )
            else:
                logger.warning(
                    "Cancelled pending deletion for paper_id=%s because its database "
                    "record still exists",
                    deletion.paper_id,
                )
            continue

        finish_paper_deletion(deletion, delete_vectors, upload_dir)


def create_paper(
    db: Session,
    filename: str,
    content: bytes,
    content_type: str,
    on_saved: Callable[[Paper], None] | None = None,
) -> Paper:
    settings = get_settings()
    validate_pdf_bytes(content, filename, settings.max_upload_bytes)

    file_hash = hashlib.sha256(content).hexdigest()
    existing_exact = (
        db.query(Paper)
        .filter(Paper.file_hash == file_hash)
        .first()
    )
    if existing_exact:
        raise AppError(
            f"检测到完全相同的重复文件！库中已存在：{existing_exact.original_filename}",
            code="DUPLICATE_PAPER",
            status_code=409,
        )

    stored_filename = f"{file_hash}.pdf"
    upload_dir = _upload_dir()
    file_path = upload_dir / stored_filename

    ensure_storage()
    created_file = False
    try:
        with file_path.open("xb") as stored_file:
            created_file = True
            stored_file.write(content)
    except FileExistsError:
        existing_file_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if existing_file_hash != file_hash:
            raise AppError(
                "上传文件存储路径已被不同内容占用。",
                code="FILE_STORAGE_CONFLICT",
                status_code=500,
            )
    except OSError as exc:
        if created_file:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove partially written PDF at %s", file_path)
        logger.exception("Could not store uploaded PDF at %s", file_path)
        raise AppError(
            "无法保存上传的 PDF 文件。",
            code="FILE_STORAGE_FAILED",
            status_code=500,
        ) from exc

    paper = Paper(
        title=Path(filename).stem[:500] or "未命名论文",
        file_hash=file_hash,
        original_filename=filename,
        stored_filename=stored_filename,
        file_path=str(file_path),
        file_size=len(content),
        content_type=content_type or "application/pdf",
        page_count=0,
        char_count=0,
        authors="未知作者",
        parse_status="pending",
        index_status="pending",
    )

    try:
        db.add(paper)
        db.flush()
        chunk_index = 0

        def persist_page(page: dict) -> None:
            nonlocal chunk_index
            page_number = page["page_number"]
            text = page["text"]
            page_chunk_index = 0
            page_row = PaperPage(
                paper_id=paper.id,
                page_number=page_number,
                text=text,
                char_count=page["char_count"],
                parse_status=page["parse_status"],
                ocr_executed=page["ocr_executed"],
                ocr_error=page["ocr_error"],
                checkpoint_status="completed",
                checkpoint_section_path=json.dumps(
                    page.get("checkpoint_section_path", []),
                    ensure_ascii=False,
                ),
                checkpoint_in_references=page.get(
                    "checkpoint_in_references",
                    False,
                ),
                checkpoint_updated_at=datetime.utcnow(),
            )
            db.add(page_row)
            element_rows: list[tuple[dict, PaperElement]] = []
            for element_index, element in enumerate(page.get("elements", [])):
                element_row = PaperElement(
                    paper_id=paper.id,
                    page=page_row,
                    page_number=page_number,
                    element_index=element_index,
                    element_type=element["element_type"],
                    text=element.get("text", ""),
                    data=(
                        json.dumps(element["data"], ensure_ascii=False)
                        if element.get("data") is not None
                        else None
                    ),
                    bbox=(
                        json.dumps(element["bbox"])
                        if element.get("bbox") is not None
                        else None
                    ),
                    section_path=element.get("section_path"),
                    extraction_status=element.get("extraction_status", "extracted"),
                    confidence=element.get("confidence"),
                )
                db.add(element_row)
                element_rows.append((element, element_row))
            db.flush()
            start = 0
            while start < len(text):
                end = min(start + 500, len(text))
                if end < len(text):
                    cut_point = max(
                        text.rfind("\n", start, end),
                        text.rfind("。", start, end),
                        text.rfind(". ", start, end),
                    )
                    if cut_point > start + 100:
                        end = cut_point + 1
                chunk_text = text[start:end].strip()
                if chunk_text:
                    db.add(
                        PaperChunk(
                            paper_id=paper.id,
                            page_number=page_number,
                            page_chunk_index=page_chunk_index,
                            chunk_index=chunk_index,
                            text=chunk_text,
                            content_type="paragraph",
                        )
                    )
                    page_chunk_index += 1
                    chunk_index += 1
                start = max(end, start + 1)

            for element, element_row in element_rows:
                if element["element_type"] != "table" or not element.get("text", "").strip():
                    continue
                db.add(
                    PaperChunk(
                        paper_id=paper.id,
                        page_number=page_number,
                        page_chunk_index=page_chunk_index,
                        chunk_index=chunk_index,
                        text=f"[TABLE]\n{element['text']}",
                        content_type="table",
                        element_id=element_row.id,
                    )
                )
                page_chunk_index += 1
                chunk_index += 1
            db.flush()

        extraction = extract_pdf(
            content,
            filename,
            page_callback=persist_page,
            collect_pages=False,
        )
        paper.title = extraction["title"][:500]
        paper.page_count = extraction["page_count"]
        paper.char_count = extraction["char_count"]
        paper.authors = extraction["authors"] or "未知作者"
        paper.parse_status = extraction["parse_status"]
        paper.parse_checkpoint_version = PDF_CHECKPOINT_SCHEMA_VERSION
        paper.parse_source_hash = file_hash
        paper.parse_source_size = len(content)
        paper.parse_source_page_count = extraction["page_count"]
        paper.parse_layout_title = extraction.get("layout_title")
        paper.pdf_metadata = json.dumps(extraction["pdf_metadata"], ensure_ascii=False)
        publication_metadata = extraction["publication_metadata"]
        paper.doi = publication_metadata["doi"]
        paper.normalized_title = publication_metadata["normalized_title"]
        paper.publication_year = publication_metadata["publication_year"]
        paper.venue = publication_metadata["venue"]
        paper.normalized_venue = publication_metadata["normalized_venue"]
        if on_saved is not None:
            on_saved(paper)
        with paper_operation_coordinator.collection_mutation():
            db.commit()
    except AppError:
        db.rollback()
        if created_file:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove PDF after parsing failure: %s", file_path)
        logger.exception("PDF parsing failed for filename=%s", filename)
        raise
    except Exception as exc:
        db.rollback()
        database_state_known = True
        record_exists = False
        try:
            record_exists = (
                db.query(Paper.id)
                .filter(Paper.file_hash == file_hash)
                .first()
                is not None
            )
        except Exception:
            database_state_known = False
            logger.exception(
                "Could not determine whether upload committed for file_hash=%s",
                file_hash,
            )
        if created_file:
            if database_state_known and not record_exists:
                try:
                    file_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "Database save failed and uploaded PDF cleanup also failed: %s",
                        file_path,
                    )
            else:
                logger.error(
                    "Preserving uploaded PDF because the database outcome is "
                    "committed or uncertain: file=%s",
                    file_path,
                )
        logger.exception("Database write failed while saving paper filename=%s", filename)
        raise AppError(
            "论文数据库写入失败，未创建论文记录。",
            code="PAPER_SAVE_FAILED",
            status_code=500,
        ) from exc

    try:
        db.refresh(paper)
    except Exception as exc:
        logger.exception(
            "Paper was committed but could not be refreshed: paper_id=%s",
            paper.id,
        )
        raise AppError(
            "论文已保存，但无法读取刚保存的记录。",
            code="PAPER_REFRESH_FAILED",
            status_code=500,
        ) from exc
    return paper


# ==============================
# 搜索论文片段
# ==============================

def _store_resumable_pdf(file_hash: str, content: bytes) -> tuple[Path, bool]:
    upload_dir = _upload_dir()
    file_path = upload_dir / f"{file_hash}.pdf"
    ensure_storage()
    created_file = False
    try:
        with file_path.open("xb") as stored_file:
            created_file = True
            stored_file.write(content)
    except FileExistsError:
        if hashlib.sha256(file_path.read_bytes()).hexdigest() != file_hash:
            raise AppError(
                "上传文件存储路径已被不同内容占用。",
                code="FILE_STORAGE_CONFLICT",
                status_code=500,
            )
    except OSError as exc:
        if created_file:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove partially written PDF at %s", file_path)
        raise AppError(
            "无法保存上传的 PDF 文件。",
            code="FILE_STORAGE_FAILED",
            status_code=500,
        ) from exc
    return file_path, created_file


def _checkpoint_context(page: PaperPage | None) -> tuple[list[str], bool]:
    if page is None:
        return [], False
    return _checkpoint_context_values(
        page.checkpoint_section_path,
        page.checkpoint_in_references,
    )


def _checkpoint_context_values(
    serialized_section_path: str | None,
    in_references: bool,
) -> tuple[list[str], bool]:
    if not serialized_section_path:
        return [], bool(in_references)
    try:
        section_path = json.loads(serialized_section_path)
    except (TypeError, ValueError):
        section_path = []
    if not isinstance(section_path, list):
        section_path = []
    return (
        [str(item) for item in section_path],
        bool(in_references),
    )


def _aggregate_page_parse_status(
    statuses: set[str],
    has_text: bool,
    has_elements: bool,
) -> str:
    if "ocr_failed" in statuses or "ocr_no_text" in statuses:
        return "partial_ocr" if has_text else "ocr_failed"
    if statuses.intersection({"ocr_unavailable", "ocr_disabled"}):
        return "needs_ocr"
    if has_text:
        return "parsed"
    if "no_text" in statuses:
        return "no_text"
    if has_elements:
        return "no_text"
    return "empty"


def _delete_incomplete_parse_state(db: Session, paper_id: int) -> None:
    db.query(PaperChunk).filter(PaperChunk.paper_id == paper_id).delete(
        synchronize_session=False
    )
    db.query(PaperElement).filter(PaperElement.paper_id == paper_id).delete(
        synchronize_session=False
    )
    db.query(PaperPage).filter(PaperPage.paper_id == paper_id).delete(
        synchronize_session=False
    )
    db.expire_all()


def get_paper_parse_completeness(
    db: Session,
    paper_id: int,
) -> PaperParseCompleteness:
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if paper is None:
        raise AppError("论文不存在", code="NOT_FOUND", status_code=404)

    expected_page_count = int(
        paper.parse_source_page_count or paper.page_count or 0
    )
    status_counts = {
        status: int(count)
        for status, count in (
            db.query(PaperPage.checkpoint_status, func.count(PaperPage.id))
            .filter(PaperPage.paper_id == paper_id)
            .group_by(PaperPage.checkpoint_status)
            .all()
        )
    }
    recorded_page_count = sum(status_counts.values())
    completed_page_count = status_counts.get("completed", 0)
    failed_page_count = status_counts.get("failed", 0)
    processing_page_count = status_counts.get("processing", 0)
    pending_page_count = status_counts.get("pending", 0)
    pending_page_count += sum(
        count
        for status, count in status_counts.items()
        if status not in {"completed", "failed", "processing", "pending"}
    )
    missing_page_count = max(expected_page_count - recorded_page_count, 0)
    legacy_assumed_complete = (
        paper.parse_checkpoint_version != PDF_CHECKPOINT_SCHEMA_VERSION
        and paper.parse_status in LEGACY_COMPLETE_PARSE_STATUSES
        and expected_page_count > 0
        and recorded_page_count == expected_page_count
    )

    page_numbers = {
        int(page_number)
        for (page_number,) in (
            db.query(PaperPage.page_number)
            .filter(PaperPage.paper_id == paper_id)
            .all()
        )
    }
    incomplete_page_numbers = sorted(
        set(range(1, expected_page_count + 1)) - page_numbers
    )
    incomplete_page_numbers.extend(
        int(page_number)
        for (page_number,) in (
            db.query(PaperPage.page_number)
            .filter(
                PaperPage.paper_id == paper_id,
                PaperPage.checkpoint_status != "completed",
            )
            .order_by(PaperPage.page_number.asc())
            .all()
        )
    )
    incomplete_page_numbers = sorted(set(incomplete_page_numbers))[:100]

    return PaperParseCompleteness(
        paper_id=int(paper.id),
        expected_page_count=expected_page_count,
        recorded_page_count=recorded_page_count,
        completed_page_count=completed_page_count,
        failed_page_count=failed_page_count,
        processing_page_count=processing_page_count,
        pending_page_count=pending_page_count,
        missing_page_count=missing_page_count,
        incomplete_page_numbers=tuple(incomplete_page_numbers),
        legacy_assumed_complete=legacy_assumed_complete,
    )


def create_or_resume_paper(
    db: Session,
    filename: str,
    content: bytes,
    content_type: str,
    on_saved: Callable[[Paper], None] | None = None,
    lease_guard: Callable[[], None] | None = None,
    commit_lease_guard: Callable[[], None] | None = None,
    worker_owner: str | None = None,
    worker_generation: int = 0,
    progress_callback: Callable[[int, int], None] | None = None,
    page_observer: Callable[[int], None] | None = None,
    reject_if_complete: bool = False,
) -> Paper:
    parse_scope: dict[str, object] = {}

    def begin_parse_scope(paper: Paper) -> None:
        if "context" in parse_scope:
            return
        context = paper_operation_coordinator.parsing(paper.id)
        context.__enter__()
        parse_scope["context"] = context

    try:
        return _create_or_resume_paper_impl(
            db=db,
            filename=filename,
            content=content,
            content_type=content_type,
            on_saved=on_saved,
            lease_guard=lease_guard,
            commit_lease_guard=commit_lease_guard,
            worker_owner=worker_owner,
            worker_generation=worker_generation,
            progress_callback=progress_callback,
            page_observer=page_observer,
            reject_if_complete=reject_if_complete,
            on_paper_ready=begin_parse_scope,
        )
    finally:
        context = parse_scope.get("context")
        if context is not None:
            context.__exit__(None, None, None)


def _create_or_resume_paper_impl(
    db: Session,
    filename: str,
    content: bytes,
    content_type: str,
    on_saved: Callable[[Paper], None] | None = None,
    lease_guard: Callable[[], None] | None = None,
    commit_lease_guard: Callable[[], None] | None = None,
    worker_owner: str | None = None,
    worker_generation: int = 0,
    progress_callback: Callable[[int, int], None] | None = None,
    page_observer: Callable[[int], None] | None = None,
    reject_if_complete: bool = False,
    on_paper_ready: Callable[[Paper], None] | None = None,
) -> Paper:
    """Persist completed PDF pages and continue from the first unfinished page."""
    settings = get_settings()
    validate_pdf_bytes(content, filename, settings.max_upload_bytes)
    source = inspect_pdf_source(content)
    page_count = int(source["page_count"])
    pdf_metadata = source["pdf_metadata"]
    file_hash = hashlib.sha256(content).hexdigest()

    def guard() -> None:
        if lease_guard is not None:
            lease_guard()

    def commit_guard() -> None:
        if commit_lease_guard is not None:
            commit_lease_guard()
        else:
            guard()

    paper = (
        db.query(Paper)
        .filter(Paper.file_hash == file_hash)
        .first()
    )
    created_file = False
    if paper is None:
        file_path, created_file = _store_resumable_pdf(file_hash, content)
        paper = Paper(
            title=Path(filename).stem[:500] or "未命名论文",
            file_hash=file_hash,
            original_filename=filename,
            stored_filename=file_path.name,
            file_path=str(file_path),
            file_size=len(content),
            content_type=content_type or "application/pdf",
            page_count=page_count,
            char_count=0,
            authors="未知作者",
            pdf_metadata=json.dumps(pdf_metadata, ensure_ascii=False),
            parse_status="pending",
            parse_checkpoint_version=PDF_CHECKPOINT_SCHEMA_VERSION,
            parse_source_hash=file_hash,
            parse_source_size=len(content),
            parse_source_page_count=page_count,
            index_status="pending",
        )
        try:
            guard()
            db.add(paper)
            db.flush()
            if on_paper_ready is not None:
                on_paper_ready(paper)
            if on_saved is not None:
                on_saved(paper)
            db.commit()
        except Exception as exc:
            db.rollback()
            if created_file:
                try:
                    file_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("Could not remove checkpoint PDF at %s", file_path)
            if isinstance(exc, AppError):
                raise
            raise AppError(
                "论文数据库写入失败，未创建论文记录。",
                code="PAPER_SAVE_FAILED",
                status_code=500,
            ) from exc
    else:
        if on_paper_ready is not None:
            on_paper_ready(paper)
        if on_saved is not None:
            guard()
            on_saved(paper)
            db.commit()

    checkpoint_valid = (
        paper.parse_checkpoint_version == PDF_CHECKPOINT_SCHEMA_VERSION
        and paper.parse_source_hash == file_hash
        and paper.parse_source_size == len(content)
        and paper.parse_source_page_count == page_count
    )
    fully_parsed = (
        paper.parse_status in LEGACY_COMPLETE_PARSE_STATUSES
        and paper.parse_checkpoint_version != PDF_CHECKPOINT_SCHEMA_VERSION
    )
    if fully_parsed and paper.page_count == page_count:
        if reject_if_complete:
            raise AppError(
                f"检测到完全相同的重复文件！库中已存在：{paper.original_filename}",
                code="DUPLICATE_PAPER",
                status_code=409,
            )
        return paper

    if not checkpoint_valid:
        guard()
        _delete_incomplete_parse_state(db, paper.id)
        paper.parse_checkpoint_version = PDF_CHECKPOINT_SCHEMA_VERSION
        paper.parse_source_hash = file_hash
        paper.parse_source_size = len(content)
        paper.parse_source_page_count = page_count
        paper.page_count = page_count
        paper.parse_status = "pending"
        paper.parse_layout_title = None
        paper.pdf_metadata = json.dumps(pdf_metadata, ensure_ascii=False)
        db.commit()

    first_incomplete = (
        db.query(PaperPage.page_number)
        .filter(
            PaperPage.paper_id == paper.id,
            PaperPage.checkpoint_status != "completed",
        )
        .order_by(PaperPage.page_number.asc())
        .first()
    )
    stored_page_count = (
        db.query(func.count(PaperPage.id))
        .filter(PaperPage.paper_id == paper.id)
        .scalar()
        or 0
    )
    checkpoint_rows = (
        db.query(
            PaperPage.page_number,
            PaperPage.checkpoint_status,
            PaperPage.checkpoint_section_path,
            PaperPage.checkpoint_in_references,
        )
        .filter(PaperPage.paper_id == paper.id)
        .order_by(PaperPage.page_number.asc())
        .all()
    )
    completed_page_numbers = {
        int(page_number)
        for page_number, checkpoint_status, _section_path, _in_references
        in checkpoint_rows
        if checkpoint_status == "completed"
    }
    checkpoint_context_by_page = {
        int(page_number): (
            _checkpoint_context_values(section_path, in_references)
        )
        for (
            page_number,
            _checkpoint_status,
            section_path,
            in_references,
        ) in checkpoint_rows
    }
    if first_incomplete is None and stored_page_count == page_count:
        if reject_if_complete:
            raise AppError(
                f"检测到完全相同的重复文件！库中已存在：{paper.original_filename}",
                code="DUPLICATE_PAPER",
                status_code=409,
            )
        _finalize_checkpoint_paper(db, paper, filename, pdf_metadata)
        return paper

    start_page = (
        int(first_incomplete[0])
        if first_incomplete is not None
        else stored_page_count + 1
    )
    completed_count = max(start_page - 1, 0)
    previous_page = (
        db.query(PaperPage)
        .filter(
            PaperPage.paper_id == paper.id,
            PaperPage.page_number == start_page - 1,
        )
        .first()
    )
    initial_section_path, initial_in_references = _checkpoint_context(previous_page)
    next_chunk_index_row = (
        db.query(PaperChunk.chunk_index)
        .filter(PaperChunk.paper_id == paper.id)
        .order_by(PaperChunk.chunk_index.desc())
        .first()
    )
    next_chunk_index = (
        int(next_chunk_index_row[0]) + 1
        if next_chunk_index_row is not None
        else 0
    )
    current_page_number: list[int | None] = [None]
    completed_pages: set[int] = set()

    guard()
    paper.parse_status = "parsing"
    db.commit()

    def mark_page_processing_locked(page_number: int) -> None:
        if page_observer is not None:
            page_observer(page_number)
        guard()
        db.expire_all()
        current_page_number[0] = page_number
        page = (
            db.query(PaperPage)
            .filter(
                PaperPage.paper_id == paper.id,
                PaperPage.page_number == page_number,
            )
            .first()
        )
        if page is not None and page.checkpoint_generation > worker_generation:
            raise RuntimeError(
                f"Page {page_number} belongs to generation "
                f"{page.checkpoint_generation}, current worker is {worker_generation}."
            )
        if page is None:
            page = PaperPage(
                paper_id=paper.id,
                page_number=page_number,
                text="",
                char_count=0,
            )
            db.add(page)
        page.checkpoint_status = "processing"
        page.checkpoint_generation = worker_generation
        page.checkpoint_worker = worker_owner
        page.checkpoint_error = None
        page.checkpoint_updated_at = datetime.utcnow()
        db.commit()

    def mark_page_processing(page_number: int) -> None:
        with paper_operation_coordinator.collection_mutation():
            mark_page_processing_locked(page_number)

    def persist_completed_page_locked(page_data: dict) -> None:
        nonlocal next_chunk_index
        guard()
        db.expire_all()
        page_number = int(page_data["page_number"])
        page = (
            db.query(PaperPage)
            .filter(
                PaperPage.paper_id == paper.id,
                PaperPage.page_number == page_number,
            )
            .one()
        )
        if page.checkpoint_generation > worker_generation:
            raise RuntimeError(
                f"Stale worker {worker_generation} cannot overwrite page "
                f"generation {page.checkpoint_generation}."
            )

        db.query(PaperChunk).filter(
            PaperChunk.paper_id == paper.id,
            PaperChunk.page_number == page_number,
        ).delete(synchronize_session=False)
        db.query(PaperElement).filter(
            PaperElement.paper_id == paper.id,
            PaperElement.page_number == page_number,
        ).delete(synchronize_session=False)

        page.text = page_data["text"]
        page.char_count = page_data["char_count"]
        page.parse_status = page_data["parse_status"]
        page.ocr_executed = page_data["ocr_executed"]
        page.ocr_error = page_data["ocr_error"]
        checkpoint_failed = page_data["parse_status"] in {
            "ocr_failed",
            "ocr_no_text",
            "ocr_unavailable",
            "ocr_disabled",
        }
        page.checkpoint_status = "failed" if checkpoint_failed else "completed"
        page.checkpoint_generation = worker_generation
        page.checkpoint_worker = worker_owner
        page.checkpoint_error = (
            page_data.get("ocr_error")
            or (
                f"Page checkpoint requires OCR retry: {page_data['parse_status']}"
                if checkpoint_failed
                else None
            )
        )
        page.checkpoint_section_path = json.dumps(
            page_data["checkpoint_section_path"],
            ensure_ascii=False,
        )
        page.checkpoint_in_references = page_data["checkpoint_in_references"]
        page.checkpoint_updated_at = datetime.utcnow()
        if page_number == 1:
            paper.parse_layout_title = page_data.get("checkpoint_layout_title")
        db.flush()

        element_rows = []
        for element_index, element in enumerate(page_data.get("elements", [])):
            row = PaperElement(
                paper_id=paper.id,
                page_id=page.id,
                page_number=page_number,
                element_index=element_index,
                element_type=element["element_type"],
                text=element.get("text", ""),
                data=(
                    json.dumps(element["data"], ensure_ascii=False)
                    if element.get("data") is not None
                    else None
                ),
                bbox=(
                    json.dumps(element["bbox"])
                    if element.get("bbox") is not None
                    else None
                ),
                section_path=element.get("section_path"),
                extraction_status=element.get("extraction_status", "extracted"),
                confidence=element.get("confidence"),
            )
            db.add(row)
            element_rows.append((element, row))
        db.flush()

        text = page_data["text"]
        page_chunk_index = 0
        start = 0
        while start < len(text):
            end = min(start + 500, len(text))
            if end < len(text):
                cut_point = max(
                    text.rfind("\n", start, end),
                    text.rfind("。", start, end),
                    text.rfind(". ", start, end),
                )
                if cut_point > start + 100:
                    end = cut_point + 1
            chunk_text = text[start:end].strip()
            if chunk_text:
                db.add(
                    PaperChunk(
                        paper_id=paper.id,
                        page_number=page_number,
                        page_chunk_index=page_chunk_index,
                        chunk_index=next_chunk_index,
                        text=chunk_text,
                        content_type="paragraph",
                    )
                )
                page_chunk_index += 1
                next_chunk_index += 1
            start = max(end, start + 1)

        for element, element_row in element_rows:
            if element["element_type"] != "table" or not element.get("text", "").strip():
                continue
            db.add(
                PaperChunk(
                    paper_id=paper.id,
                    page_number=page_number,
                    page_chunk_index=page_chunk_index,
                    chunk_index=next_chunk_index,
                    text=f"[TABLE]\n{element['text']}",
                    content_type="table",
                    element_id=element_row.id,
                )
            )
            page_chunk_index += 1
            next_chunk_index += 1
        commit_guard()
        db.commit()
        completed_pages.add(page_number)
        if progress_callback is not None:
            try:
                progress_callback(completed_count + len(completed_pages), page_count)
            except Exception:
                logger.exception(
                    "Could not update parse progress after page %s",
                    page_number,
                )

    def persist_completed_page(page_data: dict) -> None:
        with paper_operation_coordinator.collection_mutation():
            persist_completed_page_locked(page_data)

    try:
        extraction = extract_pdf(
            content,
            filename,
            page_callback=persist_completed_page,
            collect_pages=False,
            start_page=start_page,
            initial_section_path=initial_section_path,
            initial_in_references=initial_in_references,
            page_start_callback=mark_page_processing,
            page_selection_callback=lambda page_number: (
                page_number not in completed_page_numbers
            ),
            page_skip_state_callback=lambda page_number: (
                checkpoint_context_by_page.get(page_number, ([], False))
            ),
        )
    except Exception as exc:
        db.rollback()
        failed_page = current_page_number[0]
        failure_text = str(exc)
        details = getattr(exc, "details", None)
        if details:
            failure_text = f"{failure_text}: {details}"
        if failed_page is not None and failed_page not in completed_pages:
            try:
                guard()
                db.expire_all()
                page = (
                    db.query(PaperPage)
                    .filter(
                        PaperPage.paper_id == paper.id,
                        PaperPage.page_number == failed_page,
                    )
                    .first()
                )
                if (
                    page is not None
                    and page.checkpoint_status != "completed"
                    and page.checkpoint_generation <= worker_generation
                ):
                    page.checkpoint_status = "failed"
                    page.checkpoint_error = failure_text[:1000]
                    page.checkpoint_generation = worker_generation
                    page.checkpoint_worker = worker_owner
                    page.checkpoint_updated_at = datetime.utcnow()
                paper.parse_status = "failed"
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "Could not persist failed checkpoint for paper_id=%s page=%s",
                    paper.id,
                    failed_page,
                )
        raise

    guard()
    db.expire_all()
    _finalize_checkpoint_paper(
        db,
        paper,
        filename,
        pdf_metadata,
        guard=guard,
    )
    return paper


def _finalize_checkpoint_paper(
    db: Session,
    paper: Paper,
    filename: str,
    pdf_metadata: dict,
    guard: Callable[[], None] | None = None,
) -> None:
    first_pages_text = "\n".join(
        text
        for (text,) in (
            db.query(PaperPage.text)
            .filter(
                PaperPage.paper_id == paper.id,
                PaperPage.page_number <= 2,
            )
            .order_by(PaperPage.page_number.asc())
            .all()
        )
    )
    derived = derive_pdf_metadata(
        pdf_metadata,
        first_pages_text,
        filename,
        paper.parse_layout_title,
    )
    if guard is not None:
        guard()
    paper.title = derived["title"][:500]
    paper.authors = derived["authors"] or "未知作者"
    publication_metadata = derived["publication_metadata"]
    paper.doi = publication_metadata["doi"]
    paper.normalized_title = publication_metadata["normalized_title"]
    paper.publication_year = publication_metadata["publication_year"]
    paper.venue = publication_metadata["venue"]
    paper.normalized_venue = publication_metadata["normalized_venue"]
    persisted_page_count = int(
        db.query(func.count(PaperPage.id))
        .filter(PaperPage.paper_id == paper.id)
        .scalar()
        or 0
    )
    if persisted_page_count != paper.parse_source_page_count:
        raise RuntimeError(
            "PDF checkpoint page count did not converge: "
            f"persisted={persisted_page_count} "
            f"source={paper.parse_source_page_count}."
        )
    paper.page_count = persisted_page_count
    paper.char_count = int(
        db.query(func.coalesce(func.sum(PaperPage.char_count), 0))
        .filter(PaperPage.paper_id == paper.id)
        .scalar()
        or 0
    )
    statuses = {
        status
        for (status,) in (
            db.query(PaperPage.parse_status)
            .filter(PaperPage.paper_id == paper.id)
            .distinct()
            .all()
        )
    }
    has_text = (
        db.query(PaperPage.id)
        .filter(
            PaperPage.paper_id == paper.id,
            PaperPage.char_count > 0,
        )
        .first()
        is not None
    )
    has_elements = (
        db.query(PaperElement.id)
        .filter(PaperElement.paper_id == paper.id)
        .first()
        is not None
    )
    paper.parse_status = _aggregate_page_parse_status(
        statuses,
        has_text,
        has_elements,
    )
    paper.pdf_metadata = json.dumps(pdf_metadata, ensure_ascii=False)
    db.commit()


def search_chunks(db: Session, query: str, limit: int = 3):

    if not query.strip():
        return []

    # 第一步：先用完整的问题去搜
    results = (
        db.query(PaperChunk, Paper)
        .join(Paper, PaperChunk.paper_id == Paper.id)
        .filter(PaperChunk.text.contains(query))
        .limit(limit)
        .all()
    )

    print(f"\n--- [搜索调试] 原始查询: '{query}' ---")
    print(f"第一步（完整句子匹配）结果数: {len(results)}")

    # 第二步：如果完整问题搜不到，尝试提取核心英文单词去搜
    if not results:
        english_words = re.findall(r'[a-zA-Z]+', query)

        if english_words:
            # 拿最长的那个英文单词去搜
            longest_word = max(english_words, key=len)

            print(
                f"第二步（提取英文单词）: 尝试搜索 '{longest_word}'"
            )

            results = (
                db.query(PaperChunk, Paper)
                .join(Paper, PaperChunk.paper_id == Paper.id)
                .filter(PaperChunk.text.contains(longest_word))
                .limit(limit)
                .all()
            )

            print(f"第二步结果数: {len(results)}")

    # 第三步：如果还是搜不到，尝试用中文关键词匹配
    if not results:
        chinese_words = re.findall(r'[\u4e00-\u9fa5]+', query)

        for word in chinese_words:
            if len(word) >= 2:
                print(
                    f"第三步（提取中文词）: 尝试搜索 '{word}'"
                )

                results = (
                    db.query(PaperChunk, Paper)
                    .join(Paper, PaperChunk.paper_id == Paper.id)
                    .filter(PaperChunk.text.contains(word))
                    .limit(limit)
                    .all()
                )

                if results:
                    print(f"第三步结果数: {len(results)}")
                    break

    print(
        f"--- [搜索结束] 最终返回 {len(results)} 条证据 ---\n"
    )

    return [
        {
            "chunk_id": chunk.id,
            "paper_id": chunk.paper_id,
            "paper_title": paper.title,
            "page_number": chunk.page_number,
            "text": chunk.text
        }
        for chunk, paper in results
    ]


# ==============================
# 论文列表与详情
# ==============================

def list_papers(db: Session):
    return db.query(Paper).order_by(Paper.created_at.desc()).all()


def get_paper(db: Session, paper_id: int, with_pages: bool = False):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()

    if not paper:
        raise AppError(
            "论文不存在",
            code="NOT_FOUND",
            status_code=404
        )

    return paper


# ==============================
# 删除论文
# ==============================

def delete_paper(db: Session, paper_id: int) -> PendingPaperDeletion:
    with paper_operation_coordinator.deleting(paper_id):
        return _delete_paper_locked(db, paper_id)


def delete_paper_with_cleanup(
    db: Session,
    paper_id: int,
    delete_vectors: Callable[[int], None],
    upload_dir: Path | None = None,
) -> tuple[PendingPaperDeletion, bool]:
    """Keep deletion exclusive through SQLite, vector, and file cleanup."""
    with paper_operation_coordinator.deleting(paper_id):
        deletion = _delete_paper_locked(db, paper_id)
        cleanup_pending = finish_paper_deletion(
            deletion,
            delete_vectors,
            upload_dir,
        )
        return deletion, cleanup_pending


def _delete_paper_locked(db: Session, paper_id: int) -> PendingPaperDeletion:
    paper = get_paper(db, paper_id)
    if paper.version is not None and paper.version.retained:
        raise AppError(
            "该论文版本已标记为保留，请先取消保留后再删除。",
            code="PAPER_VERSION_RETAINED",
            status_code=409,
        )
    upload_dir = _upload_dir()
    file_path = Path(paper.file_path)
    safe_path = _safe_upload_file_path(file_path, upload_dir)
    deletion = PendingPaperDeletion(
        paper_id=paper.id,
        file_path=file_path,
        journal_path=_journal_path(paper.id, upload_dir),
        file_pending=True,
    )
    if safe_path is None:
        logger.error(
            "Paper file is outside the configured upload directory; "
            "database deletion will not remove it: paper_id=%s path=%s",
            paper_id,
            file_path,
        )

    try:
        _write_deletion_journal(deletion)
    except OSError as exc:
        logger.exception("Could not prepare deletion journal for paper_id=%s", paper_id)
        raise AppError(
            "无法安全准备论文清理任务，论文未删除。",
            code="DELETE_PREPARATION_FAILED",
            status_code=500,
        ) from exc

    try:
        with paper_operation_coordinator.collection_mutation():
            db.delete(paper)
            db.commit()
    except Exception as exc:
        db.rollback()
        database_state_known = True
        paper_still_exists = False
        try:
            paper_still_exists = (
                db.query(Paper.id)
                .filter(Paper.id == paper_id)
                .first()
                is not None
            )
        except Exception:
            database_state_known = False
            logger.exception(
                "Could not determine database outcome for failed deletion paper_id=%s; "
                "the recovery journal is retained",
                paper_id,
            )
        if database_state_known and paper_still_exists:
            try:
                deletion.journal_path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Could not cancel deletion journal for paper_id=%s; "
                    "startup recovery will inspect it",
                    paper_id,
                )
        elif database_state_known:
            logger.error(
                "Database record is absent after deletion raised for paper_id=%s; "
                "startup recovery will finish cleanup",
                paper_id,
            )
        logger.exception("Database deletion failed for paper_id=%s", paper_id)
        raise AppError(
            "论文数据库删除失败，PDF 文件已保留。",
            code="PAPER_DELETE_FAILED",
            status_code=500,
        ) from exc
    return deletion


# ==============================
# 论文数据序列化
# ==============================

def serialize_paper(paper: Paper, include_pages: bool = False):

    # 解析 PDF 元数据
    pdf_metadata = None

    if paper.pdf_metadata:
        try:
            parsed_metadata = json.loads(paper.pdf_metadata)

            if isinstance(parsed_metadata, dict):
                pdf_metadata = parsed_metadata

        except (TypeError, ValueError):
            pdf_metadata = None

    data = {
        "id": paper.id,
        "title": paper.title,
        "original_filename": paper.original_filename,
        "stored_filename": paper.stored_filename,
        "file_size": paper.file_size,
        "content_type": paper.content_type,
        "page_count": paper.page_count,
        "char_count": paper.char_count,
        "authors": paper.authors,
        "doi": paper.doi,
        "publication_year": paper.publication_year,
        "venue": paper.venue,
        "pdf_metadata": pdf_metadata,
        "parse_status": paper.parse_status,
        "index_status": paper.index_status,
        "created_at": (
            paper.created_at.isoformat()
            if paper.created_at else None
        ),
        "updated_at": (
            paper.updated_at.isoformat()
            if paper.updated_at else None
        ),
    }

    if include_pages:
        data["pages"] = [
            {
                "page_number": page.page_number,
                "text": page.text,
                "char_count": page.char_count,
                "parse_status": page.parse_status,
                "ocr_executed": page.ocr_executed,
                "ocr_error": page.ocr_error,
                "checkpoint_status": page.checkpoint_status,
                "checkpoint_error": page.checkpoint_error,
                "checkpoint_generation": page.checkpoint_generation,
                "elements": [
                    {
                        "id": element.id,
                        "element_index": element.element_index,
                        "element_type": element.element_type,
                        "text": element.text,
                        "data": _load_json(element.data),
                        "bbox": _load_json(element.bbox),
                        "section_path": element.section_path,
                        "extraction_status": element.extraction_status,
                        "confidence": element.confidence,
                    }
                    for element in page.elements
                ],
            }
            for page in paper.pages
        ]

    return data


def _load_json(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        logger.exception("Could not decode stored PDF structure")
        return None


def list_paper_versions(db: Session, paper_id: int) -> list[dict]:
    paper = get_paper(db, paper_id)
    if paper.version is None:
        return []
    records = (
        db.query(PaperVersion, Paper)
        .join(Paper, Paper.id == PaperVersion.paper_id)
        .filter(PaperVersion.group_id == paper.version.group_id)
        .order_by(PaperVersion.version_number.asc())
        .all()
    )
    return [_serialize_version(version, related_paper) for version, related_paper in records]


def add_paper_version(
    db: Session,
    paper_id: int,
    based_on_id: int,
    source: str = "manual",
) -> list[dict]:
    paper = get_paper(db, paper_id)
    based_on = get_paper(db, based_on_id)
    if paper_id == based_on_id:
        raise AppError("不能将论文登记为自身的版本。", code="INVALID_VERSION", status_code=400)

    paper_version = paper.version
    based_version = based_on.version
    if paper_version is not None and based_version is not None:
        if paper_version.group_id != based_version.group_id:
            raise AppError(
                "两篇论文已属于不同版本组，不能自动合并。",
                code="VERSION_GROUP_CONFLICT",
                status_code=409,
            )
        return list_paper_versions(db, paper_id)

    group_id = (
        paper_version.group_id
        if paper_version is not None
        else based_version.group_id
        if based_version is not None
        else str(uuid4())
    )
    current_versions = (
        db.query(PaperVersion)
        .filter(PaperVersion.group_id == group_id)
        .all()
    )
    next_number = max((item.version_number for item in current_versions), default=0) + 1
    try:
        if based_version is None:
            db.add(
                PaperVersion(
                    group_id=group_id,
                    paper_id=based_on.id,
                    version_number=next_number,
                    source=source,
                    version_label=based_on.title[:512],
                )
            )
            next_number += 1
        if paper_version is None:
            db.add(
                PaperVersion(
                    group_id=group_id,
                    paper_id=paper.id,
                    version_number=next_number,
                    source=source,
                    version_label=paper.title[:512],
                )
            )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Could not register paper versions paper_id=%s based_on_id=%s", paper_id, based_on_id)
        raise AppError(
            "无法保存论文版本关系。",
            code="VERSION_SAVE_FAILED",
            status_code=500,
        ) from exc
    return list_paper_versions(db, paper_id)


def retain_paper_version(db: Session, paper_id: int, retained_paper_id: int) -> list[dict]:
    versions = list_paper_versions(db, paper_id)
    if not any(item["paper_id"] == retained_paper_id for item in versions):
        raise AppError(
            "指定论文不属于当前版本组。",
            code="VERSION_NOT_FOUND",
            status_code=404,
        )
    group_id = db.query(PaperVersion.group_id).filter(PaperVersion.paper_id == paper_id).scalar()
    db.query(PaperVersion).filter(PaperVersion.group_id == group_id).update(
        {PaperVersion.retained: False},
        synchronize_session=False,
    )
    db.query(PaperVersion).filter(PaperVersion.paper_id == retained_paper_id).update(
        {PaperVersion.retained: True},
        synchronize_session=False,
    )
    db.commit()
    return list_paper_versions(db, paper_id)


def compare_paper_versions(
    db: Session,
    paper_id: int,
    other_paper_id: int,
    max_diff_chars: int = 20000,
) -> dict:
    versions = list_paper_versions(db, paper_id)
    version_ids = {item["paper_id"] for item in versions}
    if other_paper_id not in version_ids or paper_id not in version_ids:
        raise AppError(
            "只有同一版本组内的论文可以比较。",
            code="VERSION_NOT_FOUND",
            status_code=404,
        )
    first = get_paper(db, paper_id)
    second = get_paper(db, other_paper_id)
    first_text = "\n".join(page.text for page in first.pages)
    second_text = "\n".join(page.text for page in second.pages)
    diff_lines = list(
        difflib.unified_diff(
            first_text.splitlines(),
            second_text.splitlines(),
            fromfile=first.title,
            tofile=second.title,
            lineterm="",
        )
    )
    diff = "\n".join(diff_lines)
    return {
        "paper_id": paper_id,
        "other_paper_id": other_paper_id,
        "diff": diff[:max_diff_chars],
        "truncated": len(diff) > max_diff_chars,
    }


def _serialize_version(version: PaperVersion, paper: Paper) -> dict:
    return {
        "paper_id": paper.id,
        "version_number": version.version_number,
        "version_label": version.version_label or paper.title,
        "source": version.source,
        "retained": version.retained,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "title": paper.title,
        "original_filename": paper.original_filename,
        "created_at_paper": paper.created_at.isoformat() if paper.created_at else None,
    }
