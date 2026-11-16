from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CitationReference(Base):
    __tablename__ = "citation_references"
    __table_args__ = (
        UniqueConstraint(
            "source_element_id",
            "source_entry_index",
            name="uq_citation_reference_source_entry",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_page_id: Mapped[int] = mapped_column(
        ForeignKey("paper_pages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_element_id: Mapped[int] = mapped_column(
        ForeignKey("paper_elements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_entry_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    normalized_title: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        index=True,
    )
    authors: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    venue: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_paper_id: Mapped[int | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    match_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="unmatched",
        index=True,
    )
    match_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    match_confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0",
    )
    parser_version: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="1",
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    edge: Mapped["CitationEdge | None"] = relationship(
        back_populates="reference",
        cascade="all, delete-orphan",
        uselist=False,
    )


class CitationEdge(Base):
    __tablename__ = "citation_edges"
    __table_args__ = (
        UniqueConstraint(
            "citing_paper_id",
            "cited_paper_id",
            name="uq_citation_edge_citing_cited",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    citing_paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cited_paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    citation_reference_id: Mapped[int] = mapped_column(
        ForeignKey("citation_references.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    match_method: Mapped[str] = mapped_column(String(32), nullable=False)
    match_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    reference: Mapped[CitationReference] = relationship(back_populates="edge")
