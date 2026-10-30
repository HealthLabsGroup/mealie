"""Read-only metadata projection; inference plans never become source facts.

``inputs`` captures current dependencies. ``validate_plan`` is the only boundary
for untrusted provider output. ``project_metadata`` is pure and always recomputes
totals, including when evidence was validated against older source facts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from math import isfinite, ulp
from typing import Any, TypeGuard
from uuid import UUID

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import (
    HealthLabsFoodDensity,
    HealthLabsFoodMatch,
    HealthLabsFoodMatchSuppression,
    HealthLabsFoodPortion,
    HealthLabsFoodPortionMapping,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsRecipeMetadataEstimate,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel
from mealie.schema.healthlabs.recipe_metadata import (
    MAX_ESTIMATED_GRAMS,
    HealthLabsRecipeMetadataEstimatePayload,
    MetadataEffortField,
    MetadataField,
    MetadataGeneralPlan,
    MetadataIngredientPlan,
    MetadataIngredientResponse,
    MetadataModelReview,
    MetadataPlants,
    MetadataRecordedPlant,
    MetadataReuseOrigin,
    MetadataSource,
    MetadataValidation,
    NutrientKey,
    NutritionMetadataField,
    NutritionMetadataFields,
    NutritionProvenance,
    RecipeGeneralMetadata,
    RecipeMetadataProviderResponse,
    RecipeNutritionMetadata,
)
from mealie.schema.recipe import Recipe
from mealie.services.healthlabs.nutrition_service import MASS_GRAMS, VOLUME_MILLILITRES, NutritionService

POLICY_VERSION = "recipe-metadata-v1"
REQUIRED_NUTRIENTS: tuple[NutrientKey, ...] = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g")
NUTRIENTS: tuple[NutrientKey, ...] = (*REQUIRED_NUTRIENTS, "water_ml")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()
    ).hexdigest()


def valid_number(value: Any, *, positive: bool = False) -> TypeGuard[int | float | Decimal]:
    return (
        isinstance(value, (int, float, Decimal))
        and not isinstance(value, bool)
        and isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


@dataclass
class MetadataPortionCandidate:
    id: UUID
    description: str
    amount: float
    unit: str
    grams: float
    density_id: UUID | None = None
    density_grams_per_ml: float | None = None
    source_record_hash: str | None = None


@dataclass
class MetadataSourceCandidate:
    id: UUID
    release_id: UUID
    name: str
    interpretation: str
    nutrients_per_100g: dict[str, float | None]
    source_record_hash: str
    source_url: str
    preparation_state: str | None = None
    portions: list[MetadataPortionCandidate] = field(default_factory=list)


@dataclass
class IngredientMetadataInput:
    reference_id: UUID
    accepted_source_food_id: UUID | None = None
    accepted_source_release_id: UUID | None = None
    accepted_source_name: str | None = None
    accepted_preparation_state: str | None = None
    preparation_constraint: str | None = None
    interpretation: str | None = None
    resolved_grams: float | Decimal | None = None
    nutrients_per_100g: dict[str, float | None] = field(default_factory=dict)
    source_url: str | None = None
    source_record_hash: str | None = None
    candidates: list[MetadataSourceCandidate] = field(default_factory=list)
    plant_id: str | None = None
    plant_name: str | None = None
    plant_classified: bool = False
    accepted_food_group: str | None = None
    portions: list[MetadataPortionCandidate] = field(default_factory=list)
    quantity: float | None = None
    unit_names: tuple[str, ...] = ()
    input_millilitres: Decimal | None = None


@dataclass
class RecipeMetadataInputs:
    recipe: Recipe
    authoring_fingerprint: str
    input_fingerprint: str
    ingredients: list[IngredientMetadataInput]
    native_complete: bool
    authoring_context: dict[str, Any] = field(default_factory=dict)

    def missing_fields(self, payload: HealthLabsRecipeMetadataEstimatePayload | None = None) -> list[str]:
        nutrition, metadata = project_metadata(self, payload)
        missing: list[str] = [code for code in REQUIRED_NUTRIENTS if getattr(nutrition.per_serving, code).value is None]
        missing.extend(
            key
            for key in ("servings", "total_minutes", "prep_minutes", "active_minutes", "effort")
            if getattr(metadata, key).value is None
        )
        if metadata.plants.status != "complete":
            missing.append("plants")
        return missing


def _bounds(values: dict[str, float | None], model_keys: set[str]) -> None:
    for code, value in values.items():
        if value is not None and (not valid_number(value) or value > (1000 if code == "kcal" else 100)):
            raise ValueError(f"invalid per-100g dimension: {code}")
    protein, carbs, fat = (values.get(key) for key in ("protein_g", "carbs_g", "fat_g"))
    if protein is not None and carbs is not None and fat is not None:
        if protein + carbs + fat > 105:
            raise ValueError("per-100g macronutrients exceed mass")
        energy = values.get("kcal")
        # Carbohydrates may include zero-energy polyols. Immutable accepted or
        # candidate source facts are never rejected by this model-only heuristic.
        minimum = 4 * protein if model_keys.intersection({"kcal", "protein_g"}) else 0
        minimum += 9 * fat if model_keys.intersection({"kcal", "fat_g"}) else 0
        maximum = 4 * protein + 4 * carbs + 9 * fat
        energetic_estimate = bool(model_keys.intersection({"kcal", "protein_g", "carbs_g", "fat_g"}))
        # With fixed energy, the six columns may omit alcohol. Conservatively
        # allow 7 kcal/g for all unrepresented mass, plus the macro tolerance;
        # no food-name guess or change to the fixed source energy is involved.
        nonmacro_energy = 7 * max(0, 100 - protein - carbs - fat)
        upper_gap = max(720, maximum * 0.65) if "kcal" in model_keys else nonmacro_energy + max(150, maximum * 0.65)
        if energy is not None and (
            minimum - energy > max(150, minimum * 0.65) or (energetic_estimate and energy - maximum > upper_gap)
        ):
            raise ValueError("severe per-100g energy inconsistency")


def _preparation_key(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    return NutritionService._preparation_state(value) or IngredientFoodModel.normalize(value)


def _preparation_compatible(fact: IngredientMetadataInput, candidate: MetadataSourceCandidate) -> bool:
    required = _preparation_key(fact.accepted_preparation_state or fact.preparation_constraint)
    return required is None or _preparation_key(candidate.preparation_state) == required


def _preparation_from_text(text: str) -> str | None:
    return next(
        (
            state
            for state, pattern in (
                ("cooked", r"\b(cooked|boiled|roasted|baked)\b"),
                ("raw", r"\braw\b"),
                ("dried", r"\b(dried|dry)\b"),
            )
            if re.search(pattern, text.casefold())
        ),
        None,
    )


def _portion_grams(fact: IngredientMetadataInput, plan: MetadataIngredientResponse) -> Decimal:
    candidate = next((candidate for candidate in fact.candidates if candidate.id == plan.source_food_id), None)
    portions = candidate.portions if candidate else fact.portions
    portion = next(
        (portion for portion in portions if plan.portion_or_density_id in {portion.id, portion.density_id}), None
    )
    if portion is None:
        raise ValueError("unknown or ineligible portion/density ID")
    if plan.assumed_portion_amount is not None and not (plan.quantity_assumption or "").strip():
        raise ValueError("assumed portion amount requires rationale")
    if plan.portion_or_density_id == portion.density_id:
        ml = (
            Decimal(str(plan.assumed_portion_amount))
            if plan.assumed_portion_amount is not None
            else fact.input_millilitres
        )
        if ml is None or not valid_number(portion.density_grams_per_ml, positive=True):
            raise ValueError("density requires recorded volume or an explicit assumed ml amount")
        return ml * Decimal(str(portion.density_grams_per_ml))
    quantity = plan.assumed_portion_amount
    if quantity is None and any(
        IngredientUnitModel.normalize(name)
        in {
            IngredientUnitModel.normalize(portion.unit),
            IngredientUnitModel.normalize(portion.description),
        }
        for name in fact.unit_names
    ):
        quantity = fact.quantity
    if quantity is None or not valid_number(quantity):
        raise ValueError("portion requires compatible recorded units or an explicit assumed portion amount")
    return Decimal(str(quantity)) * Decimal(str(portion.grams)) / Decimal(str(portion.amount))


def _validated_mass(fact: IngredientMetadataInput, row: MetadataIngredientResponse) -> Decimal:
    effective = Decimal(str(row.effective_grams))
    if valid_number(fact.resolved_grams):
        canonical = float(fact.resolved_grams)
        # A JSON float echo gets four ULPs of representational tolerance; the
        # current full-precision Decimal remains the calculation authority.
        if abs(row.effective_grams - canonical) > 4 * ulp(canonical) or row.quantity_assumption:
            raise ValueError("source-backed mass cannot be changed")
        effective = Decimal(str(fact.resolved_grams))
    elif not (row.quantity_assumption or "").strip():
        raise ValueError("assumed mass requires rationale")
    if row.portion_or_density_id is not None:
        converted = _portion_grams(fact, row)
        if abs(converted - Decimal(str(row.effective_grams))) > Decimal("0.001"):
            raise ValueError("selected source portion/density mass cannot be changed")
        if fact.resolved_grams is None:
            effective = converted
    elif row.assumed_portion_amount is not None:
        raise ValueError("assumed portion amount requires an eligible portion/density ID")
    return effective


def validate_plan(
    inputs: RecipeMetadataInputs,
    untrusted_response: RecipeMetadataProviderResponse | dict,
    *,
    provider: str,
    model: str,
) -> HealthLabsRecipeMetadataEstimatePayload:
    response = RecipeMetadataProviderResponse.model_validate(untrusted_response)
    expected = {row.reference_id for row in inputs.ingredients}
    refs = [row.reference_id for row in response.ingredients]
    if not expected or len(refs) != len(set(refs)) or set(refs) != expected:
        raise ValueError("ingredient references must cover the recipe exactly once")
    recorded_servings = inputs.recipe.recipe_servings
    if valid_number(recorded_servings, positive=True):
        if response.effective_servings != recorded_servings or response.servings_assumption:
            raise ValueError("recorded servings cannot be changed")
    elif not (response.servings_assumption or "").strip():
        raise ValueError("assumed servings require rationale")
    current = {row.reference_id: row for row in inputs.ingredients}
    plans: list[MetadataIngredientPlan] = []
    for row in response.ingredients:
        fact = current[row.reference_id]
        effective_grams = _validated_mass(fact, row)
        candidate = next((candidate for candidate in fact.candidates if candidate.id == row.source_food_id), None)
        if row.source_food_id is not None and candidate is None:
            raise ValueError("unknown source candidate")
        if candidate is not None and not _preparation_compatible(fact, candidate):
            raise ValueError("source candidate conflicts with accepted preparation")
        values: dict[str, float | None] = {}
        quality = {}
        methods = []
        model_nutrient_keys = []
        if fact.resolved_grams is None:
            methods.append("assumed_quantity")
        if candidate:
            methods.append("source_substitution" if fact.accepted_source_food_id else "generic_food")
        for code in NUTRIENTS:
            accepted = fact.nutrients_per_100g.get(code)
            supplied = row.nutrients_per_100g.get(code)
            generic = candidate.nutrients_per_100g.get(code) if candidate else None
            if accepted is not None:
                if supplied is not None and supplied != accepted:
                    raise ValueError(f"fixed source nutrient cannot be changed: {code}")
                values[code], quality[code] = accepted, "source_backed"
            elif generic is not None:
                if supplied is not None and supplied != generic:
                    raise ValueError(f"candidate source nutrient cannot be changed: {code}")
                values[code], quality[code] = generic, "estimated"
            elif supplied is not None:
                values[code], quality[code] = supplied, "estimated"
                model_nutrient_keys.append(code)
                if "model_estimate" not in methods:
                    methods.append("model_estimate")
            else:
                values[code], quality[code] = None, "unavailable"
        if any(values[key] is None for key in REQUIRED_NUTRIENTS):
            raise ValueError("plan must supply all five required nutrients")
        _bounds(values, set(model_nutrient_keys))
        plans.append(
            MetadataIngredientPlan(
                **row.model_dump(exclude={"nutrients_per_100g", "effective_grams"}),
                effective_grams=float(effective_grams),
                nutrients_per_100g=values,
                grams_quality="source_conversion" if fact.resolved_grams is not None else "estimated",
                nutrient_quality=quality,
                source_release_id=candidate.release_id if candidate else fact.accepted_source_release_id,
                source_record_hash=candidate.source_record_hash if candidate else fact.source_record_hash,
                methods=methods,
                interpretation=candidate.interpretation if candidate else fact.interpretation,
                accepted_source_food_id=fact.accepted_source_food_id,
                accepted_plant_id=fact.plant_id,
                accepted_plant_classified=fact.plant_classified,
                accepted_food_group=fact.accepted_food_group,
                model_nutrient_keys=model_nutrient_keys,
                accepted_interpretation=fact.interpretation,
            )
        )
    _validate_general(inputs, response.general_metadata)
    payload = HealthLabsRecipeMetadataEstimatePayload(
        effective_servings=response.effective_servings,
        servings_assumption=response.servings_assumption,
        ingredients=plans,
        general_metadata=response.general_metadata,
        model_review=MetadataModelReview(provider=provider, model=model),
        validation=MetadataValidation(
            group_id=inputs.recipe.group_id,
            recipe_id=inputs.recipe.id,
            authoring_fingerprint=inputs.authoring_fingerprint,
            input_fingerprint=inputs.input_fingerprint,
            policy_version=POLICY_VERSION,
        ),
    )
    if not project_metadata(inputs, payload)[0].complete:
        raise ValueError("plan has unrepresentable per-serving nutrient totals")
    return payload


def _validate_general(inputs: RecipeMetadataInputs, general: MetadataGeneralPlan) -> None:
    expected = {row.reference_id for row in inputs.ingredients}
    recorded = _recorded_general(inputs.recipe)
    for key in ("total_minutes", "prep_minutes", "active_minutes", "effort"):
        value = getattr(general, key)
        if value is not None:
            if recorded[key] is not None:
                raise ValueError(f"recorded metadata cannot be changed: {key}")
            if not general.rationales.get(key, "").strip():
                raise ValueError(f"missing metadata rationale: {key}")
    plant_refs = general.plant_ingredient_references
    if plant_refs and (
        len(plant_refs) != len(set(plant_refs))
        or set(plant_refs) != expected
        or not (general.plants_rationale or "").strip()
    ):
        raise ValueError("plant analysis must cover every ingredient with rationale")
    if general.plants and not plant_refs:
        raise ValueError("inferred plants require complete ingredient analysis")
    for plant in general.plants:
        if any(quantity.ingredient_position >= len(inputs.ingredients) for quantity in plant.quantities):
            raise ValueError("plant quantity references unknown ingredient position")


def _recorded_general(recipe: Recipe) -> dict:
    extras = recipe.extras or {}
    try:
        authoring = json.loads(extras.get("healthlabs.ephra", "{}"))
    except ValueError, TypeError:
        authoring = {}
    if not isinstance(authoring, dict):
        authoring = {}
    # The authoring contract stores supported effort flags, not a numeric effort field.
    effort = 1 if authoring.get("ephraEffort1") is True else (2 if authoring.get("ephraEffort2") is True else None)
    return {
        "servings": recipe.recipe_servings if valid_number(recipe.recipe_servings, positive=True) else None,
        "total_minutes": recipe.total_time_seconds / 60 if recipe.total_time_seconds is not None else None,
        "prep_minutes": recipe.prep_time_seconds / 60 if recipe.prep_time_seconds is not None else None,
        "active_minutes": recipe.perform_time_seconds / 60 if recipe.perform_time_seconds is not None else None,
        "effort": effort,
    }


def _compatible(fact: IngredientMetadataInput, plan: MetadataIngredientPlan) -> bool:
    selected = next((candidate for candidate in fact.candidates if candidate.id == plan.source_food_id), None)
    if selected is not None and not _preparation_compatible(fact, selected):
        return False
    # A newly identified food cannot inherit a previous unknown-food interpretation.
    if fact.accepted_source_food_id != plan.accepted_source_food_id:
        return (
            fact.accepted_source_food_id is not None
            and fact.accepted_source_food_id == plan.source_food_id
            and fact.interpretation == plan.interpretation
        )
    if fact.interpretation != plan.accepted_interpretation:
        return False
    return (
        fact.interpretation == plan.interpretation
        if plan.source_food_id is None
        else any(
            candidate.id == plan.source_food_id and candidate.interpretation == plan.interpretation
            for candidate in fact.candidates
        )
    )


def _plant_compatible(fact: IngredientMetadataInput, plan: MetadataIngredientPlan) -> bool:
    # None plant ID alone conflates unknown taxonomy with accepted nonplants.
    # Legacy plans lacking classification identity fail closed for plant reuse,
    # while independent nutrition/time assumptions retain their existing guards.
    return (
        _compatible(fact, plan)
        and fact.plant_id == plan.accepted_plant_id
        and fact.plant_classified == plan.accepted_plant_classified
        and fact.accepted_food_group == plan.accepted_food_group
    )


def _current_grams(fact: IngredientMetadataInput, plan: MetadataIngredientPlan | None) -> Decimal | float | None:
    if valid_number(fact.resolved_grams):
        return fact.resolved_grams
    if plan is None:
        return None
    try:
        if plan.portion_or_density_id is not None:
            grams = _portion_grams(fact, plan)
        elif plan.grams_quality == "estimated" and (plan.quantity_assumption or "").strip():
            grams = Decimal(str(plan.effective_grams))
        else:
            return None
        return grams if valid_number(grams) and grams <= MAX_ESTIMATED_GRAMS else None
    except DecimalException, OverflowError, ValueError:
        return None


def _per_serving_value(total: Decimal, servings: float | None) -> float | None:
    if servings is None:
        return None
    try:
        amount = total / Decimal(str(servings))
        number = float(amount)
        if not valid_number(number):
            return None
        # Above this scale float has no milligram-level precision to preserve;
        # quantizing to 0.001 would exceed Decimal's default significant digits.
        return number if amount.adjusted() > 20 else float(amount.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
    except DecimalException, OverflowError, ValueError:
        return None


def project_metadata(
    inputs: RecipeMetadataInputs,
    payload: HealthLabsRecipeMetadataEstimatePayload | None = None,
    *,
    calculated_at: datetime | None = None,
) -> tuple[RecipeNutritionMetadata, RecipeGeneralMetadata]:
    if payload is not None and (
        payload.validation.group_id != inputs.recipe.group_id
        or payload.validation.recipe_id != inputs.recipe.id
        or payload.validation.authoring_fingerprint != inputs.authoring_fingerprint
        or payload.validation.policy_version != POLICY_VERSION
        or {row.reference_id for row in payload.ingredients} != {row.reference_id for row in inputs.ingredients}
    ):
        payload = None
    recorded = _recorded_general(inputs.recipe)
    servings = recorded["servings"] or (payload.effective_servings if payload and payload.servings_assumption else None)
    provenance: dict[str, NutritionProvenance] = {}
    plans = {row.reference_id: row for row in payload.ingredients} if payload else {}

    def evidence(ref: UUID | None, kind: str, *, fact=None, assumption=None) -> str:
        identity = {
            "reference": str(ref),
            "kind": kind,
            "group": str(inputs.recipe.group_id),
            "recipe": str(inputs.recipe.id),
            "source": str(fact.id)
            if isinstance(fact, MetadataSourceCandidate)
            else str(fact.accepted_source_food_id)
            if fact
            else None,
            "assumption": assumption,
            "evidence": (
                payload.reuse_origin.input_fingerprint if payload.reuse_origin else payload.validation.input_fingerprint
            )
            if payload and kind != "source"
            else None,
            "source_record": fact.source_record_hash if fact else None,
            "provider": payload.model_review.provider if payload and kind != "source" else None,
            "model": payload.model_review.model if payload and kind != "source" else None,
        }
        key = fingerprint(identity)
        provenance[key] = NutritionProvenance(
            id=key,
            ingredient_reference=str(ref) if ref else None,
            kind=kind,
            source_food_id=identity["source"] if fact else None,
            source_release_id=str(
                fact.release_id if isinstance(fact, MetadataSourceCandidate) else fact.accepted_source_release_id
            )
            if fact
            else None,
            source_url=fact.source_url if fact else None,
            assumption=assumption,
            provider=payload.model_review.provider if payload and kind != "source" else None,
            model=payload.model_review.model if payload and kind != "source" else None,
        )
        return key

    serving_refs = (
        [evidence(None, "assumption", assumption=payload.servings_assumption)]
        if payload and recorded["servings"] is None
        else []
    )
    fields = {}
    for code in NUTRIENTS:
        total = Decimal(0)
        verified = bool(inputs.ingredients) and recorded["servings"] is not None
        available = bool(inputs.ingredients) and servings is not None
        missing = []
        methods = set()
        refs = list(serving_refs)
        for fact in inputs.ingredients:
            plan = plans.get(fact.reference_id)
            if plan is not None and not _compatible(fact, plan):
                plan = None
            grams = _current_grams(fact, plan)
            value = fact.nutrients_per_100g.get(code)
            source_backed = valid_number(fact.resolved_grams) and valid_number(value)
            if not source_backed:
                verified = False
                missing.append(str(fact.reference_id))
            if fact.accepted_source_food_id and value is not None:
                refs.append(evidence(fact.reference_id, "source", fact=fact))
            if plan is not None:
                if fact.resolved_grams is None and grams is not None:
                    refs.append(evidence(fact.reference_id, "assumption", assumption=plan.quantity_assumption))
                    methods.add("assumed_quantity")
                if value is None:
                    candidate = next(
                        (candidate for candidate in fact.candidates if candidate.id == plan.source_food_id), None
                    )
                    if candidate is not None:
                        value = candidate.nutrients_per_100g.get(code)
                        if value is not None:
                            methods.add("source_substitution" if fact.accepted_source_food_id else "generic_food")
                            refs.append(evidence(fact.reference_id, "source", fact=candidate))
                            refs.append(evidence(fact.reference_id, "assumption", assumption=plan.rationale))
                    if value is None and code in plan.model_nutrient_keys:
                        value = plan.nutrients_per_100g.get(code)
                        if value is not None:
                            methods.add("model_estimate")
                            refs.append(evidence(fact.reference_id, "model_estimate", assumption=plan.rationale))
            if not valid_number(grams) or not valid_number(value):
                available = False
            else:
                try:
                    total += Decimal(str(grams)) * Decimal(str(value)) / 100
                except DecimalException, OverflowError, ValueError:
                    available = False
        result = _per_serving_value(total, servings) if available else None
        available = available and result is not None
        verified = verified and available
        fields[code] = NutritionMetadataField(
            verified=result if verified else None,
            estimated=result if available and not verified else None,
            value=result,
            quality="source_backed" if verified else "estimated" if available else "unavailable",
            estimate_methods=sorted(methods) if available and not verified else [],
            provenance_ids=sorted(set(refs)) if available else [],
            missing_verified_ingredient_refs=missing,
        )
    required = [fields[code] for code in REQUIRED_NUTRIENTS]
    complete = all(item.value is not None for item in required)
    status = (
        "source_backed"
        if all(item.quality == "source_backed" for item in required)
        else "estimated"
        if complete
        else "partial"
        if any(item.value is not None for item in required)
        else "unavailable"
    )
    general_fields: dict[str, MetadataField] = {}
    for key, value in recorded.items():
        estimate = (
            servings if payload and key == "servings" else getattr(payload.general_metadata, key) if payload else None
        )
        rationale = (
            payload.servings_assumption
            if payload and key == "servings"
            else payload.general_metadata.rationales.get(key)
            if payload
            else None
        )
        refs = [] if value is not None or estimate is None else [evidence(None, "assumption", assumption=rationale)]
        field_type = MetadataEffortField if key == "effort" else MetadataField
        general_fields[key] = field_type(
            recorded=value,
            estimated=estimate if value is None else None,
            value=value if value is not None else estimate,
            quality="recorded" if value is not None else "estimated" if estimate is not None else "unavailable",
            provenance_ids=refs,
        )
    plants = {
        fact.plant_id: MetadataRecordedPlant(plant_id=fact.plant_id, name=fact.plant_name or fact.plant_id)
        for fact in inputs.ingredients
        if fact.plant_id
    }
    plant_complete = bool(inputs.ingredients) and all(fact.plant_classified for fact in inputs.ingredients)
    inferred = []
    plant_refs = []
    if (
        payload
        and all(_plant_compatible(fact, plans[fact.reference_id]) for fact in inputs.ingredients)
        and payload.general_metadata.plant_ingredient_references
    ):
        inferred = [plant for plant in payload.general_metadata.plants if plant.plant_id not in plants]
        plant_complete = True
        plant_refs = [evidence(None, "assumption", assumption=payload.general_metadata.plants_rationale)]
    metadata = RecipeGeneralMetadata(
        **general_fields,
        source=MetadataSource(
            ownership="ephra" if inputs.recipe.healthlabs_scope == "catalogue" else "own",
            url=inputs.recipe.org_url,
            attribution_status="recorded" if inputs.recipe.org_url else "not_recorded",
        ),
        plants=MetadataPlants(
            recorded=list(plants.values()),
            estimated=inferred,
            status="complete" if plant_complete else "partial" if plants else "unavailable",
            provenance_ids=plant_refs,
        ),
        provenance=[
            provenance[key]
            for key in sorted(
                {key for value in general_fields.values() for key in value.provenance_ids} | set(plant_refs)
            )
        ],
    )
    nutrition = RecipeNutritionMetadata(
        status=status,
        complete=complete,
        native_complete=inputs.native_complete,
        effective_servings=servings,
        servings_quality="recorded" if recorded["servings"] else "estimated" if servings else "unavailable",
        per_serving=NutritionMetadataFields(**fields),
        provenance=[
            provenance[key]
            for key in sorted({key for value in fields.values() for key in value.provenance_ids} | set(serving_refs))
        ],
        input_fingerprint=inputs.input_fingerprint,
        calculated_at=calculated_at or get_utc_now(),
    )
    return nutrition, metadata


def _row_data(row) -> dict | None:
    if row is None:
        return None
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
        if column.name not in {"created_at", "update_at", "updated_at"}
    }


def _source_values(food: HealthLabsNutrientFood) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for code in NUTRIENTS:
        fact = food.nutrient_values.get(code, {})
        value = fact.get("amount") if isinstance(fact, dict) else None
        result[code] = float(value) if valid_number(value) else None
    return result


def _interpretation(food: HealthLabsNutrientFood) -> str:
    # Include identity and preparation, not the nutrient hash: improved facts can be recomposed.
    return fingerprint({"id": food.id, "name": food.name, "preparation": food.preparation_state})


def _authoring_ingredient(row: dict[str, Any]) -> dict[str, Any]:
    original = {
        key: row.get(key)
        for key in (
            "reference_id",
            "quantity",
            "note",
            "original_text",
            "title",
            "substitutions",
        )
    }
    # Catalogue timestamps, recipe counts and conversion definitions are dependencies,
    # not edits to the original ingredient. Do not send them as inference context.
    for key, names in (
        ("food", ("id", "name", "plural_name", "description")),
        ("unit", ("id", "name", "plural_name", "abbreviation", "plural_abbreviation")),
    ):
        original[key] = {name: row[key].get(name) for name in names} if row.get(key) else None
    referenced = row.get("referenced_recipe")
    original["referenced_recipe"] = {key: referenced.get(key) for key in ("id", "name")} if referenced else None
    return original


_MEASURE_WORDS = frozenset(
    (
        "g gram grams kg kilogram kilograms mg ml milliliter millilitre milliliters millilitres "
        "l liter litre liters litres oz ounce ounces lb lbs pound pounds tbsp tbs tablespoon tablespoons "
        "tsp teaspoon teaspoons cup cups piece pieces slice slices scoop scoops tin tins can cans "
        "handful handfuls pinch pinches dash dashes clove cloves large medium small "
        "heaped level of a an optional to taste"
    ).split()
)


def _candidate_names(text: str) -> list[list[str]]:
    branches = re.split(r"\bor\b", text.casefold())[:3]
    names = []
    for branch in branches:
        # Preparation notes are retained independently below; quantities and units
        # only help establish mass and must not prevent a food-name lookup.
        tokens = IngredientFoodModel.normalize(re.split(r"[,;(]", branch, maxsplit=1)[0]).split()
        terms = [
            token
            for token in tokens
            if not token.isnumeric()
            and token not in _MEASURE_WORDS
            and token not in {"raw", "cooked", "boiled", "dried"}
        ]
        if terms:
            names.append(terms[:6])
    return names


class RecipeMetadataService:
    """No network, scheduling, commits or writes. Call after recipe authorization.

    Future workers consume ``inputs`` and ``validate_plan`` under their own claim.
    All original context needed for inference remains on inputs.recipe. Candidate
    nutrient values are loaded here, never accepted as model-asserted source facts.
    """

    def __init__(self, session: Session, *, country: str = "GB") -> None:
        self.session = session
        self.country = country.upper()

    def inputs(self, recipe: Recipe) -> RecipeMetadataInputs:
        data = recipe.model_dump(mode="json", by_alias=False)
        authoring = {
            key: data.get(key)
            for key in (
                "id",
                "group_id",
                "name",
                "description",
                "recipe_servings",
                "recipe_yield_quantity",
                "recipe_yield",
                "total_time",
                "prep_time",
                "perform_time",
                "cook_time",
                "total_time_seconds",
                "prep_time_seconds",
                "perform_time_seconds",
                "org_url",
                "healthlabs_scope",
                "extras",
            )
        }
        authoring["ingredients"] = [_authoring_ingredient(row) for row in data["recipe_ingredient"]]
        authoring["instructions"] = [
            {key: value for key, value in row.items() if key != "id"} for row in data.get("recipe_instructions") or []
        ]
        authoring["policy"] = POLICY_VERSION
        authoring_hash = fingerprint(authoring)
        dependencies = []
        ingredients = []
        nutrition = NutritionService(self.session)
        for ingredient in recipe.recipe_ingredient:
            fact = IngredientMetadataInput(reference_id=ingredient.reference_id)
            ingredients.append(fact)
            food_id = ingredient.food.id if ingredient.food else None
            unit_id = ingredient.unit.id if ingredient.unit else None
            food = (
                self.session.scalar(
                    sa.select(IngredientFoodModel).where(
                        IngredientFoodModel.id == food_id, IngredientFoodModel.group_id == recipe.group_id
                    )
                )
                if food_id
                else None
            )
            unit = (
                self.session.scalar(
                    sa.select(IngredientUnitModel).where(
                        IngredientUnitModel.id == unit_id, IngredientUnitModel.group_id == recipe.group_id
                    )
                )
                if unit_id
                else None
            )
            if food_id and food is None or unit_id and unit is None:
                dependencies.append({"reference": ingredient.reference_id, "invalid_group_reference": True})
                continue
            fact.quantity = ingredient.quantity
            if unit:
                fact.unit_names = tuple(
                    value
                    for value in (
                        unit.name,
                        unit.plural_name,
                        unit.abbreviation,
                        unit.plural_abbreviation,
                        unit.standard_unit,
                        *(alias.name for alias in unit.aliases),
                    )
                    if value
                )
                volume_factor = VOLUME_MILLILITRES.get((unit.standard_unit or "").strip().lower())
                if volume_factor is not None and valid_number(ingredient.quantity):
                    fact.input_millilitres = (
                        Decimal(str(ingredient.quantity)) * Decimal(str(unit.standard_quantity or 1)) * volume_factor
                    )
            if unit and valid_number(ingredient.quantity):
                mass_factor = MASS_GRAMS.get((unit.standard_unit or "").strip().lower())
                if mass_factor is not None and valid_number(unit.standard_quantity or 1, positive=True):
                    fact.resolved_grams = (
                        Decimal(str(ingredient.quantity)) * Decimal(str(unit.standard_quantity or 1)) * mass_factor
                    )
            match = (
                self.session.scalar(
                    sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
                )
                if food
                else None
            )
            suppression = (
                self.session.scalar(
                    sa.select(HealthLabsFoodMatchSuppression).where(
                        HealthLabsFoodMatchSuppression.ingredient_food_id == food_id
                    )
                )
                if food
                else None
            )
            accepted = nutrition.food_facts(food.id, unit.id if unit else None, ingredient.quantity) if food else None
            source_food = self.session.get(HealthLabsNutrientFood, match.nutrient_food_id) if match else None
            source = (
                self.session.get(HealthLabsNutrientSourceRelease, source_food.source_release_id)
                if source_food
                else None
            )
            portions = (
                list(
                    self.session.scalars(
                        sa.select(HealthLabsFoodPortion)
                        .where(
                            HealthLabsFoodPortion.nutrient_food_id == source_food.id,
                            sa.or_(
                                HealthLabsFoodPortion.source_managed.is_(True),
                                HealthLabsFoodPortion.group_id == recipe.group_id,
                            ),
                        )
                        .order_by(HealthLabsFoodPortion.id)
                    )
                )
                if source_food
                else []
            )
            mappings = (
                list(
                    self.session.scalars(
                        sa.select(HealthLabsFoodPortionMapping)
                        .where(
                            HealthLabsFoodPortionMapping.group_id == recipe.group_id,
                            HealthLabsFoodPortionMapping.ingredient_food_id == food_id,
                            HealthLabsFoodPortionMapping.ingredient_unit_id == unit_id,
                        )
                        .order_by(HealthLabsFoodPortionMapping.id)
                    )
                )
                if food and unit
                else []
            )
            density_ids = [portion.density_id for portion in portions if portion.density_id]
            densities = (
                list(
                    self.session.scalars(
                        sa.select(HealthLabsFoodDensity)
                        .where(HealthLabsFoodDensity.id.in_(density_ids))
                        .order_by(HealthLabsFoodDensity.id)
                    )
                )
                if density_ids
                else []
            )
            if (
                accepted
                and accepted.status == "matched"
                and accepted.food_match
                and source_food
                and source
                and food
                and match
            ):
                fact.accepted_source_food_id = source_food.id
                fact.accepted_source_release_id = source.id
                fact.accepted_source_name = source_food.name
                fact.accepted_preparation_state = _preparation_key(source_food.preparation_state)
                fact.nutrients_per_100g = _source_values(source_food)
                fact.source_record_hash = fingerprint({"food": _row_data(source_food), "source": _row_data(source)})
                fact.interpretation = _interpretation(source_food)
                fact.source_url = source.source_url
                # Preserve full conversion precision; native public facts round grams to 3 decimals.
                fact.resolved_grams = (
                    nutrition._resolve_grams(
                        food.id, source_food.id, unit.id if unit else None, Decimal(str(ingredient.quantity))
                    )
                    if ingredient.quantity is not None and valid_number(ingredient.quantity)
                    else None
                )
                fact.plant_id = match.plant_id
                fact.plant_name = food.name
                fact.accepted_food_group = (match.food_group or "").strip().casefold() or None
                fact.plant_classified = bool(match.plant_id) or fact.accepted_food_group in {
                    "meat",
                    "fish",
                    "eggs",
                    "dairy",
                    "water",
                    "salt",
                }
                fact.portions = self._portions(source_food.id, recipe.group_id)
            text = ingredient.food.name if ingredient.food else ingredient.note or ingredient.original_text or ""
            preparation_context = " ".join(
                value
                for value in (
                    ingredient.food.name if ingredient.food else None,
                    ingredient.note,
                    ingredient.original_text,
                )
                if value
            )
            fact.preparation_constraint = fact.accepted_preparation_state or _preparation_from_text(preparation_context)
            fact.candidates = (
                self._candidates(text, group_id=recipe.group_id, preparation_state=fact.preparation_constraint)
                if suppression is None
                else []
            )
            dependencies.append(
                {
                    "reference": ingredient.reference_id,
                    "food": _row_data(food),
                    "unit": _row_data(unit),
                    "unit_aliases": sorted((str(alias.id), alias.name) for alias in unit.aliases) if unit else [],
                    "conversion_inputs": {
                        "quantity": fact.quantity,
                        "unit_names": sorted(fact.unit_names),
                        "millilitres": fact.input_millilitres,
                        "grams": fact.resolved_grams,
                    },
                    "match": _row_data(match),
                    "suppression": _row_data(suppression),
                    "source_food": _row_data(source_food),
                    "source": _row_data(source),
                    "portions": [_row_data(row) for row in portions],
                    "mappings": [_row_data(row) for row in mappings],
                    "densities": [_row_data(row) for row in densities],
                    "eligible_portions": [asdict(portion) for portion in fact.portions],
                    "candidates": [asdict(candidate) for candidate in fact.candidates],
                }
            )
        native_complete = (
            bool(ingredients)
            and valid_number(recipe.recipe_servings, positive=True)
            and all(
                valid_number(fact.resolved_grams)
                and all(valid_number(fact.nutrients_per_100g.get(code)) for code in NUTRIENTS)
                for fact in ingredients
            )
        )
        return RecipeMetadataInputs(
            recipe=recipe,
            authoring_fingerprint=authoring_hash,
            input_fingerprint=fingerprint(
                {"authoring": authoring_hash, "dependencies": dependencies, "calculation": POLICY_VERSION}
            ),
            ingredients=ingredients,
            native_complete=native_complete,
            authoring_context=authoring,
        )

    def _portions(self, food_id: UUID, group_id: UUID | None) -> list[MetadataPortionCandidate]:
        query = sa.select(HealthLabsFoodPortion).where(HealthLabsFoodPortion.nutrient_food_id == food_id)
        query = (
            query.where(
                sa.or_(HealthLabsFoodPortion.source_managed.is_(True), HealthLabsFoodPortion.group_id == group_id)
            )
            if group_id
            else query.where(HealthLabsFoodPortion.source_managed.is_(True))
        )
        result = []
        for portion in self.session.scalars(query.order_by(HealthLabsFoodPortion.id).limit(64)):
            density = self.session.get(HealthLabsFoodDensity, portion.density_id) if portion.density_id else None
            release = self.session.get(HealthLabsNutrientSourceRelease, density.source_release_id) if density else None
            result.append(
                MetadataPortionCandidate(
                    id=portion.id,
                    description=portion.description,
                    amount=portion.amount,
                    unit=portion.unit,
                    grams=portion.grams,
                    density_id=portion.density_id,
                    density_grams_per_ml=density.grams_per_ml if density else None,
                    source_record_hash=fingerprint(
                        {"portion": _row_data(portion), "density": _row_data(density), "release": _row_data(release)}
                    ),
                )
            )
        return result

    def _candidates(
        self, text: str, *, group_id: UUID | None = None, preparation_state: str | None = None
    ) -> list[MetadataSourceCandidate]:
        names = _candidate_names(text)
        if not names:
            return []
        query = (
            sa.select(HealthLabsNutrientFood, HealthLabsNutrientSourceRelease)
            .join(
                HealthLabsNutrientSourceRelease,
                HealthLabsNutrientSourceRelease.id == HealthLabsNutrientFood.source_release_id,
            )
            .where(sa.func.upper(HealthLabsNutrientSourceRelease.country).in_({self.country, "OTHER"}))
        )
        lower_name = sa.func.lower(HealthLabsNutrientFood.name)
        query = query.where(
            sa.or_(*(sa.and_(*(lower_name.contains(term, autoescape=True) for term in terms)) for terms in names))
        )
        expected_state = _preparation_key(preparation_state) or _preparation_from_text(text)
        # Omit an unused rank: PostgreSQL interprets a bare ORDER BY 0 as a column position.
        preparation_order: list[sa.ColumnElement[int]] = []
        if expected_state:
            source_state = sa.func.lower(sa.func.trim(HealthLabsNutrientFood.preparation_state))
            # Mirror the existing native preparation normalization before the SQL cap.
            markers = (
                "cooked",
                "boiled",
                "cuite",
                "cuit",
                "gegart",
                "gekocht",
                "raw",
                "crue",
                "cru",
                "roh",
                "dried",
                "dry",
                "séch",
                "getrocknet",
            )
            normalized_state = sa.case(
                *((source_state.contains(marker, autoescape=True), _preparation_key(marker)) for marker in markers),
                else_=source_state,
            )
            query = query.where(
                sa.or_(
                    normalized_state == expected_state,
                    sa.and_(sa.literal(preparation_state is None), HealthLabsNutrientFood.preparation_state.is_(None)),
                )
            )
            preparation_order.append(sa.case((normalized_state == expected_state, 0), else_=1))
        exact = lower_name.in_([" ".join(terms) for terms in names])
        # Rank meaning before the bounded cap, using UUID only to break genuine ties.
        rows = self.session.execute(
            query.order_by(
                sa.case((exact, 0), else_=1),
                *preparation_order,
                sa.func.length(HealthLabsNutrientFood.name),
                lower_name,
                HealthLabsNutrientFood.id,
            ).limit(8)
        )
        return [
            MetadataSourceCandidate(
                id=food.id,
                release_id=source.id,
                name=food.name,
                interpretation=_interpretation(food),
                nutrients_per_100g=_source_values(food),
                source_record_hash=fingerprint({"food": _row_data(food), "source": _row_data(source)}),
                source_url=source.source_url,
                preparation_state=_preparation_key(food.preparation_state),
                portions=self._portions(food.id, group_id),
            )
            for food, source in rows
        ]

    def _ready_evidence(
        self, inputs: RecipeMetadataInputs
    ) -> Iterator[tuple[HealthLabsRecipeMetadataEstimate, HealthLabsRecipeMetadataEstimatePayload]]:
        recipe = inputs.recipe
        rows = self.session.scalars(
            sa.select(HealthLabsRecipeMetadataEstimate)
            .where(
                HealthLabsRecipeMetadataEstimate.group_id == recipe.group_id,
                HealthLabsRecipeMetadataEstimate.recipe_id == recipe.id,
                HealthLabsRecipeMetadataEstimate.authoring_fingerprint == inputs.authoring_fingerprint,
                HealthLabsRecipeMetadataEstimate.policy_version == POLICY_VERSION,
                HealthLabsRecipeMetadataEstimate.state == "ready",
            )
            .order_by(
                sa.case((HealthLabsRecipeMetadataEstimate.input_fingerprint == inputs.input_fingerprint, 0), else_=1),
                HealthLabsRecipeMetadataEstimate.created_at.desc(),
                HealthLabsRecipeMetadataEstimate.id.desc(),
            )
            .limit(32)
        )
        for row in rows:
            try:
                payload = HealthLabsRecipeMetadataEstimatePayload.model_validate(row.payload)
                if (
                    payload.validation.input_fingerprint != row.input_fingerprint
                    or payload.validation.authoring_fingerprint != row.authoring_fingerprint
                    or payload.validation.group_id != row.group_id
                    or payload.validation.recipe_id != row.recipe_id
                    or payload.validation.policy_version != row.policy_version
                    or payload.model_review.provider != row.provider_name
                    or payload.model_review.model != row.provider_model
                ):
                    continue
                for ingredient in payload.ingredients:
                    _bounds(ingredient.nutrients_per_100g, set(ingredient.model_nutrient_keys))
                if payload.reuse_origin:
                    origin = self.session.get(HealthLabsRecipeMetadataEstimate, payload.reuse_origin.estimate_id)
                    if (
                        origin is None
                        or origin.state != "ready"
                        or (
                            origin.group_id != row.group_id
                            or origin.recipe_id != row.recipe_id
                            or origin.authoring_fingerprint != row.authoring_fingerprint
                            or origin.policy_version != row.policy_version
                            or origin.input_fingerprint != payload.reuse_origin.input_fingerprint
                            or origin.updated_at != payload.reuse_origin.inferred_at
                            or origin.provider_name != row.provider_name
                            or origin.provider_model != row.provider_model
                        )
                    ):
                        continue
                yield row, payload
            except ValidationError, ValueError, TypeError, DecimalException, OverflowError:
                continue

    def reusable_payload(self, inputs: RecipeMetadataInputs) -> HealthLabsRecipeMetadataEstimatePayload | None:
        """Materialize only current, complete assumption inputs, preserving inference lineage.

        This never writes. All source values and conversions are re-read from inputs;
        only proven-compatible missing assumptions pass the ordinary validator again.
        """
        for evidence, prior in self._ready_evidence(inputs):
            if inputs.missing_fields(prior):
                continue
            try:
                old_rows = {row.reference_id: row for row in prior.ingredients}
                rows = []
                compatible_plants = True
                for fact in inputs.ingredients:
                    old = old_rows[fact.reference_id]
                    compatible = _compatible(fact, old)
                    compatible_plants &= _plant_compatible(fact, old)
                    selected = next((c for c in fact.candidates if c.id == old.source_food_id), None)
                    grams = _current_grams(fact, old if compatible else None)
                    if grams is None:
                        raise ValueError("missing current mass")
                    rows.append(
                        MetadataIngredientResponse(
                            reference_id=fact.reference_id,
                            effective_grams=float(grams),
                            quantity_assumption=old.quantity_assumption
                            if fact.resolved_grams is None and compatible
                            else None,
                            source_food_id=old.source_food_id if compatible else None,
                            portion_or_density_id=old.portion_or_density_id
                            if fact.resolved_grams is None and compatible
                            else None,
                            assumed_portion_amount=old.assumed_portion_amount
                            if fact.resolved_grams is None and compatible
                            else None,
                            nutrients_per_100g={
                                key: old.nutrients_per_100g.get(key)
                                for key in old.model_nutrient_keys
                                if compatible
                                and fact.nutrients_per_100g.get(key) is None
                                and (selected is None or selected.nutrients_per_100g.get(key) is None)
                            },
                            rationale=old.rationale,
                        )
                    )
                general = prior.general_metadata.model_copy(deep=True)
                if not compatible_plants:
                    general.plants, general.plant_ingredient_references, general.plants_rationale = [], [], None
                payload = validate_plan(
                    inputs,
                    RecipeMetadataProviderResponse(
                        effective_servings=inputs.recipe.recipe_servings
                        if valid_number(inputs.recipe.recipe_servings, positive=True)
                        else prior.effective_servings,
                        servings_assumption=None
                        if valid_number(inputs.recipe.recipe_servings, positive=True)
                        else prior.servings_assumption,
                        ingredients=rows,
                        general_metadata=general,
                    ),
                    provider=prior.model_review.provider,
                    model=prior.model_review.model,
                )
                if inputs.missing_fields(payload):
                    continue
                payload.reuse_origin = prior.reuse_origin or MetadataReuseOrigin(
                    estimate_id=evidence.id,
                    input_fingerprint=evidence.input_fingerprint,
                    inferred_at=evidence.updated_at,
                )
                return payload
            except ValidationError, ValueError, TypeError, DecimalException, OverflowError, KeyError:
                continue
        return None

    def project(self, recipe: Recipe) -> tuple[RecipeNutritionMetadata, RecipeGeneralMetadata]:
        inputs = self.inputs(recipe)
        for _, payload in self._ready_evidence(inputs):
            return project_metadata(inputs, payload)
        return project_metadata(inputs)
