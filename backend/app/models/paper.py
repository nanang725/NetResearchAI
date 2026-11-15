from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/pdf")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    authors: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    pdf_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    normalized_title: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    publication_year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    venue: Mapped[str | None] = mapped_column(String(512), nullable=True)
    normalized_venue: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, default="parsed")
    parse_checkpoint_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    parse_source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parse_source_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    parse_source_page_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    parse_layout_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    index_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    pages: Mapped[list["PaperPage"]] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        order_by="PaperPage.page_number",
    )
    chunks: Mapped[list["PaperChunk"]] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        order_by=(
            "PaperChunk.page_number, PaperChunk.page_chunk_index, "
            "PaperChunk.chunk_index, PaperChunk.id"
        ),
    )
    elements: Mapped[list["PaperElement"]] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        order_by="PaperElement.page_number, PaperElement.element_index",
    )
    version: Mapped["PaperVersion | None"] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        uselist=False,
    )


class PaperPage(Base):
    __tablename__ = "paper_pages"
    __table_args__ = (UniqueConstraint("paper_id", "page_number", name="uq_paper_page"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, default="text_extracted")
    ocr_executed: Mapped[bool] = mapped_column(nullable=False, default=False)
    ocr_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    checkpoint_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    checkpoint_worker: Mapped[str | None] = mapped_column(String(36), nullable=True)
    checkpoint_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_in_references: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="0",
    )
    checkpoint_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    paper: Mapped[Paper] = relationship(back_populates="pages")
    elements: Mapped[list["PaperElement"]] = relationship(
        back_populates="page",
        cascade="all, delete-orphan",
        order_by="PaperElement.element_index",
    )


class PaperElement(Base):
    __tablename__ = "paper_elements"
    __table_args__ = (
        UniqueConstraint("page_id", "element_index", name="uq_paper_element_page_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("paper_pages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    element_index: Mapped[int] = mapped_column(Integer, nullable=False)
    element_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[str | None] = mapped_column(Text, nullable=True)
    bbox: Mapped[str | None] = mapped_column(Text, nullable=True)
    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False, default="extracted")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    paper: Mapped[Paper] = relationship(back_populates="elements")
    page: Mapped[PaperPage] = relationship(back_populates="elements")

class PaperChunk(Base):
    __tablename__ = "paper_chunks"

    page_chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        index=True,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False) # 记录这个块在第几页
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False) # 记录是第几个块
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(24), nullable=False, default="paragraph")
    element_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_elements.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    paper: Mapped[Paper] = relationship(back_populates="chunks")


class PaperImportJob(Base):
    __tablename__ = "paper_import_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="import")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    items: Mapped[list["PaperImportItem"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="PaperImportItem.created_at",
    )


class PaperImportItem(Base):
    __tablename__ = "paper_import_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    job_id: Mapped[str] = mapped_column(
        ForeignKey("paper_import_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    paper_id: Mapped[int | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    staged_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    index_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    lease_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    job: Mapped[PaperImportJob] = relationship(back_populates="items")


class PaperIndexRepairLock(Base):
    __tablename__ = "paper_index_repair_locks"

    target_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("paper_import_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )


class PaperVersion(Base):
    __tablename__ = "paper_versions"
    __table_args__ = (
        UniqueConstraint("paper_id", name="uq_paper_version_paper"),
        UniqueConstraint("group_id", "version_number", name="uq_paper_version_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False, default="manual")
    version_label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    retained: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    paper: Mapped[Paper] = relationship(back_populates="version")
