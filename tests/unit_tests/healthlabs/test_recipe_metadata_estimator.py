"""The transport adapter selects evidence; the approved validator computes mass."""

import asyncio
import json
from uuid import uuid4

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
