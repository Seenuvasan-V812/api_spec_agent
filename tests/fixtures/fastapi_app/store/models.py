"""Domain models for the pet store."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PetStatus(str, Enum):
    """Availability state of a pet."""

    AVAILABLE = "available"
    PENDING = "pending"
    SOLD = "sold"


class Category(BaseModel):
    id: int
    name: str = Field(..., min_length=1, max_length=50)


class Pet(BaseModel):
    """A pet in the store.

    Attributes:
        name: Display name of the pet.
    """

    id: int
    name: str
    status: PetStatus = PetStatus.AVAILABLE
    category: Optional[Category] = None
    tags: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class PetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    status: PetStatus = PetStatus.AVAILABLE
    category_id: Optional[int] = Field(default=None, ge=1)


class OrderItem(BaseModel):
    pet_id: int = Field(..., ge=1)
    quantity: int = Field(default=1, ge=1, le=10)


class Order(BaseModel):
    id: int
    items: list[OrderItem]
    total_cents: int
    placed_at: datetime


class ErrorMessage(BaseModel):
    detail: str
