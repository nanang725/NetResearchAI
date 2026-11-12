from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
