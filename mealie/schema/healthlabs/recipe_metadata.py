"""Additive recipe metadata and isolated, validated inference evidence.

The provider response is a missing-input plan. It cannot contain recipe totals,
source nutrient assertions or arbitrary provider configuration.
"""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from mealie.schema._mealie import MealieModel

NutrientKey = Literal["kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "water_ml"]
EstimateMethod = Literal["assumed_quantity", "generic_food", "source_substitution", "model_estimate"]
NutritionQuality = Literal["source_backed", "estimated", "unavailable"]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
MAX_ESTIMATED_GRAMS = 1_000_000
MIN_ESTIMATED_SERVINGS = 0.1
MAX_ESTIMATED_SERVINGS = 10_000


class MetadataModel(MealieModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NutritionProvenance(MetadataModel):
    id: str
    ingredient_reference: str | None = None
    kind: Literal["source", "assumption", "model_estimate"]
    source_food_id: str | None = None
    source_release_id: str | None = None
    source_url: str | None = None
    assumption: str | None = None
    provider: str | None = None
    model: str | None = None


class NutritionMetadataField(MetadataModel):
    verified: Nonnegative | None = None
    estimated: Nonnegative | None = None
    value: Nonnegative | None = None
    quality: NutritionQuality = "unavailable"
    estimate_methods: list[EstimateMethod] = Field(default_factory=list)
    provenance_ids: list[str] = Field(default_factory=list)
    missing_verified_ingredient_refs: list[str] = Field(default_factory=list)


class NutritionMetadataFields(MetadataModel):
    kcal: NutritionMetadataField = Field(default_factory=NutritionMetadataField)
    protein_g: NutritionMetadataField = Field(default_factory=NutritionMetadataField)
    carbs_g: NutritionMetadataField = Field(default_factory=NutritionMetadataField)
    fat_g: NutritionMetadataField = Field(default_factory=NutritionMetadataField)
    fibre_g: NutritionMetadataField = Field(default_factory=NutritionMetadataField)
    water_ml: NutritionMetadataField = Field(default_factory=NutritionMetadataField)


class RecipeNutritionMetadata(MetadataModel):
    schema_version: Literal[1] = 1
    basis: Literal["per_serving"] = "per_serving"
    status: Literal["source_backed", "estimated", "partial", "unavailable"]
    complete: bool
    native_complete: bool
    effective_servings: Positive | None = None
    servings_quality: Literal["recorded", "estimated", "unavailable"]
    per_serving: NutritionMetadataFields
    provenance: list[NutritionProvenance] = Field(default_factory=list)
    input_fingerprint: str
    calculated_at: datetime


class MetadataField(MetadataModel):
    recorded: Nonnegative | None = None
    estimated: Nonnegative | None = None
    value: Nonnegative | None = None
    quality: Literal["recorded", "derived", "estimated", "unavailable"] = "unavailable"
    provenance_ids: list[str] = Field(default_factory=list)


class MetadataEffortField(MetadataField):
    recorded: Literal[1, 2] | None = None
    estimated: Literal[1, 2] | None = None
    value: Literal[1, 2] | None = None


class MetadataSource(MetadataModel):
    ownership: Literal["ephra", "own"]
    url: str | None = None
    attribution_status: Literal["recorded", "not_recorded"]


class MetadataPlantQuantity(MetadataModel):
    amount: Positive | None = None
    unit: str = Field(default="", max_length=80)
    ingredient_name: str = Field(min_length=1, max_length=1000)
    ingredient_position: int = Field(ge=0, le=1000)
    kind: Literal["direct", "shared", "product"] = "direct"


class MetadataPlant(MetadataModel):
    plant_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    name: str = Field(min_length=1, max_length=120)
    quantities: list[MetadataPlantQuantity] = Field(default_factory=list, max_length=100)


class MetadataPlants(MetadataModel):
    recorded: list[MetadataPlant] = Field(default_factory=list)
    estimated: list[MetadataPlant] = Field(default_factory=list)
    status: Literal["complete", "partial", "unavailable"] = "unavailable"
    provenance_ids: list[str] = Field(default_factory=list)


class RecipeGeneralMetadata(MetadataModel):
    servings: MetadataField = Field(default_factory=MetadataField)
    total_minutes: MetadataField = Field(default_factory=MetadataField)
    prep_minutes: MetadataField = Field(default_factory=MetadataField)
    active_minutes: MetadataField = Field(default_factory=MetadataField)
    effort: MetadataEffortField = Field(default_factory=MetadataEffortField)
    source: MetadataSource
    plants: MetadataPlants = Field(default_factory=MetadataPlants)
    provenance: list[NutritionProvenance] = Field(default_factory=list)


class MetadataGeneralPlan(MetadataModel):
    total_minutes: Nonnegative | None = None
    prep_minutes: Nonnegative | None = None
    active_minutes: Nonnegative | None = None
    effort: Literal[1, 2] | None = None
    rationales: dict[Literal["total_minutes", "prep_minutes", "active_minutes", "effort"], str] = Field(
        default_factory=dict
    )
    plants: list[MetadataPlant] = Field(default_factory=list, max_length=100)
    plant_ingredient_references: list[UUID] = Field(default_factory=list, max_length=1000)
    plants_rationale: str | None = Field(default=None, max_length=2000)


class MetadataIngredientResponse(MetadataModel):
    reference_id: UUID
    effective_grams: Nonnegative
    quantity_assumption: str | None = Field(default=None, max_length=2000)
    source_food_id: UUID | None = None
    portion_or_density_id: UUID | None = None
    assumed_portion_amount: Positive | None = None
    nutrients_per_100g: dict[NutrientKey, Nonnegative | None] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=2000)


class RecipeMetadataProviderResponse(MetadataModel):
    effective_servings: Positive
    servings_assumption: str | None = Field(default=None, max_length=2000)
    ingredients: list[MetadataIngredientResponse] = Field(max_length=1000)
    general_metadata: MetadataGeneralPlan


class MetadataIngredientPlan(MetadataIngredientResponse):
    grams_quality: Literal["recorded", "source_conversion", "estimated"]
    nutrient_quality: dict[NutrientKey, NutritionQuality]
    source_release_id: UUID | None = None
    source_record_hash: str | None = None
    methods: list[EstimateMethod] = Field(default_factory=list)
    interpretation: str | None = None
    accepted_source_food_id: UUID | None = None
    accepted_plant_id: str | None = None
    accepted_plant_classified: bool | None = None
    accepted_food_group: str | None = None
    accepted_interpretation: str | None = None
    model_nutrient_keys: list[NutrientKey] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounded_estimated_mass(self) -> Self:
        if self.grams_quality == "estimated":
            if not (self.quantity_assumption or "").strip():
                raise ValueError("estimated mass requires an explicit quantity assumption")
            if self.effective_grams > MAX_ESTIMATED_GRAMS:
                raise ValueError("estimated mass exceeds 1000 kg per ingredient")
        return self


class MetadataModelReview(MetadataModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=255)


class MetadataValidation(MetadataModel):
    group_id: UUID
    recipe_id: UUID
    authoring_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str


class MetadataReuseOrigin(MetadataModel):
    estimate_id: UUID
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    inferred_at: datetime


class HealthLabsRecipeMetadataEstimatePayload(MetadataModel):
    effective_servings: Positive
    servings_assumption: str | None = None
    ingredients: list[MetadataIngredientPlan]
    general_metadata: MetadataGeneralPlan
    model_review: MetadataModelReview
    validation: MetadataValidation
    reuse_origin: MetadataReuseOrigin | None = None

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        refs = [row.reference_id for row in self.ingredients]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate ingredient references")
        if self.servings_assumption and not MIN_ESTIMATED_SERVINGS <= self.effective_servings <= MAX_ESTIMATED_SERVINGS:
            raise ValueError("estimated servings must be between 0.1 and 10000")
        return self
