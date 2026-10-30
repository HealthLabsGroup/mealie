from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa

from mealie.schema.recipe import Recipe, RecipeIngredient, RecipeStep, SaveIngredientUnit
from tests.unit_tests.healthlabs.test_nutrition_calculation import (
    CORE_VALUES,
    _ingredient_food,
    _nutrient_food,
    _reviewed_match,
    _source,
)
from tests.utils import TestUser


def _recipe(user, *, servings=2):
    food = _ingredient_food(user, f"rice-{uuid4()}")
    source_food = _nutrient_food(user, _source(user), name=food.name)
    _reviewed_match(user, food.id, source_food.id)
    unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=user.group_id, name=f"g-{uuid4()}", standard_unit="gram", standard_quantity=1)
    )
    recipe = user.repos.recipes.create(
        Recipe(
            name=f"Metadata {uuid4()}",
            user_id=user.user_id,
            household_id=user.household_id,
            group_id=user.group_id,
            recipe_servings=servings,
            recipe_ingredient=[RecipeIngredient(reference_id=uuid4(), food=food, unit=unit, quantity=100)],
        )
    )
    return recipe, source_food


def test_read_only_projection_attaches_fieldwise_current_facts(unique_user_fn_scoped: TestUser):
    from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    service = RecipeMetadataService(user.repos.session)
    nutrition, metadata = service.project(recipe)
    assert nutrition.per_serving.protein_g.verified == 5
    assert nutrition.per_serving.water_ml.value is None
    assert nutrition.complete is True
    assert metadata.servings.recorded == 2
    assert (
        user.repos.session.scalar(
            sa.select(sa.func.count())
            .select_from(HealthLabsRecipeMetadataEstimate)
            .where(HealthLabsRecipeMetadataEstimate.recipe_id == recipe.id)
        )
        == 0
    )
    assert "prep_minutes" in service.inputs(recipe).missing_fields()


def test_authoring_and_dependency_fingerprints_separate_source_edits(unique_user_fn_scoped: TestUser):
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, source_food = _recipe(user)
    service = RecipeMetadataService(user.repos.session)
    before = service.inputs(recipe)
    source_food.nutrient_values = {**CORE_VALUES, "protein_g": {"amount": 20, "unit": "g"}}
    user.repos.session.commit()
    after = service.inputs(recipe)
    assert before.authoring_fingerprint == after.authoring_fingerprint
    assert before.input_fingerprint != after.input_fingerprint
    assert service.project(recipe)[0].per_serving.protein_g.verified == 10
    recipe.recipe_instructions = [RecipeStep(text="Use half the rice")]
    assert after.authoring_fingerprint != service.inputs(recipe).authoring_fingerprint


def test_ready_evidence_is_recomposed_and_original_edit_hides_it(unique_user_fn_scoped: TestUser):
    from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate
    from mealie.services.healthlabs.recipe_metadata_service import POLICY_VERSION, RecipeMetadataService, validate_plan

    user = unique_user_fn_scoped
    recipe, source_food = _recipe(user, servings=0)
    service = RecipeMetadataService(user.repos.session)
    inputs = service.inputs(recipe)
    payload = validate_plan(
        inputs,
        {
            "effective_servings": 2,
            "servings_assumption": "Two portions",
            "ingredients": [
                {
                    "reference_id": str(inputs.ingredients[0].reference_id),
                    "effective_grams": 100,
                    "nutrients_per_100g": {},
                    "rationale": "Recorded mass",
                }
            ],
            "general_metadata": {},
        },
        provider="fake",
        model="fake",
    )
    user.repos.session.add(
        HealthLabsRecipeMetadataEstimate(
            group_id=user.group_id,
            recipe_id=recipe.id,
            authoring_fingerprint=inputs.authoring_fingerprint,
            input_fingerprint=inputs.input_fingerprint,
            policy_version=POLICY_VERSION,
            state="ready",
            payload=payload.model_dump(mode="json"),
            provider_name="fake",
            provider_model="fake",
            next_attempt_at=datetime.now(UTC),
        )
    )
    user.repos.session.commit()
    source_food.nutrient_values = {**CORE_VALUES, "protein_g": {"amount": 20, "unit": "g"}}
    user.repos.session.commit()
    assert service.project(recipe)[0].per_serving.protein_g.estimated == 10
    recipe.recipe_servings = 4
    assert service.project(recipe)[0].per_serving.protein_g.verified == 5
    recipe.recipe_servings = 0
    recipe.description = "Changed original context"
    assert service.project(recipe)[0].per_serving.protein_g.value is None


