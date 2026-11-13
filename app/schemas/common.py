from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Generic, TypeVar

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(from_attributes=True)
    items: list[T]
    total: int
    page: int
    per_page: int
    pages: int


class MessageResponse(BaseModel):
    message: str
