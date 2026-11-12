from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiError(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    data: T | None = None
    error: ApiError | None = None


class HealthData(BaseModel):
    name: str
    status: str = "ok"
    env: str
    version: str = "0.1.0"
    features: dict[str, str] = Field(default_factory=dict)
