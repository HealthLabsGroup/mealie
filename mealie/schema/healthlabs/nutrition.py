from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import UUID4, Field

from mealie.schema._mealie import MealieModel


class HealthLabsNutritionStatus(StrEnum):
    MATCHED = "matched"
    REVIEW_REQUIRED = "review_required"
    UNKNOWN = "unknown"


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
