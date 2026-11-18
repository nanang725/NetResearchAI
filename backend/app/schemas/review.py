from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LiteratureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[int] = Field(min_length=2, max_length=5)
    topic: str = Field(min_length=3, max_length=200)
    include_future_work: bool = True


class ReviewEvidenceCitation(BaseModel):
    paper_id: int
    title: str
    page_number: int
    chunk_id: int
    evidence: str


class ReviewSection(BaseModel):
    heading: str
    content: str
    citations: list[ReviewEvidenceCitation]


class ReviewCoverage(BaseModel):
    paper_id: int
    title: str
    evidence_count: int
    status: Literal["covered", "insufficient_evidence"]


class ReviewComparisonFinding(BaseModel):
    paper_id: int
    title: str
    summary: str
    claim_kind: Literal["evidence", "inference", "insufficient_evidence"]
    evidence_status: Literal["covered", "insufficient_evidence"]
    citations: list[ReviewEvidenceCitation]


class ReviewDimensionComparison(BaseModel):
    dimension: Literal["methods", "results", "conclusions", "limitations"]
    label: str
    items: list[ReviewComparisonFinding]


class ReviewFutureWorkItem(BaseModel):
    paper_id: int
    title: str
    page_number: int
    chunk_id: int
    kind: Literal["explicit", "inferred"]
    original_evidence: str
    summary: str


class LiteratureReviewData(BaseModel):
    title: str
    topic: str
    selected_paper_ids: list[int]
    sections: list[ReviewSection]
    paper_coverage: list[ReviewCoverage]
    dimension_comparison: list[ReviewDimensionComparison]
    future_work: list[ReviewFutureWorkItem]
    sources: list[ReviewEvidenceCitation]
    model: str
    disclaimer: str


class LiteratureReviewResponse(BaseModel):
    success: Literal[True]
    data: LiteratureReviewData
    error: None


class LiteratureReviewSessionEnvelope(BaseModel):
    success: Literal[True]
    data: dict[str, Any] | None
    error: None
