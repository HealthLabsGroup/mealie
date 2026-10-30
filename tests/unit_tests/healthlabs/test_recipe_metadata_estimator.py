"""The transport adapter selects evidence; the approved validator computes mass."""

import asyncio
import json
from uuid import uuid4

import pytest

from mealie.services.healthlabs.recipe_metadata_estimator import MetadataTransportResponse, RecipeMetadataEstimator
from mealie.services.healthlabs.recipe_metadata_service import MetadataPortionCandidate
from tests.unit_tests.healthlabs.test_recipe_metadata_projection import _inputs


def test_selected_portion_mass_is_computed_without_model_echo():
    inputs = _inputs(grams=None)
    fact = inputs.ingredients[0]
    portion_id = uuid4()
    fact.quantity = 2
    fact.unit_names = ("slice",)
    fact.portions = [MetadataPortionCandidate(id=portion_id, description="slice", amount=1, unit="slice", grams=25)]
    from mealie.services.healthlabs.nutrition_provider_resolution import ResolvedCuratorProvider

    provider = ResolvedCuratorProvider(
        group_id=inputs.recipe.group_id,
        identity_id=uuid4(),
        audit_provider_id=None,
        name="offline",
        base_url="https://offline.test",
        model="offline",
        timeout=30,
        request_headers=(),
        request_params=(),
        _credential=lambda: "offline",
    )

    async def request(service, provider, message, schema, limits):
        context = json.loads(message)
        if context["phase"] == "ingredients":
            return MetadataTransportResponse.model_validate(
                {
                    "ingredients": [
                        {
                            "reference_id": str(fact.reference_id),
                            "effective_grams": None,
                            "quantity_assumption": "Use the supplied slice conversion",
                            "source_food_id": None,
                            "portion_or_density_id": str(portion_id),
                            "assumed_portion_amount": None,
                            "nutrients_per_100g": dict.fromkeys(
                                ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "water_ml")
                            ),
                            "rationale": "Recorded two slices",
                        }
                    ],
                    "effective_servings": None,
                    "servings_assumption": None,
                    "general_metadata": None,
                }
            )
        return MetadataTransportResponse.model_validate(
            {
                "ingredients": [],
                "effective_servings": None,
                "servings_assumption": None,
                "general_metadata": {
                    "total_minutes": 20,
                    "prep_minutes": 5,
                    "active_minutes": 15,
                    "effort": 1,
                    "rationales": dict.fromkeys(
                        ("total_minutes", "prep_minutes", "active_minutes", "effort"), "One pot"
                    ),
                    "plants": [],
                    "plant_ingredient_references": [str(fact.reference_id)],
                    "plants_rationale": "Reviewed all ingredients",
                },
            }
        )

    plan = asyncio.run(RecipeMetadataEstimator().estimate(inputs, provider, None, request=request))
    assert plan.ingredients[0].effective_grams == 50
    assert plan.ingredients[0].portion_or_density_id == portion_id


def _nullable_candidate_case(fixed, candidate_values):
    from mealie.services.healthlabs.nutrition_provider_resolution import ResolvedCuratorProvider
    from mealie.services.healthlabs.recipe_metadata_service import REQUIRED_NUTRIENTS, MetadataSourceCandidate

    inputs = _inputs()
    fact = inputs.ingredients[0]
    fact.nutrients_per_100g = fixed
    fact.candidates = [
        MetadataSourceCandidate(
            id=uuid4(),
            release_id=uuid4(),
            name="Example food",
            interpretation="example",
            nutrients_per_100g=candidate_values,
            source_record_hash="d" * 64,
            source_url="https://example.test/food",
        )
    ]
    inputs.authoring_context = {
        "name": "Example recipe",
        "ingredients": [{"reference_id": str(fact.reference_id), "note": "100 g example food"}],
    }
    provider = ResolvedCuratorProvider(
        group_id=inputs.recipe.group_id,
        identity_id=uuid4(),
        audit_provider_id=None,
        name="offline",
        base_url="https://offline.test",
        model="offline",
        timeout=30,
        request_headers=(),
        request_params=(),
        _credential=lambda: "offline",
    )
    response = {
        "ingredients": [
            {
                "reference_id": str(fact.reference_id),
                "effective_grams": None,
                "quantity_assumption": None,
                "source_food_id": str(fact.candidates[0].id),
                "portion_or_density_id": None,
                "assumed_portion_amount": None,
                "nutrients_per_100g": dict.fromkeys((*REQUIRED_NUTRIENTS, "water_ml")),
                "rationale": "Use candidate values and estimate missing nutrients",
            }
        ],
        "effective_servings": None,
        "servings_assumption": None,
        "general_metadata": None,
    }
    return inputs, provider, response


def _general_response(inputs):
    return {
        "ingredients": [],
        "effective_servings": None,
        "servings_assumption": None,
        "general_metadata": {
            "total_minutes": 20,
            "prep_minutes": 5,
            "active_minutes": 15,
            "effort": 1,
            "rationales": dict.fromkeys(
                ("total_minutes", "prep_minutes", "active_minutes", "effort"), "Simple preparation"
            ),
            "plants": [],
            "plant_ingredient_references": [str(f.reference_id) for f in inputs.ingredients],
            "plants_rationale": "All references reviewed",
        },
    }


