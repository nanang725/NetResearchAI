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
    page_chunk_index: int = 0
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
    doi: str | None = None
    publication_year: int | None = None
    venue: str | None = None
    parse_status: str
    index_status: str
    created_at: datetime
    updated_at: datetime


class PaperElementOut(BaseModel):
    id: int
    element_index: int
    element_type: str
    text: str
    data: dict[str, Any] | list[Any] | None = None
    bbox: list[float] | None = None
    section_path: str | None = None
    extraction_status: str
    confidence: float | None = None


class PaperPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_number: int
    text: str
    char_count: int
    parse_status: str = "text_extracted"
    ocr_executed: bool = False
    ocr_error: str | None = None
    checkpoint_status: str = "pending"
    checkpoint_error: str | None = None
    checkpoint_generation: int = 0
    elements: list[PaperElementOut] = Field(default_factory=list)


class PaperDetail(PaperListItem):
    content_type: str
    stored_filename: str
    pdf_metadata: dict | None = None
    pages: list[PaperPageOut] = Field(default_factory=list)


class PaperTranslationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(gt=0)
    element_id: int | None = Field(default=None, gt=0)
    direction: Literal["zh_to_en", "en_to_zh"]


class PaperTranslationData(BaseModel):
    paper_id: int
    page_number: int
    element_id: int | None
    scope: Literal["page", "element"]
    direction: Literal["zh_to_en", "en_to_zh"]
    source_language: str
    target_language: str
    original_text: str
    translated_text: str
    protected_token_count: int
    model: str
    disclaimer: str


class PaperTranslationResponse(BaseModel):
    success: Literal[True]
    data: PaperTranslationData
    error: None


class PaperUploadResult(PaperDetail):
    pass


class PaperImportItemOut(BaseModel):
    id: str
    original_filename: str
    paper_id: int | None
    status: str
    parse_status: str
    index_status: str
    current_stage: str
    progress: int
    attempts: int
    error_message: str | None
    file_size: int
    can_retry: bool
    created_at: datetime | None
    updated_at: datetime | None


class PaperImportJobOut(BaseModel):
    id: str
    kind: Literal["import", "index_rebuild"]
    status: Literal["queued", "running", "partial", "failed", "completed"]
    total_count: int
    completed_count: int
    failed_count: int
    queued_count: int
    running_count: int
    progress: int
    created_at: datetime | None
    updated_at: datetime | None
    items: list[PaperImportItemOut]


class PaperImportJobResponse(BaseModel):
    success: Literal[True]
    data: PaperImportJobOut
    error: None


class PaperImportJobListResponse(BaseModel):
    success: Literal[True]
    data: list[PaperImportJobOut]
    error: None


class PaperVersionCreate(BaseModel):
    based_on_id: int = Field(gt=0)
    source: str = Field(default="manual", min_length=1, max_length=128)
