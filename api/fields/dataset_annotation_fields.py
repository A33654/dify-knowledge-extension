from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from fields.base import ResponseModel
from libs.helper import to_timestamp

FeedbackType = Literal["like", "dislike"]


class DatasetAnnotationResponse(ResponseModel):
    id: str
    question: str
    answer: str
    tags: list[str] = Field(default_factory=list)
    feedback_type: FeedbackType | None = None
    created_by: str
    created_at: int | None = None
    updated_at: int | None = None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _normalize_timestamps(cls, value: datetime | int | None) -> int | None:
        return to_timestamp(value)


class DatasetAnnotationListResponse(ResponseModel):
    data: list[DatasetAnnotationResponse]
    has_more: bool
    limit: int
    total: int
    page: int
