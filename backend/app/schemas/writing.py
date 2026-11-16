from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


TranslationDirection = Literal["zh_to_en", "en_to_zh"]


class AcademicOutlineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: int = Field(gt=0)
    topic: str = Field(min_length=0, max_length=200)


class AcademicReferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[int] = Field(default_factory=list, max_length=100)


class AcademicTranslationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=12_000)
    direction: TranslationDirection


class AcademicWritingEnvelope(BaseModel):
    success: Literal[True]
    data: dict[str, Any]
    error: None


class CsvFieldProfile(BaseModel):
    name: str
    type: Literal["integer", "number", "boolean", "date", "text"]
    count: int
    missing: int
    missing_rate: float
    unique: int
    statistics: dict[str, float | None]
    top_values: list[dict[str, Any]]


class CsvChart(BaseModel):
    chart_type: Literal["bar"]
    title: str
    series_name: str
    categories: list[str]
    values: list[float | int]
    aggregation: str
    category_column: str
    value_column: str | None
    caption: str
    analysis: str
    data_origin: Literal["uploaded_csv_aggregation"]
    data_sha256: str


class CsvAnalysisData(BaseModel):
    filename: str
    row_count: int
    column_count: int
    missing_cell_count: int
    fields: list[dict[str, Any]]
    profiles: list[CsvFieldProfile]
    chart: CsvChart
    content_labels: dict[str, str]
    disclaimer: str


class CsvAnalysisResponse(BaseModel):
    success: Literal[True]
    data: CsvAnalysisData
    error: None
