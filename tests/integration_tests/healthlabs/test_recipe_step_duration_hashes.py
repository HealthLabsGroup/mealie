"""Upstream added `duration_seconds` to recipe steps. A step without one must hash as it did before."""

from uuid import uuid4

from mealie.schema.recipe import RecipeStep
from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService
from mealie.services.healthlabs.revision_service import RecipeRevisionService
from tests.integration_tests.healthlabs.test_recipe_metadata_reads import _recipe
from tests.utils import TestUser


def _with_step(recipe, duration_seconds: int | None):
    step = RecipeStep(id=uuid4(), text="Simmer", duration_seconds=duration_seconds)
    return recipe.model_copy(update={"recipe_instructions": [step]})


def test_unset_step_duration_is_left_out_of_the_revision_snapshot(unique_user_fn_scoped: TestUser):
    recipe, _ = _recipe(unique_user_fn_scoped)

    unset = RecipeRevisionService.build_snapshot(_with_step(recipe, None))
    timed = RecipeRevisionService.build_snapshot(_with_step(recipe, 300))

    assert "duration_seconds" not in unset["recipe_instructions"][0]
    assert timed["recipe_instructions"][0]["duration_seconds"] == 300
    assert RecipeRevisionService.canonical_json(unset) != RecipeRevisionService.canonical_json(timed)


def test_unset_step_duration_is_left_out_of_the_authoring_fingerprint(unique_user_fn_scoped: TestUser):
    recipe, _ = _recipe(unique_user_fn_scoped)
    service = RecipeMetadataService(unique_user_fn_scoped.repos.session)

    unset = service.inputs(_with_step(recipe, None))
    timed = service.inputs(_with_step(recipe, 300))

    assert "duration_seconds" not in unset.authoring_context["instructions"][0]
    assert timed.authoring_context["instructions"][0]["duration_seconds"] == 300
    assert unset.authoring_fingerprint != timed.authoring_fingerprint
