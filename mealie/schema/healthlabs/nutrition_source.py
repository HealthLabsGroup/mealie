from __future__ import annotations

import math
from typing import Any

from pydantic import UUID4, Field, field_validator

from mealie.schema._mealie import MealieModel


class NutrientValue(MealieModel):
    amount: float | None = None
    unit: str = Field(min_length=1, max_length=32)
    source_name: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_must_be_finite_and_non_negative(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("nutrient amount must be finite and non-negative")
        return value


class SourcePortion(MealieModel):
    description: str = Field(min_length=1, max_length=255)
    amount: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=64)
    grams: float = Field(gt=0)


class SourceManifest(MealieModel):
    provider: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=100)
    country: str = Field(min_length=2, max_length=5)
    source_url: str = Field(min_length=1, max_length=2048)
    license_url: str = Field(min_length=1, max_length=2048)
    attribution: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceFoodRow(MealieModel):
    source_food_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=500)
    preparation_state: str | None = Field(default=None, max_length=100)
    nutrients_per_100g: dict[str, NutrientValue]
    raw_nutrients: dict[str, Any] = Field(default_factory=dict)
    portions: list[SourcePortion] = Field(default_factory=list)


class NutritionImportResult(MealieModel):
    source_release_id: UUID4
    created_release: bool
    foods_imported: int
    portions_imported: int
