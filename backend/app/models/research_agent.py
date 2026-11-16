from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ResearchAgentRun(Base):
    __tablename__ = "research_agent_runs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    research_question: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )
    time_range: Mapped[str | None] = mapped_column(String(255), nullable=True)
    analysis_dimensions_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        index=True,
    )
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    replan_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    max_nodes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=8,
        server_default="8",
    )
    max_tool_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=12,
        server_default="12",
    )
    max_llm_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=4,
        server_default="4",
    )
    max_arxiv_requests: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        server_default="3",
    )
    max_execution_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=180,
        server_default="180",
    )
    tool_calls_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    llm_calls_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    arxiv_requests_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    nodes: Mapped[list[ResearchAgentNode]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ResearchAgentNode.created_at, ResearchAgentNode.node_key",
    )
    tool_calls: Mapped[list[ResearchAgentToolCall]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ResearchAgentToolCall.started_at, ResearchAgentToolCall.id",
    )
    evidence: Mapped[list[ResearchAgentEvidence]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ResearchAgentEvidence.created_at, ResearchAgentEvidence.id",
    )
    artifacts: Mapped[list[ResearchAgentArtifact]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ResearchAgentArtifact.created_at",
    )


class ResearchAgentNode(Base):
    __tablename__ = "research_agent_nodes"
    __table_args__ = (
        UniqueConstraint("run_id", "node_key", name="uq_research_agent_node_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_key: Mapped[str] = mapped_column(String(80), nullable=False)
    node_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="tool",
    )
    dependencies_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )
    tool_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    completion_condition: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        index=True,
    )
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    retry_limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    run: Mapped[ResearchAgentRun] = relationship(back_populates="nodes")


class ResearchAgentToolCall(Base):
    __tablename__ = "research_agent_tool_calls"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("research_agent_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    arguments_summary_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="{}",
    )
    result_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="running",
        index=True,
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped[ResearchAgentRun] = relationship(back_populates="tool_calls")
    node: Mapped[ResearchAgentNode] = relationship()


class ResearchAgentEvidence(Base):
    __tablename__ = "research_agent_evidence"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("research_agent_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_call_id: Mapped[str] = mapped_column(
        ForeignKey("research_agent_tool_calls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    paper_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_valid: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        index=True,
    )
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    run: Mapped[ResearchAgentRun] = relationship(back_populates="evidence")
    node: Mapped[ResearchAgentNode] = relationship()
    tool_call: Mapped[ResearchAgentToolCall] = relationship()


class ResearchAgentArtifact(Base):
    __tablename__ = "research_agent_artifacts"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True,
    )
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    valid_source_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    run: Mapped[ResearchAgentRun] = relationship(back_populates="artifacts")
