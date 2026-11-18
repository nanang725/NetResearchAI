from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


DiscoveryField = Literal["all", "title", "abstract", "author"]
DiscoverySortBy = Literal["relevance", "last_updated", "submitted"]
DiscoverySortOrder = Literal["ascending", "descending"]


class FavoriteExternalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arxiv_id: str = Field(min_length=4, max_length=100)


class TrackingSearchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=2, max_length=300)
    field: DiscoveryField = "all"
    sort_by: DiscoverySortBy = "submitted"
    sort_order: DiscoverySortOrder = "descending"
    page_size: int = Field(default=20, ge=1, le=50)


class DailyInterestsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interests: list[str] = Field(min_length=1, max_length=8)


class DailyReadStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_read: bool


class DiscoveryEnvelope(BaseModel):
    success: Literal[True]
    data: dict[str, Any]
    error: None


class DiscoveryListEnvelope(BaseModel):
    success: Literal[True]
    data: list[dict[str, Any]]
    error: None
