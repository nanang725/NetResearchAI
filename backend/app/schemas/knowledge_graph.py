from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


KnowledgeNodeType = Literal[
    "paper",
    "author",
    "topic",
    "method",
    "dataset",
    "model",
    "finding",
    "institution",
]
KnowledgeRelationType = Literal[
    "CITES",
    "AUTHORED_BY",
    "STUDIES",
    "USES_METHOD",
    "USES_DATASET",
    "USES_MODEL",
    "SUPPORTS",
    "CONTRADICTS",
    "EXTENDS",
    "AFFILIATED_WITH",
]


class KnowledgeGraphRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[int] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )

    @field_validator("paper_ids")
    @classmethod
    def validate_paper_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if len(value) != len(set(value)):
            raise ValueError("paper_ids 不能重复。")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in value
        ):
            raise ValueError("paper_ids 只能包含正整数。")
        return value


class KnowledgeGraphEnvelope(BaseModel):
    success: Literal[True]
    data: dict[str, Any]
    error: None
