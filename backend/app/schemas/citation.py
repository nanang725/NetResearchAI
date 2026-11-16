from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CitationNode(BaseModel):
    id: str
    paper_id: int
    title: str
    year: int | None
    authors: list[str]
    in_degree: int
    out_degree: int
    pagerank: float
    rank: int
    is_foundational: bool
    foundational_reasons: list[str]


class CitationGraphEdge(BaseModel):
    id: int
    source: str
    target: str
    reference_id: int
    raw_text: str
    source_page: int
    match_method: str
    match_confidence: float


class CitationGraphStats(BaseModel):
    paper_count: int
    displayed_node_count: int
    displayed_edge_count: int
    independent_paper_count: int
    reference_count: int
    matched_reference_count: int
    unmatched_reference_count: int
    ambiguous_reference_count: int
    edge_count: int
    foundational_paper_count: int


class CitationGraphData(BaseModel):
    nodes: list[CitationNode]
    edges: list[CitationGraphEdge]
    stats: CitationGraphStats
    disclaimer: str


class CitationGraphResponse(BaseModel):
    success: Literal[True]
    data: CitationGraphData
    error: None


class CitationRelation(BaseModel):
    paper_id: int
    title: str
    year: int | None
    reference_id: int
    raw_reference: str
    source_page: int
    match_method: str
    match_confidence: float


class CitationPaperDetailData(BaseModel):
    paper_id: int
    title: str
    year: int | None
    in_degree: int
    out_degree: int
    pagerank: float
    rank: int
    is_foundational: bool
    foundational_reasons: list[str]
    outgoing: list[CitationRelation]
    incoming: list[CitationRelation]


class CitationPaperDetailResponse(BaseModel):
    success: Literal[True]
    data: CitationPaperDetailData
    error: None


class CitationRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[int] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )


class CitationRebuildData(BaseModel):
    rebuilt_paper_ids: list[int]
    reference_count: int
    matched_reference_count: int
    ambiguous_reference_count: int
    unmatched_reference_count: int
    edge_count: int


class CitationRebuildResponse(BaseModel):
    success: Literal[True]
    data: CitationRebuildData
    error: None