def test_all_null_selected_candidate_requires_each_core_nutrient_and_targeted_correction():
    from mealie.services.healthlabs.recipe_metadata_service import REQUIRED_NUTRIENTS

    inputs, provider, response = _nullable_candidate_case({}, dict.fromkeys(REQUIRED_NUTRIENTS))
    messages = []

    async def request(service, provider, message, schema, limits):
        context = json.loads(message)
        messages.append(context)
        if context["phase"] == "general":
            return schema.model_validate(_general_response(inputs))
        if len(messages) == 2:
            assert context["correction"] == {
                "code": "missing_required_nutrients",
                "reference_id": str(inputs.ingredients[0].reference_id),
                "fields": [f"nutrients_per_100g.{k}" for k in REQUIRED_NUTRIENTS],
            }
            response["ingredients"][0]["nutrients_per_100g"].update(
                kcal=120, protein_g=4, carbs_g=22, fat_g=2, fibre_g=0
            )
        return schema.model_validate(response)

    plan = asyncio.run(RecipeMetadataEstimator().estimate(inputs, provider, None, request=request))
    assert len(messages) == 3
    assert messages[0]["ingredients"][0]["candidates"][0]["missing_required_nutrients"] == list(REQUIRED_NUTRIENTS)
    assert set(plan.ingredients[0].model_nutrient_keys) == set(REQUIRED_NUTRIENTS)
    assert not inputs.missing_fields(plan)


def test_partial_candidate_and_fixed_zero_are_preserved_while_missing_values_are_estimated():
    from mealie.services.healthlabs.recipe_metadata_service import REQUIRED_NUTRIENTS

    inputs, provider, response = _nullable_candidate_case(
        {"fat_g": 0}, {"kcal": 100, "protein_g": 4, "carbs_g": None, "fibre_g": None}
    )
    response["ingredients"][0]["nutrients_per_100g"].update(carbs_g=21, fibre_g=0)
    messages = []

    async def request(service, provider, message, schema, limits):
        context = json.loads(message)
        messages.append(context)
        return schema.model_validate(response if context["phase"] == "ingredients" else _general_response(inputs))

    plan = asyncio.run(RecipeMetadataEstimator().estimate(inputs, provider, None, request=request))
    assert len(messages) == 2
    context = messages[0]["ingredients"][0]
    assert context["missing_required_nutrients"] == [k for k in REQUIRED_NUTRIENTS if k != "fat_g"]
    assert context["candidates"][0]["missing_required_nutrients"] == ["carbs_g", "fat_g", "fibre_g"]
    assert plan.ingredients[0].nutrients_per_100g == {
        "kcal": 100,
        "protein_g": 4,
        "carbs_g": 21,
        "fat_g": 0,
        "fibre_g": 0,
        "water_ml": None,
    }
    assert set(plan.ingredients[0].model_nutrient_keys) == {"carbs_g", "fibre_g"}
    assert inputs.ingredients[0].nutrients_per_100g == {"fat_g": 0}
    assert inputs.ingredients[0].candidates[0].nutrients_per_100g["carbs_g"] is None


@pytest.mark.parametrize("rewrite", ["fixed", "candidate"])
def test_nutrient_feedback_never_accepts_source_rewrites(rewrite):
    from mealie.services.healthlabs.recipe_metadata_estimator import InvalidMetadataPlan

    inputs, provider, response = _nullable_candidate_case(
        {"fat_g": 0}, {"kcal": 100, "protein_g": 4, "carbs_g": 21, "fibre_g": 0}
    )
    response["ingredients"][0]["nutrients_per_100g"]["fat_g" if rewrite == "fixed" else "kcal"] = 1
    calls = []

    async def request(service, provider, message, schema, limits):
        calls.append(json.loads(message))
        return schema.model_validate(response)

    with pytest.raises(InvalidMetadataPlan, match="invalid_plan_values_or_source_rewrite"):
        asyncio.run(RecipeMetadataEstimator().estimate(inputs, provider, None, request=request))
    assert len(calls) == 2
    assert inputs.ingredients[0].nutrients_per_100g == {"fat_g": 0}
    assert inputs.ingredients[0].candidates[0].nutrients_per_100g["kcal"] == 100


def test_nullable_candidate_rejection_stays_bounded_and_feedback_contains_no_provider_text(caplog):
    from mealie.services.healthlabs.recipe_metadata_estimator import InvalidMetadataPlan
    from mealie.services.healthlabs.recipe_metadata_service import REQUIRED_NUTRIENTS

    inputs, provider, response = _nullable_candidate_case({}, dict.fromkeys(REQUIRED_NUTRIENTS))
    response["ingredients"][0]["rationale"] = "PRIVATE_RESPONSE_SENTINEL"
    calls = []

    async def request(service, provider, message, schema, limits):
        calls.append(json.loads(message))
        return schema.model_validate(response)

    with pytest.raises(InvalidMetadataPlan, match="^missing_required_nutrients$"):
        asyncio.run(RecipeMetadataEstimator().estimate(inputs, provider, None, request=request))
    assert len(calls) == 2
    assert calls[1]["correction"] == {
        "code": "missing_required_nutrients",
        "reference_id": str(inputs.ingredients[0].reference_id),
        "fields": [f"nutrients_per_100g.{k}" for k in REQUIRED_NUTRIENTS],
    }
    assert "PRIVATE_RESPONSE_SENTINEL" not in json.dumps(calls)
    assert "PRIVATE_RESPONSE_SENTINEL" not in caplog.text
