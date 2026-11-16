from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ExternalPaper(Base):
    __tablename__ = "external_papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arxiv_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    primary_category: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    abs_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    pdf_url: Mapped[str] = mapped_column(String(1024), nullable=False)
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

    favorite: Mapped["ExternalFavorite | None"] = relationship(
        back_populates="external_paper",
        cascade="all, delete-orphan",
        uselist=False,
    )
    tracking_seen: Mapped[list["TrackingSeen"]] = relationship(
        back_populates="external_paper",
        cascade="all, delete-orphan",
    )


class ExternalFavorite(Base):
    __tablename__ = "external_favorites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_paper_id: Mapped[int] = mapped_column(
        ForeignKey("external_papers.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    import_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="not_imported",
        server_default="not_imported",
        index=True,
    )
    import_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    import_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    local_paper_id: Mapped[int | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
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

    external_paper: Mapped[ExternalPaper] = relationship(
        back_populates="favorite"
    )


class TrackingSearch(Base):
    __tablename__ = "tracking_searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    query: Mapped[str] = mapped_column(String(300), nullable=False)
    field: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="all",
        server_default="all",
    )
    sort_by: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="submittedDate",
        server_default="submittedDate",
    )
    sort_order: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="descending",
        server_default="descending",
    )
    page_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=20,
        server_default="20",
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
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

    runs: Mapped[list["TrackingRun"]] = relationship(
        back_populates="tracking",
        cascade="all, delete-orphan",
        order_by="TrackingRun.id.desc()",
    )
    seen: Mapped[list["TrackingSeen"]] = relationship(
        back_populates="tracking",
        cascade="all, delete-orphan",
    )


class TrackingSeen(Base):
    __tablename__ = "tracking_seen"
    __table_args__ = (
        UniqueConstraint(
            "tracking_id",
            "external_paper_id",
            name="uq_tracking_seen_paper",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tracking_id: Mapped[int] = mapped_column(
        ForeignKey("tracking_searches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_paper_id: Mapped[int] = mapped_column(
        ForeignKey("external_papers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    tracking: Mapped[TrackingSearch] = relationship(back_populates="seen")
    external_paper: Mapped[ExternalPaper] = relationship(
        back_populates="tracking_seen"
    )


class TrackingRun(Base):
    __tablename__ = "tracking_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tracking_id: Mapped[int] = mapped_column(
        ForeignKey("tracking_searches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="running",
        server_default="running",
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_results: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    new_result_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    tracking: Mapped[TrackingSearch] = relationship(back_populates="runs")
    items: Mapped[list["TrackingRunItem"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="TrackingRunItem.position",
    )


class TrackingRunItem(Base):
    __tablename__ = "tracking_run_items"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "external_paper_id",
            name="uq_tracking_run_paper",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("tracking_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_paper_id: Mapped[int] = mapped_column(
        ForeignKey("external_papers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_new: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    run: Mapped[TrackingRun] = relationship(back_populates="items")
    external_paper: Mapped[ExternalPaper] = relationship()
