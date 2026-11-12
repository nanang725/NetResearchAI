from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.core.exceptions import AppError
from app.models.paper import Paper, PaperPage
from app.services.pdf_service import dump_metadata, extract_pdf, validate_pdf_bytes


def list_papers(db: Session) -> list[Paper]:
    stmt = select(Paper).order_by(Paper.created_at.desc())
    return list(db.scalars(stmt).all())


def get_paper(db: Session, paper_id: int, with_pages: bool = True) -> Paper:
    stmt = select(Paper).where(Paper.id == paper_id)
    if with_pages:
        stmt = stmt.options(selectinload(Paper.pages))
    paper = db.scalars(stmt).first()
    if paper is None:
        raise AppError("论文不存在。", code="PAPER_NOT_FOUND", status_code=404)
    return paper


def create_paper(db: Session, filename: str, content: bytes, content_type: str | None) -> Paper:
    settings = get_settings()
    validate_pdf_bytes(content, filename, settings.max_upload_bytes)

    upload_dir = settings.upload_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid.uuid4().hex}.pdf"
    file_path = upload_dir / stored_filename

    try:
        file_path.write_bytes(content)
        extracted = extract_pdf(file_path)
        paper = Paper(
            title=extracted["title"],
            original_filename=filename,
            stored_filename=stored_filename,
            file_path=str(file_path),
            file_size=len(content),
            content_type=content_type or "application/pdf",
            page_count=extracted["page_count"],
            char_count=extracted["char_count"],
            authors=extracted["authors"],
            pdf_metadata=dump_metadata(extracted["pdf_metadata"]),
            parse_status="parsed",
            index_status="pending",
        )
        db.add(paper)
        db.flush()
        for page in extracted["pages"]:
            db.add(
                PaperPage(
                    paper_id=paper.id,
                    page_number=page["page_number"],
                    text=page["text"],
                    char_count=page["char_count"],
                )
            )
        db.commit()
        db.refresh(paper)
        return get_paper(db, paper.id, with_pages=True)
    except Exception:
        db.rollback()
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        raise


def delete_paper(db: Session, paper_id: int) -> None:
    paper = get_paper(db, paper_id, with_pages=False)
    file_path = Path(paper.file_path)
    db.delete(paper)
    db.commit()
    if file_path.exists():
        file_path.unlink(missing_ok=True)


def serialize_paper(paper: Paper, include_pages: bool = True) -> dict:
    metadata = None
    if paper.pdf_metadata:
        try:
            metadata = json.loads(paper.pdf_metadata)
        except json.JSONDecodeError:
            metadata = {"raw": paper.pdf_metadata}

    payload = {
        "id": paper.id,
        "title": paper.title,
        "original_filename": paper.original_filename,
        "stored_filename": paper.stored_filename,
        "file_size": paper.file_size,
        "content_type": paper.content_type,
        "page_count": paper.page_count,
        "char_count": paper.char_count,
        "authors": paper.authors,
        "parse_status": paper.parse_status,
        "index_status": paper.index_status,
        "pdf_metadata": metadata,
        "created_at": paper.created_at.isoformat() if paper.created_at else None,
        "updated_at": paper.updated_at.isoformat() if paper.updated_at else None,
    }
    if include_pages:
        payload["pages"] = [
            {
                "page_number": page.page_number,
                "text": page.text,
                "char_count": page.char_count,
            }
            for page in paper.pages
        ]
    return payload


def ensure_storage() -> None:
    settings = get_settings()
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.replace("sqlite:///", "", 1))
        db_path.parent.mkdir(parents=True, exist_ok=True)
