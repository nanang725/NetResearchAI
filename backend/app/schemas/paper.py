from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class AgentChatContext(BaseModel):
    chunk_id: int
    paper_id: int
    paper_title: str
    page_number: int
    text: str
    retrieval_method: Literal["hybrid", "bm25", "vector"]
    retrieval_score: float
    retrieval_sources: list[Literal["bm25", "vector"]]
    bm25_rank: int | None
    vector_rank: int | None
    bm25_score: float | None
    vector_score: float | None


class AgentChatData(BaseModel):
    answer: str
    steps: list[str]
    contexts: list[AgentChatContext]
    is_relevant: bool
    intent: Literal[
        "paper_qa",
        "simple_explanation",
        "comparative_analysis",
        "research_assistance",
        "academic_writing",
    ]
    knowledge_source: Literal["paper_evidence", "generic_knowledge"]


class AgentChatResponse(BaseModel):
    success: Literal[True]
    data: AgentChatData
    error: None


class ValidationErrorDetail(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    loc: list[str | int]
    msg: str
    input: Any
    ctx: dict[str, Any] | None = None
    url: str | None = None


class AgentChatValidationError(BaseModel):
    code: Literal["VALIDATION_ERROR"]
    message: str
    details: list[ValidationErrorDetail]


class AgentChatValidationErrorResponse(BaseModel):
    success: Literal[False]
    data: None
    error: AgentChatValidationError


class PaperListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    original_filename: str
    file_size: int
    page_count: int
    char_count: int
    authors: str | None = None
    parse_status: str
    index_status: str
    created_at: datetime
    updated_at: datetime


class PaperPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_number: int
    text: str
    char_count: int


class PaperDetail(PaperListItem):
    content_type: str
    stored_filename: str
    pdf_metadata: dict | None = None
    pages: list[PaperPageOut] = Field(default_factory=list)


class PaperUploadResult(PaperDetail):
    pass
