from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import UUID4, Field, field_validator

from mealie.schema._mealie import MealieModel


class HealthLabsRecipeScope(StrEnum):
    PERSONAL = "personal"
    CATALOGUE = "catalogue"


class HealthLabsFoodIn(MealieModel):
    id: UUID4 | None = None
    name: str = Field(min_length=1)
    plural_name: str | None = None
    description: str = ""


class HealthLabsFoodOut(HealthLabsFoodIn):
    id: UUID4


class HealthLabsUnitIn(MealieModel):
    id: UUID4 | None = None
    name: str = Field(min_length=1)
    plural_name: str | None = None
    abbreviation: str = ""
    plural_abbreviation: str = ""


class HealthLabsUnitOut(HealthLabsUnitIn):
    id: UUID4


class HealthLabsIngredientIn(MealieModel):
    reference_id: UUID = Field(default_factory=uuid4)
    quantity: float | None = None
    food: HealthLabsFoodIn | None = None
    unit: HealthLabsUnitIn | None = None
    note: str | None = ""
    original_text: str | None = None
    title: str | None = None


class HealthLabsIngredientOut(MealieModel):
    reference_id: UUID
    quantity: float | None = None
    food: HealthLabsFoodOut | None = None
    unit: HealthLabsUnitOut | None = None
    note: str | None = ""
    original_text: str | None = None
    title: str | None = None


class HealthLabsIngredientConfidence(MealieModel):
    average: float | None = None
    quantity: float | None = None
    unit: float | None = None
    food: float | None = None
    comment: float | None = None


class HealthLabsDraftIngredient(HealthLabsIngredientIn):
    confidence: HealthLabsIngredientConfidence = HealthLabsIngredientConfidence()


class HealthLabsInstruction(MealieModel):
    title: str | None = ""
    summary: str | None = ""
    text: str
    ingredient_references: list[UUID] = []


class HealthLabsRecipeWrite(MealieModel):
    name: str = Field(min_length=1)
    description: str | None = ""
    scope: HealthLabsRecipeScope | None = None
    servings: float = Field(default=0, ge=0)
    yield_quantity: float = Field(default=0, ge=0)
    yield_name: str | None = None
    total_time_seconds: int | None = Field(default=None, ge=0)
    prep_time_seconds: int | None = Field(default=None, ge=0)
    perform_time_seconds: int | None = Field(default=None, ge=0)
    source_url: str | None = Field(default=None, max_length=2048)
    ingredients: list[HealthLabsIngredientIn] = []
    instructions: list[HealthLabsInstruction] = []
    authoring: dict[str, bool] = {}

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("sourceUrl must be an HTTP(S) URL without credentials")
        return value


class HealthLabsRecipeOut(MealieModel):
    id: UUID4
    slug: str
    name: str
    description: str | None = ""
    scope: HealthLabsRecipeScope
    revision_id: UUID4 | None = None
    revision_number: int = Field(ge=0)
    copied_from_recipe_id: UUID4 | None = None
    copied_from_revision_id: UUID4 | None = None
    copied_at: datetime | None = None
    servings: float = 0
    yield_quantity: float = 0
    yield_name: str | None = None
    total_time_seconds: int | None = None
    prep_time_seconds: int | None = None
    perform_time_seconds: int | None = None
    source_url: str | None = None
    image: str | None = None
    ingredients: list[HealthLabsIngredientOut] = []
    instructions: list[HealthLabsInstruction] = []
    authoring: dict[str, bool] = {}


class HealthLabsRecipePage(MealieModel):
    items: list[HealthLabsRecipeOut]
    next_cursor: str | None = None


class HealthLabsRecipeRevisionOut(MealieModel):
    id: UUID4
    recipe_id: UUID4
    revision_number: int
    content_sha256: str
    created_at: datetime
    recipe: HealthLabsRecipeOut


class HealthLabsOperationOut(MealieModel):
    idempotency_key: str
    state: str
    request_sha256: str
    response_status: int | None = None
    response_body: dict[str, Any] | None = None
    completed_at: datetime | None = None
    expires_at: datetime


class HealthLabsImportDraftRequest(MealieModel):
    url: str = Field(min_length=1, max_length=2048)


class HealthLabsImportRecipeDraft(HealthLabsRecipeWrite):
    ingredients: list[HealthLabsDraftIngredient] = []


class HealthLabsImportDraftOut(MealieModel):
    id: UUID4
    state: str
    recipe: HealthLabsImportRecipeDraft
    created_at: datetime
    expires_at: datetime
