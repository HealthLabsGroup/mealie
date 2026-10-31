"""A list join's association order must not hide unchanged READY evidence."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mealie.schema.recipe import RecipeStep
from mealie.schema.recipe.recipe_step import IngredientReferences
from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService, fingerprint
from tests.unit_tests.healthlabs.test_recipe_metadata_projection import _inputs, _plan


def _projection(monkeypatch):
    original = _inputs(values={"kcal": 120, "protein_g": 4, "carbs_g": 22, "fat_g": 2})
    first, second = uuid4(), uuid4()
    original.recipe.recipe_instructions = [
        RecipeStep(
            text="Cook the ingredients",
            ingredient_references=[IngredientReferences(reference_id=first), IngredientReferences(reference_id=second)],
        )
    ]
    original.authoring_context = {
        "id": str(original.recipe.id),
        "group_id": str(original.recipe.group_id),
        "name": original.recipe.name,
        "ingredients": [{"reference_id": str(original.ingredients[0].reference_id)}],
        "instructions": [
            {
                "text": "Cook the ingredients",
                "ingredient_references": [{"reference_id": str(first)}, {"reference_id": str(second)}],
            }
        ],
        "extras": {},
    }
    original.authoring_fingerprint = fingerprint(original.authoring_context)
    fallback = deepcopy(original)
    fallback.recipe.recipe_instructions[0].ingredient_references.reverse()
    fallback.authoring_context["instructions"][0]["ingredient_references"].reverse()
    fallback.authoring_fingerprint = fingerprint(fallback.authoring_context)
    fallback.input_fingerprint = "d" * 64
    plan = _plan(fallback)
    service = RecipeMetadataService(None)
    monkeypatch.setattr(service, "inputs", lambda recipe: fallback if recipe is fallback.recipe else original)
    monkeypatch.setattr(
        service,
        "_ready_evidence",
        lambda inputs: iter([(SimpleNamespace(id=uuid4()), plan)]) if inputs is fallback else iter(()),
    )
    return service, original, fallback


def test_reference_order_fallback_reuses_existing_evidence_without_mutating_authoring(monkeypatch):
    service, original, fallback = _projection(monkeypatch)
    before = deepcopy(original.authoring_context)
    displayed = [ref.reference_id for ref in original.recipe.recipe_instructions[0].ingredient_references]
    original_fp = original.authoring_fingerprint
    calls = []
    nutrition, _ = service.project(original.recipe, reload_recipe=lambda: calls.append(True) or fallback.recipe)
    assert nutrition.per_serving.fibre_g.estimated == 1.5
    assert nutrition.input_fingerprint == fallback.input_fingerprint
    assert len(calls) == 1
    assert original.authoring_context == before
    assert original.authoring_fingerprint == original_fp
    assert [ref.reference_id for ref in original.recipe.recipe_instructions[0].ingredient_references] == displayed


@pytest.mark.parametrize(
    "change",
    ["content", "add_ref", "remove_ref", "duplicate", "step_order", "ingredient_order", "extras", "group", "recipe"],
)
def test_reference_order_fallback_rejects_any_other_authoring_change(monkeypatch, change):
    service, original, fallback = _projection(monkeypatch)
    if change == "content":
        fallback.authoring_context["instructions"][0]["text"] = "Changed instructions"
    elif change == "add_ref":
        fallback.authoring_context["instructions"][0]["ingredient_references"].append({"reference_id": str(uuid4())})
    elif change == "remove_ref":
        fallback.authoring_context["instructions"][0]["ingredient_references"].pop()
    elif change == "duplicate":
        refs = fallback.authoring_context["instructions"][0]["ingredient_references"]
        refs[0] = deepcopy(refs[1])
    elif change == "step_order":
        extra = {"text": "Serve", "ingredient_references": []}
        original.authoring_context["instructions"].append(extra)
        fallback.authoring_context["instructions"].insert(0, deepcopy(extra))
    elif change == "ingredient_order":
        extra = {"reference_id": str(uuid4())}
        original.authoring_context["ingredients"].append(extra)
        fallback.authoring_context["ingredients"].insert(0, deepcopy(extra))
    elif change == "extras":
        original.authoring_context["extras"] = {"ingredient_references": ["first", "second"]}
        fallback.authoring_context["extras"] = {"ingredient_references": ["second", "first"]}
    elif change == "group":
        fallback.recipe.group_id = uuid4()
    else:
        fallback.recipe.id = uuid4()
    nutrition, _ = service.project(original.recipe, reload_recipe=lambda: fallback.recipe)
    assert nutrition.per_serving.fibre_g.value is None
    assert nutrition.input_fingerprint == original.input_fingerprint


def test_current_ready_evidence_does_not_reload(monkeypatch):
    service, original, fallback = _projection(monkeypatch)
    monkeypatch.setattr(service, "_ready_evidence", lambda inputs: iter([(SimpleNamespace(), _plan(original))]))
    nutrition, _ = service.project(original.recipe, reload_recipe=lambda: pytest.fail("current READY must not reload"))
    assert nutrition.per_serving.fibre_g.estimated == 1.5


def test_reference_order_comparison_preserves_duplicate_multiplicity(monkeypatch):
    service, original, fallback = _projection(monkeypatch)
    duplicate = deepcopy(original.authoring_context["instructions"][0]["ingredient_references"][0])
    original.authoring_context["instructions"][0]["ingredient_references"].append(duplicate)
    fallback.authoring_context["instructions"][0]["ingredient_references"].insert(0, deepcopy(duplicate))
    nutrition, _ = service.project(original.recipe, reload_recipe=lambda: fallback.recipe)
    assert nutrition.per_serving.fibre_g.estimated == 1.5


@pytest.mark.parametrize("mode", ["no_callback", "one_ref", "missing_recipe", "no_evidence", "same_fingerprint"])
def test_unavailable_and_snapshot_reads_keep_original_projection(monkeypatch, mode):
    service, original, fallback = _projection(monkeypatch)

    def callback():
        if mode == "one_ref":
            pytest.fail("single reference cannot have a permutation")
        return None if mode == "missing_recipe" else fallback.recipe

    if mode == "one_ref":
        original.recipe.recipe_instructions[0].ingredient_references.pop()
    elif mode == "no_evidence":
        monkeypatch.setattr(service, "_ready_evidence", lambda inputs: iter(()))
    elif mode == "same_fingerprint":
        fallback.authoring_fingerprint = original.authoring_fingerprint
    nutrition, _ = service.project(original.recipe, reload_recipe=None if mode == "no_callback" else callback)
    assert nutrition.per_serving.fibre_g.value is None
    assert nutrition.input_fingerprint == original.input_fingerprint
