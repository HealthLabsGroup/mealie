from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import UUID4, Field, StringConstraints

from mealie.schema._mealie import MealieModel


class HealthLabsNutritionStatus(StrEnum):
    MATCHED = "matched"
    REVIEW_REQUIRED = "review_required"
    UNKNOWN = "unknown"


class HealthLabsNutritionWorkKind(StrEnum):
    INGREDIENT_STRUCTURE = "ingredient_structure"
    FOOD_MATCH = "food_match"
    PORTION_RESOLUTION = "portion_resolution"


class HealthLabsNutritionWorkState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    REVIEW_REQUIRED = "review_required"
    COMPLETED = "completed"
    FAILED = "failed"


class HealthLabsNutritionWorkerStatus(MealieModel):
    counts: dict[str, int]
    provider_enabled: bool
    provider_budget_exhausted: bool
    last_updated_at: datetime | None = None


class HealthLabsNutritionWorkOut(MealieModel):
    id: UUID4
    kind: HealthLabsNutritionWorkKind
    target_id: str
    state: HealthLabsNutritionWorkState
    attempt_count: int = Field(ge=0)
    result: dict[str, Any] | None = None
    last_error: str | None = None
    updated_at: datetime | None = None


class HealthLabsNutritionValues(MealieModel):
    kcal: float | None = Field(default=None, ge=0)
    protein_g: float | None = Field(default=None, ge=0)
    fibre_g: float | None = Field(default=None, ge=0)
    carbs_g: float | None = Field(default=None, ge=0)
    fat_g: float | None = Field(default=None, ge=0)
    water_ml: float | None = Field(default=None, ge=0)


class HealthLabsNutritionSourceOut(MealieModel):
    provider: str
    version: str
    country: str
    source_url: str
    license_url: str
    attribution: str


class HealthLabsFoodPortionOut(MealieModel):
    description: str
    amount: float
    unit: str
    grams: float


class HealthLabsFoodPortionCreate(MealieModel):
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    amount: float = Field(gt=0)
    unit: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    grams: float = Field(gt=0)


class HealthLabsFoodMatchWrite(MealieModel):
    nutrient_food_id: UUID4
    country: Annotated[
        str,
        StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=5),
    ]
    confidence: float = Field(default=1, ge=0, le=1)
    food_group: str | None = Field(default=None, max_length=32)
    plant_id: str | None = Field(default=None, max_length=100)
    allergens: dict[str, str] | None = None
    diets: dict[str, str] | None = None


class HealthLabsFoodMatchOut(MealieModel):
    id: UUID4 | None = None
    food_id: UUID4
    nutrient_food_id: UUID4
    source_food_id: str
    name: str
    preparation_state: str | None = None
    status: HealthLabsNutritionStatus
    country: str
    match_method: str
    confidence: float = Field(ge=0, le=1)
    reviewed: bool
    food_group: str | None = None
    plant_id: str | None = None
    allergens: dict[str, str] | None = None
    diets: dict[str, str] | None = None
    reviewed_by_user_id: UUID4 | None = None
    reviewed_at: datetime | None = None
    nutrients_per_100g: HealthLabsNutritionValues
    portions: list[HealthLabsFoodPortionOut] = Field(default_factory=list)
    source: HealthLabsNutritionSourceOut


class HealthLabsFoodNutritionOut(MealieModel):
    id: UUID4
    name: str | None = None
    plural_name: str | None = None
    description: str | None = None
    status: HealthLabsNutritionStatus
    nutrition: HealthLabsFoodMatchOut | None = None


class HealthLabsFoodNutritionPage(MealieModel):
    items: list[HealthLabsFoodNutritionOut]
    total: int = Field(ge=0)


class HealthLabsNutritionSourceReleaseOut(HealthLabsNutritionSourceOut):
    id: UUID4
    sha256: str
    imported_at: datetime


class HealthLabsNutrientFoodOut(MealieModel):
    id: UUID4
    source_food_id: str
    name: str
    preparation_state: str | None = None
    nutrients_per_100g: HealthLabsNutritionValues
    portions: list[HealthLabsFoodPortionOut] = Field(default_factory=list)
    source: HealthLabsNutritionSourceReleaseOut


class HealthLabsIngredientNutrition(MealieModel):
    status: HealthLabsNutritionStatus
    resolved_grams: float | None = Field(default=None, ge=0)
    nutrients: HealthLabsNutritionValues = Field(default_factory=HealthLabsNutritionValues)
    complete: bool = False
    missing_reason: str | None = None
    food_match: HealthLabsFoodMatchOut | None = None


class HealthLabsRecipeNutrition(MealieModel):
    totals: HealthLabsNutritionValues = Field(default_factory=HealthLabsNutritionValues)
    per_serving: HealthLabsNutritionValues = Field(default_factory=HealthLabsNutritionValues)
    complete: bool = False
    missing_ingredient_references: list[UUID] = Field(default_factory=list)
    source_versions: list[HealthLabsNutritionSourceOut] = Field(default_factory=list)