def test_foreign_group_ingredient_facts_are_not_exposed(unique_user_fn_scoped: TestUser):
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    recipe.group_id = uuid4()
    inputs = RecipeMetadataService(user.repos.session).inputs(recipe)
    assert inputs.ingredients[0].accepted_source_food_id is None
    assert inputs.ingredients[0].candidates == []


def test_recorded_mass_is_fixed_even_without_an_accepted_food_match(unique_user_fn_scoped: TestUser):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    user.repos.session.execute(
        sa.delete(HealthLabsFoodMatch).where(
            HealthLabsFoodMatch.ingredient_food_id == recipe.recipe_ingredient[0].food.id
        )
    )
    user.repos.session.commit()
    inputs = RecipeMetadataService(user.repos.session).inputs(recipe)
    assert inputs.ingredients[0].resolved_grams == 100
    assert inputs.ingredients[0].accepted_source_food_id is None


def test_conversion_definition_change_is_a_fact_refresh_not_an_authoring_edit(unique_user_fn_scoped: TestUser):
    from mealie.db.models.recipe import IngredientUnitModel
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    service = RecipeMetadataService(user.repos.session)
    before = service.inputs(recipe)
    unit = user.repos.session.get(IngredientUnitModel, recipe.recipe_ingredient[0].unit.id)
    unit.standard_quantity = 2
    user.repos.session.commit()
    refreshed = user.repos.recipes.get_one(recipe.id, "id")
    after = service.inputs(refreshed)
    assert after.authoring_fingerprint == before.authoring_fingerprint
    assert after.input_fingerprint != before.input_fingerprint
    assert after.ingredients[0].resolved_grams == 200


@pytest.mark.parametrize(("raw", "name"), [("2 tbsp olive oil", "olive oil"), ("150 g Greek yoghurt", "Greek yoghurt")])
def test_raw_measurements_do_not_hide_credible_source_candidates(unique_user_fn_scoped: TestUser, raw, name):
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    source_food = _nutrient_food(user, _source(user), name=name)
    recipe = Recipe(
        id=uuid4(),
        group_id=user.group_id,
        name="Raw recipe",
        recipe_servings=2,
        recipe_ingredient=[RecipeIngredient(reference_id=uuid4(), original_text=raw, note=raw)],
    )
    before = recipe.model_dump(mode="json")
    candidates = RecipeMetadataService(user.repos.session).inputs(recipe).ingredients[0].candidates
    assert source_food.id in [candidate.id for candidate in candidates]
    assert recipe.model_dump(mode="json") == before


def test_exact_name_and_compatible_preparation_rank_before_eight_candidate_cap(unique_user_fn_scoped: TestUser):
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    unique = f"lentil{uuid4().hex}"
    source = _source(user)
    # Nine raw variants must not crowd the exact cooked identity out of the bounded query.
    for index in range(9):
        _nutrient_food(user, source, name=f"{unique} raw variant {index}", state="raw")
    correct = _nutrient_food(user, source, name=unique, state="cooked")
    service = RecipeMetadataService(user.repos.session)
    candidates = service._candidates(f"150 g {unique}, cooked")
    assert candidates[0].id == correct.id
    assert all("raw" not in candidate.name for candidate in candidates)
    assert len(candidates) <= 8


def test_durable_payload_rejects_unknown_configuration_and_nonfinite_values():
    from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate

    with pytest.raises(ValueError):
        HealthLabsRecipeMetadataEstimate(payload={"api_key": "fake-must-not-persist"})


def test_portion_candidates_exclude_other_groups_and_honor_suppression(
    unique_user_fn_scoped: TestUser, unique_user: TestUser
):
    from mealie.db.models.healthlabs import HealthLabsFoodMatchSuppression, HealthLabsFoodPortion
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, source_food = _recipe(user)
    public, own, foreign = [
        HealthLabsFoodPortion(
            nutrient_food_id=source_food.id,
            description=description,
            amount=1,
            unit="piece",
            grams=grams,
            source_managed=owner is None,
            group_id=owner,
        )
        for description, grams, owner in (
            ("public", 80, None),
            ("own", 90, user.group_id),
            ("foreign", 100, unique_user.group_id),
        )
    ]
    user.repos.session.add_all([public, own, foreign])
    user.repos.session.commit()
    service = RecipeMetadataService(user.repos.session)
    fact = service.inputs(recipe).ingredients[0]
    assert {portion.id for portion in fact.portions} == {public.id, own.id}
    candidate = next(candidate for candidate in fact.candidates if candidate.id == source_food.id)
    assert {portion.id for portion in candidate.portions} == {public.id, own.id}
    user.repos.session.add(
        HealthLabsFoodMatchSuppression(
            ingredient_food_id=recipe.recipe_ingredient[0].food.id,
            suppressed_by_user_id=user.user_id,
            suppressed_at=datetime.now(UTC),
        )
    )
    user.repos.session.commit()
    suppressed = service.inputs(recipe).ingredients[0]
    assert suppressed.portions == []
    assert suppressed.candidates == []


