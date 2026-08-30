"""Shared schema primitives."""

from __future__ import annotations

from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

ItemT = TypeVar("ItemT")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    message: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None


class Paginated(BaseModel, Generic[ItemT]):
    items: List[ItemT]
    total: int
    page: int
    limit: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // max(1, self.limit)))
