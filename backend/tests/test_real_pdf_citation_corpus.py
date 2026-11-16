from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import get_settings
from app.database import Base
from app.models.paper import Paper, PaperChunk, PaperElement, PaperPage
from app.services import citation_service
from app.services.paper_service import create_or_resume_paper


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
MANIFEST = BACKEND_ROOT / "tests" / "fixtures" / "real_pdf_manifest.json"


@pytest.mark.real_pdf
def test_real_pdf_reference_corpus_build_is_read_only_for_paper_content(
    tmp_path,
    monkeypatch,
):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    samples = [
        sample
        for sample in manifest["samples"]
        if sample["id"] in {"P02", "P04", "P08", "P09"}
    ]
    paths = [PROJECT_ROOT / sample["path"] for sample in samples]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        pytest.skip(f"Missing real PDF samples: {missing}")

    database_path = tmp_path / "real_citations.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("MAX_UPLOAD_MB", "30")
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    try:
        paper_ids = []
        for sample, path in zip(samples, paths, strict=True):
            content = path.read_bytes()
            assert hashlib.sha256(content).hexdigest() == sample["sha256"]
            with factory() as db:
                paper = create_or_resume_paper(
                    db,
                    filename=path.name,
                    content=content,
                    content_type="application/pdf",
                )
                paper_ids.append(paper.id)

        with factory() as db:
            before = {
                "pages": [
                    (page.paper_id, page.page_number, page.text)
                    for page in db.query(PaperPage).order_by(PaperPage.id).all()
                ],
                "elements": [
                    (
                        element.paper_id,
                        element.page_number,
                        element.element_index,
                        element.element_type,
                        element.text,
                    )
                    for element in db.query(PaperElement)
                    .order_by(PaperElement.id)
                    .all()
                ],
                "chunks": [
                    (
                        chunk.paper_id,
                        chunk.page_number,
                        chunk.page_chunk_index,
                        chunk.text,
                    )
                    for chunk in db.query(PaperChunk).order_by(PaperChunk.id).all()
                ],
            }
            reference_element_counts = {
                paper_id: (
                    db.query(PaperElement)
                    .filter(
                        PaperElement.paper_id == paper_id,
                        PaperElement.element_type == "reference",
                    )
                    .count()
                )
                for paper_id in paper_ids
            }

        with factory() as db:
            stats = citation_service.rebuild_citations(db)
            after = {
                "pages": [
                    (page.paper_id, page.page_number, page.text)
                    for page in db.query(PaperPage).order_by(PaperPage.id).all()
                ],
                "elements": [
                    (
                        element.paper_id,
                        element.page_number,
                        element.element_index,
                        element.element_type,
                        element.text,
                    )
                    for element in db.query(PaperElement)
                    .order_by(PaperElement.id)
                    .all()
                ],
                "chunks": [
                    (
                        chunk.paper_id,
                        chunk.page_number,
                        chunk.page_chunk_index,
                        chunk.text,
                    )
                    for chunk in db.query(PaperChunk).order_by(PaperChunk.id).all()
                ],
            }
            papers = [
                {
                    "paper_id": paper.id,
                    "title": paper.title,
                }
                for paper in db.query(Paper).order_by(Paper.id).all()
            ]

        assert before == after
        assert stats["reference_count"] > 0
        assert any(reference_element_counts.values())
        print(
            json.dumps(
                {
                    "papers": papers,
                    "reference_elements": reference_element_counts,
                    "citation_stats": stats,
                },
                ensure_ascii=False,
            )
        )
    finally:
        engine.dispose()
        get_settings.cache_clear()