def test_accepted_raw_rice_constrains_candidates_without_raw_in_original_name(unique_user_fn_scoped: TestUser):
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService, validate_plan

    user = unique_user_fn_scoped
    recipe, accepted = _recipe(user)
    accepted.preparation_state = "raw"
    accepted.nutrient_values = {**CORE_VALUES, "fibre_g": {"amount": None, "unit": "g"}}
    user.repos.session.commit()
    cooked = _nutrient_food(user, _source(user), name=accepted.name, state="cooked")
    compatible = _nutrient_food(
        user,
        _source(user),
        name=accepted.name,
        state="raw",
        values={**CORE_VALUES, "fibre_g": {"amount": 3, "unit": "g"}},
    )
    inputs = RecipeMetadataService(user.repos.session).inputs(recipe)
    fact = inputs.ingredients[0]
    assert "raw" not in recipe.recipe_ingredient[0].food.name
    assert fact.accepted_source_name == accepted.name
    assert fact.accepted_preparation_state == "raw"
    assert cooked.id not in {candidate.id for candidate in fact.candidates}
    selected = next(candidate for candidate in fact.candidates if candidate.id == compatible.id)
    assert selected.name == compatible.name
    assert selected.preparation_state == "raw"
    plan = validate_plan(
        inputs,
        {
            "effective_servings": 2,
            "ingredients": [
                {
                    "reference_id": str(fact.reference_id),
                    "effective_grams": 100,
                    "source_food_id": str(compatible.id),
                    "rationale": "Compatible raw rice fibre source",
                }
            ],
            "general_metadata": {},
        },
        provider="fake",
        model="fake",
    )
    assert plan.ingredients[0].nutrients_per_100g["fibre_g"] == 3


def test_unit_alias_conversion_changes_only_the_dependency_fingerprint(unique_user_fn_scoped: TestUser):
    from mealie.db.models.healthlabs import HealthLabsFoodPortion
    from mealie.db.models.recipe.ingredient import IngredientUnitAliasModel, IngredientUnitModel
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, source_food = _recipe(user)
    unit = user.repos.session.get(IngredientUnitModel, recipe.recipe_ingredient[0].unit.id)
    unit.standard_unit = None
    user.repos.session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=source_food.id,
            description="review-piece",
            amount=1,
            unit="review-piece",
            grams=10,
            source_managed=True,
        )
    )
    user.repos.session.commit()
    service = RecipeMetadataService(user.repos.session)
    before = service.inputs(recipe)
    assert before.ingredients[0].resolved_grams is None
    alias = IngredientUnitAliasModel(session=user.repos.session, name="review-piece", unit_id=unit.id)
    user.repos.session.add(alias)
    user.repos.session.commit()
    after = service.inputs(recipe)
    assert after.ingredients[0].resolved_grams == 1000
    assert after.authoring_fingerprint == before.authoring_fingerprint
    assert after.input_fingerprint != before.input_fingerprint


@pytest.mark.parametrize("context_field", ["note", "original_text"])
def test_linked_food_keeps_explicit_preparation_from_its_original_context(
    unique_user_fn_scoped: TestUser, context_field
):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, raw = _recipe(user)
    raw.preparation_state = "raw"
    setattr(recipe.recipe_ingredient[0], context_field, "cooked")
    user.repos.session.execute(
        sa.delete(HealthLabsFoodMatch).where(
            HealthLabsFoodMatch.ingredient_food_id == recipe.recipe_ingredient[0].food.id
        )
    )
    cooked = _nutrient_food(user, _source(user), name=raw.name, state="cooked")
    user.repos.session.commit()
    fact = RecipeMetadataService(user.repos.session).inputs(recipe).ingredients[0]
    assert raw.id not in {candidate.id for candidate in fact.candidates}
    assert cooked.id in {candidate.id for candidate in fact.candidates}
    assert fact.preparation_constraint == "cooked"


