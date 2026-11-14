
import difflib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import fitz
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.models.paper import Paper, PaperPage, PaperChunk
from app.services.pdf_service import validate_pdf_bytes

logger = logging.getLogger(__name__)
DELETION_JOURNAL_DIR = ".pending_deletions"


@dataclass
class PendingPaperDeletion:
    paper_id: int
    file_path: Path
    journal_path: Path
    vector_pending: bool = True
    file_pending: bool = True


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

    try:
        with fitz.open(stream=content, filetype="pdf") as document:
            metadata = document.metadata or {}
            pages = [page.get_text("text") or "" for page in document]
    except Exception as exc:
        logger.exception("PDF parsing failed for filename=%s", filename)
        raise AppError(
            "无法解析 PDF 文件，请确认文件完整且未损坏。",
            code="PDF_PARSE_ERROR",
            status_code=400,
            details=str(exc),
        ) from exc

    text_sample = "".join(pages[:3])
    text_sample_clean = re.sub(r"[^a-z0-9]", "", text_sample.lower())
    if len(text_sample_clean) > 100:
        existing_papers = db.query(Paper).all()
        for existing_paper in existing_papers:
            existing_pages = (
                db.query(PaperPage)
                .filter(
                    PaperPage.paper_id == existing_paper.id,
                    PaperPage.page_number <= 3,
                )
                .all()
            )
            existing_text = "".join(page.text for page in existing_pages)
            existing_text_clean = re.sub(r"[^a-z0-9]", "", existing_text.lower())
            if not existing_text_clean:
                continue

            similarity = difflib.SequenceMatcher(
                None,
                text_sample_clean[:5000],
                existing_text_clean[:5000],
            ).ratio()
            logger.info(
                "Semantic duplicate check against paper_id=%s similarity=%.2f%%",
                existing_paper.id,
                similarity * 100,
            )
            if similarity > 0.50:
                raise AppError(
                    f"检测到高度重合的论文版本！库中已有：《{existing_paper.title}》。"
                    f"文本相似度高达 {similarity * 100:.1f}%。建议保留更新版本。",
                    code="DUPLICATE_VERSION",
                    status_code=409,
                )

    title = (metadata.get("title") or "").strip()
    if not title or len(title) < 5:
        lines = [line.strip() for line in (pages[0] if pages else "").splitlines() if line.strip()]
        title = lines[0] if lines else filename.rsplit(".", 1)[0]
    authors = (metadata.get("author") or "").strip() or "未知作者"
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

    page_count = len(pages)
    char_count = sum(len(text) for text in pages)
    paper = Paper(
        title=title[:500],
        file_hash=file_hash,
        original_filename=filename,
        stored_filename=stored_filename,
        file_path=str(file_path),
        file_size=len(content),
        content_type=content_type or "application/pdf",
        page_count=page_count,
        char_count=char_count,
        authors=authors,
        parse_status="parsed",
        index_status="pending",
        pdf_metadata=json.dumps(metadata, ensure_ascii=False),
    )

    try:
        db.add(paper)
        db.flush()
        chunk_index = 0
        for page_index, text in enumerate(pages):
            db.add(
                PaperPage(
                    paper_id=paper.id,
                    page_number=page_index + 1,
                    text=text,
                    char_count=len(text),
                )
            )
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
                            page_number=page_index + 1,
                            chunk_index=chunk_index,
                            text=chunk_text,
                        )
                    )
                    chunk_index += 1
                start = end
        db.commit()
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
    paper = get_paper(db, paper_id)
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
            }
            for page in paper.pages
        ]

    return data
