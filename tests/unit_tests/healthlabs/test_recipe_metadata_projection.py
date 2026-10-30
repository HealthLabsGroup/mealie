"""Field coverage, authoring freshness and estimate validation, using hand-calculated plans."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mealie.schema.recipe import Recipe, RecipeIngredient


def _inputs(*, servings=2, values=None, grams=100):
    from mealie.services.healthlabs.recipe_metadata_service import IngredientMetadataInput, RecipeMetadataInputs

    reference = uuid4()
    recipe = Recipe(
        id=uuid4(),
        group_id=uuid4(),
        name="Example",
        recipe_servings=servings,
        recipe_ingredient=[RecipeIngredient(reference_id=reference, note="rice", quantity=100)],
    )
    return RecipeMetadataInputs(
        recipe=recipe,
        authoring_fingerprint="a" * 64,
        input_fingerprint="b" * 64,
        ingredients=[
            IngredientMetadataInput(
                reference_id=reference,
                accepted_source_food_id=uuid4(),
                accepted_source_release_id=uuid4(),
                interpretation="rice:cooked",
                resolved_grams=grams,
                nutrients_per_100g=values or {"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2, "fibre_g": 0},
                source_url="https://example.test/source",
                source_record_hash="c" * 64,
            )
        ],
        native_complete=False,
    )


def _response(inputs):
    return {
        "effective_servings": 2,
        "servings_assumption": None,
        "ingredients": [
            {
                "reference_id": str(inputs.ingredients[0].reference_id),
                "effective_grams": 100,
                "quantity_assumption": None,
                "source_food_id": None,
                "nutrients_per_100g": {"fibre_g": 3},
                "rationale": "Typical cooked rice fibre",
            }
        ],
        "general_metadata": {
            "prep_minutes": 5,
            "active_minutes": 10,
            "total_minutes": 20,
            "effort": 1,
            "rationales": {
                "prep_minutes": "Rinse",
                "active_minutes": "Stir",
                "total_minutes": "Simmer",
                "effort": "One pot",
            },
            "plants": [],
            "plant_ingredient_references": [str(inputs.ingredients[0].reference_id)],
            "plants_rationale": "No additional plants",
        },
    }


def _plan(inputs, response=None):
    from mealie.services.healthlabs.recipe_metadata_service import validate_plan

    return validate_plan(inputs, response or _response(inputs), provider="test-provider", model="test-model")


def test_five_field_source_coverage_survives_missing_water_and_preserves_zero():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    nutrition, metadata = project_metadata(_inputs(), calculated_at=datetime(2026, 10, 30, tzinfo=UTC))
    assert nutrition.complete is True
    assert nutrition.native_complete is False
    assert nutrition.status == "source_backed"
    assert nutrition.per_serving.protein_g.verified == 2
    assert nutrition.per_serving.fibre_g.value == 0
    assert nutrition.per_serving.water_ml.value is None
    assert metadata.source.attribution_status == "not_recorded"


def test_only_missing_nutrient_is_estimated_and_totals_are_calculated_in_code():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    nutrition, metadata = project_metadata(inputs, _plan(inputs))
    assert nutrition.per_serving.protein_g.verified == 2
    assert nutrition.per_serving.protein_g.estimated is None
    assert nutrition.per_serving.fibre_g.verified is None
    assert nutrition.per_serving.fibre_g.estimated == 1.5
    assert nutrition.per_serving.fibre_g.estimate_methods == ["model_estimate"]
    assert nutrition.status == "estimated"
    assert metadata.total_minutes.value == 20
    assert metadata.plants.status == "complete"
    evidence = {p.id: p for p in nutrition.provenance}
    assert any(evidence[p].provider == "test-provider" for p in nutrition.per_serving.fibre_g.provenance_ids)


def test_missing_mass_and_servings_are_explicit_assumptions():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(grams=None, servings=0)
    response = _response(inputs)
    response["ingredients"][0]["nutrients_per_100g"] = {}
    response["ingredients"][0]["quantity_assumption"] = "Assume 100 g cooked rice"
    response["servings_assumption"] = "Assume two adult portions"
    nutrition, metadata = project_metadata(inputs, _plan(inputs, response))
    assert nutrition.per_serving.kcal.value == 60
    assert nutrition.per_serving.kcal.verified is None
    assert nutrition.servings_quality == "estimated"
    assert metadata.servings.recorded is None
    assert metadata.servings.estimated == 2


def test_no_partial_sum_when_one_ingredient_is_uncovered():
    from mealie.services.healthlabs.recipe_metadata_service import IngredientMetadataInput, project_metadata

    inputs = _inputs()
    inputs.ingredients.append(IngredientMetadataInput(reference_id=uuid4()))
    nutrition, _ = project_metadata(inputs)
    assert nutrition.per_serving.kcal.value is None
    assert nutrition.per_serving.kcal.missing_verified_ingredient_refs == [str(inputs.ingredients[1].reference_id)]


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "duplicate", "negative", "nan", "macro", "fixed", "source", "mass", "servings"]
)
def test_invalid_model_plans_are_rejected(mutation):
    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    response = _response(inputs)
    row = response["ingredients"][0]
    if mutation == "missing":
        response["ingredients"] = []
    if mutation == "extra":
        response["ingredients"].append({**row, "reference_id": str(uuid4())})
    if mutation == "duplicate":
        response["ingredients"].append(dict(row))
    if mutation == "negative":
        row["nutrients_per_100g"] = {"fibre_g": -1}
    if mutation == "nan":
        row["nutrients_per_100g"] = {"fibre_g": float("nan")}
    if mutation == "macro":
        row["nutrients_per_100g"] = {"fibre_g": 110}
    if mutation == "fixed":
        row["nutrients_per_100g"] = {"protein_g": 20, "fibre_g": 3}
    if mutation == "source":
        row["source_food_id"] = str(uuid4())
    if mutation == "mass":
        row["effective_grams"] = 200
    if mutation == "servings":
        response["effective_servings"] = 3
    with pytest.raises(ValueError):
        _plan(inputs, response)


def test_source_improvement_recomposes_without_reusing_cached_totals():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    plan = _plan(inputs)
    inputs.ingredients[0].nutrients_per_100g["protein_g"] = 8
    inputs.ingredients[0].nutrients_per_100g["fibre_g"] = 2
    nutrition, _ = project_metadata(inputs, plan)
    assert nutrition.per_serving.protein_g.verified == 4
    assert nutrition.per_serving.fibre_g.verified == 1
    assert nutrition.per_serving.fibre_g.estimated is None


def test_contradictory_preparation_invalidates_affected_assumptions():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    plan = _plan(inputs)
    inputs.ingredients[0].interpretation = "rice:raw"
    nutrition, _ = project_metadata(inputs, plan)
    assert nutrition.per_serving.protein_g.verified == 2
    assert nutrition.per_serving.fibre_g.value is None


def test_authoring_edit_and_cross_group_payload_hide_all_stale_assumptions():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(grams=None)
    response = _response(inputs)
    response["ingredients"][0]["quantity_assumption"] = "One portion"
    response["ingredients"][0]["nutrients_per_100g"] = {}
    plan = _plan(inputs, response)
    inputs.authoring_fingerprint = "d" * 64
    nutrition, metadata = project_metadata(inputs, plan)
    assert nutrition.per_serving.kcal.value is None
    assert metadata.prep_minutes.value is None
    inputs.authoring_fingerprint = "a" * 64
    inputs.recipe.group_id = uuid4()
    assert project_metadata(inputs, plan)[0].per_serving.kcal.value is None


def test_generic_source_values_are_loaded_by_code_and_reread_on_improvement():
    from mealie.services.healthlabs.recipe_metadata_service import MetadataSourceCandidate, project_metadata

    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    candidate = MetadataSourceCandidate(
        id=uuid4(),
        release_id=uuid4(),
        name="Rice",
        interpretation="rice:cooked",
        nutrients_per_100g={"fibre_g": 3},
        source_record_hash="e" * 64,
        source_url="https://example.test/generic",
    )
    inputs.ingredients[0].candidates = [candidate]
    response = _response(inputs)
    response["ingredients"][0]["source_food_id"] = str(candidate.id)
    response["ingredients"][0]["nutrients_per_100g"] = {}
    plan = _plan(inputs, response)
    nutrition, _ = project_metadata(inputs, plan)
    assert nutrition.per_serving.fibre_g.value == 1.5
    assert nutrition.per_serving.fibre_g.estimate_methods == ["source_substitution"]
    candidate.nutrients_per_100g["fibre_g"] = 4
    assert project_metadata(inputs, plan)[0].per_serving.fibre_g.value == 2
    candidate.nutrients_per_100g["fibre_g"] = None
    assert project_metadata(inputs, plan)[0].per_serving.fibre_g.value is None
    inputs.ingredients[0].candidates = []
    assert project_metadata(inputs, plan)[0].per_serving.fibre_g.value is None


def test_empty_recipe_has_no_fabricated_zero_nutrients():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs()
    inputs.ingredients = []
    inputs.recipe.recipe_ingredient = []
    nutrition, _ = project_metadata(inputs)
    assert nutrition.complete is False
    assert nutrition.per_serving.kcal.value is None


def test_general_evidence_excludes_nutrition_only_model_assumptions():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    nutrition, metadata = project_metadata(inputs, _plan(inputs))
    assert any(e.kind == "model_estimate" for e in nutrition.provenance)
    assert not any(e.kind == "model_estimate" for e in metadata.provenance)
    assert {e.id for e in metadata.provenance} == {
        ref
        for item in (
            metadata.servings,
            metadata.prep_minutes,
            metadata.active_minutes,
            metadata.total_minutes,
            metadata.effort,
            metadata.plants,
        )
        for ref in item.provenance_ids
    }


def test_recorded_general_metadata_wins_and_valid_zero_minutes_is_retained():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    inputs.recipe.prep_time_seconds = 0
    inputs.recipe.perform_time_seconds = 600
    inputs.recipe.total_time_seconds = 1200
    inputs.recipe.extras = {"healthlabs.ephra": '{"ephraEffort2":true}'}
    nutrition, metadata = project_metadata(inputs)
    assert metadata.prep_minutes.recorded == 0
    assert metadata.active_minutes.value == 10
    assert metadata.total_minutes.value == 20
    assert metadata.effort.recorded == 2
    assert nutrition.servings_quality == "recorded"
    with pytest.raises(ValueError, match="recorded metadata"):
        _plan(inputs)


def test_accepted_alcohol_energy_is_not_rejected_by_a_four_four_nine_rule():
    inputs = _inputs(values={"kcal": 700, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fibre_g": 0})
    response = _response(inputs)
    response["ingredients"][0]["nutrients_per_100g"] = {}
    assert _plan(inputs, response).ingredients[0].nutrients_per_100g["kcal"] == 700


def test_severely_impossible_model_energy_is_rejected():
    inputs = _inputs(values={"kcal": None, "protein_g": None, "carbs_g": None, "fat_g": None, "fibre_g": None})
    response = _response(inputs)
    response["ingredients"][0]["nutrients_per_100g"] = {
        "kcal": 0,
        "protein_g": 0,
        "carbs_g": 0,
        "fat_g": 100,
        "fibre_g": 0,
    }
    with pytest.raises(ValueError, match="energy"):
        _plan(inputs, response)


def test_selected_source_portion_mass_is_computed_and_refreshed_in_code():
    from mealie.services.healthlabs.recipe_metadata_service import MetadataPortionCandidate, project_metadata

    inputs = _inputs(grams=None)
    portion = MetadataPortionCandidate(id=uuid4(), description="piece", amount=1, unit="piece", grams=80)
    inputs.ingredients[0].portions = [portion]
    inputs.ingredients[0].quantity = 2
    inputs.ingredients[0].unit_names = ("piece",)
    response = _response(inputs)
    response["ingredients"][0].update(
        effective_grams=160,
        quantity_assumption="Use two supplied pieces",
        portion_or_density_id=str(portion.id),
        nutrients_per_100g={},
    )
    plan = _plan(inputs, response)
    assert plan.ingredients[0].effective_grams == 160
    assert plan.ingredients[0].grams_quality == "estimated"
    assert project_metadata(inputs, plan)[0].per_serving.kcal.estimated == 96
    portion.grams = 100
    assert project_metadata(inputs, plan)[0].per_serving.kcal.estimated == 120
    inputs.ingredients[0].portions = []
    assert project_metadata(inputs, plan)[0].per_serving.kcal.value is None


def test_unknown_portion_or_false_mass_claim_is_rejected():
    from mealie.services.healthlabs.recipe_metadata_service import MetadataPortionCandidate

    inputs = _inputs(grams=None)
    portion = MetadataPortionCandidate(id=uuid4(), description="piece", amount=1, unit="piece", grams=80)
    inputs.ingredients[0].portions = [portion]
    inputs.ingredients[0].quantity = 2
    inputs.ingredients[0].unit_names = ("piece",)
    response = _response(inputs)
    response["ingredients"][0].update(
        effective_grams=160, quantity_assumption="Two pieces", portion_or_density_id=str(uuid4()), nutrients_per_100g={}
    )
    with pytest.raises(ValueError, match="portion"):
        _plan(inputs, response)
    response["ingredients"][0]["portion_or_density_id"] = str(portion.id)
    response["ingredients"][0]["effective_grams"] = 200
    with pytest.raises(ValueError, match="mass"):
        _plan(inputs, response)


def test_supplied_density_converts_recorded_ml_without_promoting_to_verified():
    from decimal import Decimal

    from mealie.services.healthlabs.recipe_metadata_service import MetadataPortionCandidate, project_metadata

    inputs = _inputs(grams=None)
    density_id = uuid4()
    inputs.ingredients[0].input_millilitres = Decimal("50")
    inputs.ingredients[0].portions = [
        MetadataPortionCandidate(
            id=uuid4(),
            description="ml",
            amount=100,
            unit="ml",
            grams=103,
            density_id=density_id,
            density_grams_per_ml=1.03,
        )
    ]
    response = _response(inputs)
    response["ingredients"][0].update(
        effective_grams=51.5,
        quantity_assumption="Use supplied food density",
        portion_or_density_id=str(density_id),
        nutrients_per_100g={},
    )
    plan = _plan(inputs, response)
    assert project_metadata(inputs, plan)[0].per_serving.kcal.estimated == 30.9


@pytest.mark.parametrize("preparation", ["cooked", None])
def test_known_raw_preparation_rejects_incompatible_or_unknown_supplied_source(preparation):
    from mealie.services.healthlabs.recipe_metadata_service import MetadataSourceCandidate

    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    inputs.ingredients[0].accepted_preparation_state = "raw"
    candidate = MetadataSourceCandidate(
        id=uuid4(),
        release_id=uuid4(),
        name="Rice",
        interpretation="rice:other",
        nutrients_per_100g={"fibre_g": 3},
        source_record_hash="e" * 64,
        source_url="https://example.test/source",
        preparation_state=preparation,
    )
    inputs.ingredients[0].candidates = [candidate]
    response = _response(inputs)
    response["ingredients"][0].update(source_food_id=str(candidate.id), nutrients_per_100g={})
    with pytest.raises(ValueError, match="preparation"):
        _plan(inputs, response)


def test_withdrawn_source_mass_is_not_reclassified_as_an_assumed_quantity():
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    plan = _plan(inputs)
    assert plan.ingredients[0].grams_quality == "source_conversion"
    assert plan.ingredients[0].quantity_assumption is None
    inputs.ingredients[0].resolved_grams = None
    inputs.input_fingerprint = "f" * 64
    nutrition, metadata = project_metadata(inputs, plan)
    assert nutrition.per_serving.kcal.value is None
    assert nutrition.per_serving.fibre_g.value is None
    assert metadata.prep_minutes.estimated == 5


@pytest.mark.parametrize("dimension", ["mass", "servings"])
def test_absurd_finite_model_dimensions_are_rejected(dimension):
    inputs = _inputs(grams=None if dimension == "mass" else 100, servings=0 if dimension == "servings" else 2)
    response = _response(inputs)
    response["ingredients"][0]["nutrients_per_100g"] = {}
    if dimension == "mass":
        response["ingredients"][0].update(effective_grams=1e100, quantity_assumption="Assume amount")
    else:
        response.update(effective_servings=1e-100, servings_assumption="Assume servings")
    with pytest.raises(ValueError, match=dimension):
        _plan(inputs, response)


def test_fractional_fixed_mass_accepts_its_float_echo_but_keeps_decimal_authority():
    from decimal import Decimal

    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    grams = Decimal("9.4498410416666657216825625")
    inputs = _inputs(grams=grams, values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    response = _response(inputs)
    response["ingredients"][0]["effective_grams"] = float(grams)
    plan = _plan(inputs, response)
    assert inputs.ingredients[0].resolved_grams == grams
    assert project_metadata(inputs, plan)[0].per_serving.kcal.verified == 5.67
    response["ingredients"][0]["effective_grams"] += 0.01
    with pytest.raises(ValueError, match="source-backed mass"):
        _plan(inputs, response)


@pytest.mark.parametrize("missing_protein", [False, True])
def test_accepted_zero_energy_polyol_facts_do_not_require_a_source_rewrite(missing_protein):
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(
        values={"kcal": 0, "protein_g": None if missing_protein else 0, "carbs_g": 100, "fat_g": 0, "fibre_g": 0}
    )
    response = _response(inputs)
    response["ingredients"][0]["nutrients_per_100g"] = {"protein_g": 0} if missing_protein else {}
    plan = _plan(inputs, response)
    nutrition, _ = project_metadata(inputs, plan)
    assert nutrition.per_serving.kcal.verified == 0
    assert nutrition.per_serving.carbs_g.verified == 50
    assert nutrition.per_serving.protein_g.value == 0


def test_large_trusted_mass_remains_representable_without_decimal_quantization_failure():
    from decimal import Decimal

    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(grams=Decimal("1e100"))
    nutrition, _ = project_metadata(inputs)
    assert nutrition.per_serving.kcal.verified == pytest.approx(6e99)


def test_unrepresentable_scaled_source_values_are_unavailable_without_raising():
    from decimal import Decimal

    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    inputs = _inputs(grams=Decimal("1e308"), servings=1e-308)
    nutrition, _ = project_metadata(inputs)
    assert nutrition.per_serving.kcal.value is None
    assert nutrition.per_serving.kcal.verified is None
    assert nutrition.per_serving.kcal.quality == "unavailable"


def test_plan_rejects_overflow_after_combining_current_source_mass_with_assumed_servings():
    from decimal import Decimal

    inputs = _inputs(grams=Decimal("1e308"), servings=0)
    response = _response(inputs)
    response.update(effective_servings=0.1, servings_assumption="One tenth portion")
    response["ingredients"][0].update(effective_grams=1e308, nutrients_per_100g={})
    with pytest.raises(ValueError, match="unrepresentable"):
        _plan(inputs, response)


def test_original_preparation_constraint_controls_supplied_candidates_and_reuse():
    from mealie.services.healthlabs.recipe_metadata_service import MetadataSourceCandidate, project_metadata

    inputs = _inputs()
    fact = inputs.ingredients[0]
    fact.accepted_source_food_id = None
    fact.interpretation = None
    fact.nutrients_per_100g = {}
    candidate = MetadataSourceCandidate(
        id=uuid4(),
        release_id=uuid4(),
        name="Rice",
        interpretation="rice:raw",
        nutrients_per_100g={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2, "fibre_g": 3},
        source_record_hash="e" * 64,
        source_url="https://example.test/source",
        preparation_state="raw",
    )
    fact.candidates = [candidate]
    response = _response(inputs)
    response["ingredients"][0].update(source_food_id=str(candidate.id), nutrients_per_100g={})
    plan = _plan(inputs, response)
    fact.preparation_constraint = "cooked"
    with pytest.raises(ValueError, match="preparation"):
        _plan(inputs, response)
    assert project_metadata(inputs, plan)[0].per_serving.fibre_g.value is None


@pytest.mark.parametrize("missing_macro", ["fat_g", "protein_g", "carbs_g"])
def test_fixed_energy_rejects_impossible_model_completed_macros(missing_macro):
    values = {"kcal": 900, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fibre_g": 0}
    values[missing_macro] = None
    inputs = _inputs(values=values)
    response = _response(inputs)
    response["ingredients"][0]["nutrients_per_100g"] = {missing_macro: 0}
    with pytest.raises(ValueError, match="energy"):
        _plan(inputs, response)


@pytest.mark.parametrize("missing_macro", ["fat_g", "protein_g", "carbs_g"])
def test_fixed_energy_allows_bounded_unrepresented_alcohol_mass(missing_macro):
    from mealie.services.healthlabs.recipe_metadata_service import project_metadata

    values = {"kcal": 700, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fibre_g": 0}
    values[missing_macro] = None
    inputs = _inputs(values=values)
    response = _response(inputs)
    response["ingredients"][0]["nutrients_per_100g"] = {missing_macro: 0}
    nutrition, _ = project_metadata(inputs, _plan(inputs, response))
    assert nutrition.per_serving.kcal.verified == 350
    assert getattr(nutrition.per_serving, missing_macro).estimated == 0


def test_wholly_fixed_source_energy_is_not_subject_to_model_plausibility():
    inputs = _inputs(values={"kcal": 900, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fibre_g": 0})
    response = _response(inputs)
    response["ingredients"][0]["nutrients_per_100g"] = {}
    assert _plan(inputs, response).ingredients[0].model_nutrient_keys == []