@pytest.mark.parametrize("dimension", ["mass", "servings"])
def test_read_rejects_unsafe_historical_payload_and_withdrawn_source_conversion(
    unique_user_fn_scoped: TestUser, dimension
):
    from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate
    from mealie.db.models.recipe import IngredientUnitModel
    from mealie.services.healthlabs.recipe_metadata_service import POLICY_VERSION, RecipeMetadataService, validate_plan

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user, servings=0 if dimension == "servings" else 2)
    service = RecipeMetadataService(user.repos.session)
    inputs = service.inputs(recipe)
    plan = validate_plan(
        inputs,
        {
            "effective_servings": 2,
            "servings_assumption": "Two portions" if dimension == "servings" else None,
            "ingredients": [
                {
                    "reference_id": str(inputs.ingredients[0].reference_id),
                    "effective_grams": 100,
                    "rationale": "Fixed source mass",
                }
            ],
            "general_metadata": {},
        },
        provider="fake",
        model="fake",
    )
    row = HealthLabsRecipeMetadataEstimate(
        group_id=user.group_id,
        recipe_id=recipe.id,
        authoring_fingerprint=inputs.authoring_fingerprint,
        input_fingerprint=inputs.input_fingerprint,
        policy_version=POLICY_VERSION,
        state="ready",
        payload=plan.model_dump(mode="json"),
        provider_name="fake",
        provider_model="fake",
        next_attempt_at=datetime.now(UTC),
    )
    user.repos.session.add(row)
    user.repos.session.commit()
    unit = user.repos.session.get(IngredientUnitModel, recipe.recipe_ingredient[0].unit.id)
    unit.standard_unit = None
    user.repos.session.commit()
    assert service.project(recipe)[0].per_serving.kcal.value is None
    unsafe = plan.model_dump(mode="json")
    if dimension == "mass":
        unsafe["ingredients"][0].update(
            effective_grams=1e100, grams_quality="estimated", quantity_assumption="Assume mass"
        )
    else:
        unsafe["effective_servings"] = 1e-100
    # Historical/bulk SQL payloads bypass the ORM validator; GET must still fail closed.
    user.repos.session.execute(
        sa.update(HealthLabsRecipeMetadataEstimate)
        .where(HealthLabsRecipeMetadataEstimate.id == row.id)
        .values(payload=unsafe)
    )
    user.repos.session.commit()
    assert service.project(recipe)[0].per_serving.kcal.value is None


def test_ready_polyol_source_facts_keep_valid_general_estimates(unique_user_fn_scoped: TestUser):
    from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate
    from mealie.services.healthlabs.recipe_metadata_service import POLICY_VERSION, RecipeMetadataService, validate_plan

    user = unique_user_fn_scoped
    recipe, source_food = _recipe(user)
    source_food.nutrient_values = {
        key: {"amount": 100 if key == "carbs_g" else 0, "unit": value["unit"]} for key, value in CORE_VALUES.items()
    }
    user.repos.session.commit()
    service = RecipeMetadataService(user.repos.session)
    inputs = service.inputs(recipe)
    plan = validate_plan(
        inputs,
        {
            "effective_servings": 2,
            "ingredients": [
                {
                    "reference_id": str(inputs.ingredients[0].reference_id),
                    "effective_grams": 100,
                    "rationale": "Fixed facts",
                }
            ],
            "general_metadata": {"prep_minutes": 5, "rationales": {"prep_minutes": "Measure ingredients"}},
        },
        provider="fake",
        model="fake",
    )
    user.repos.session.add(
        HealthLabsRecipeMetadataEstimate(
            group_id=user.group_id,
            recipe_id=recipe.id,
            authoring_fingerprint=inputs.authoring_fingerprint,
            input_fingerprint=inputs.input_fingerprint,
            policy_version=POLICY_VERSION,
            state="ready",
            payload=plan.model_dump(mode="json"),
            provider_name="fake",
            provider_model="fake",
            next_attempt_at=datetime.now(UTC),
        )
    )
    user.repos.session.commit()
    nutrition, metadata = service.project(recipe)
    assert nutrition.per_serving.kcal.verified == 0
    assert nutrition.per_serving.carbs_g.verified == 50
    assert metadata.prep_minutes.estimated == 5
