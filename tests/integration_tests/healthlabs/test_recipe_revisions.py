import hashlib
import json
from copy import deepcopy
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import select

from mealie.db.models.healthlabs import HealthLabsCapabilityGrant
from mealie.db.models.recipe.recipe import HealthLabsRecipeRevision
from mealie.db.models.users import User
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.recipe.recipe_ingredient import SaveIngredientFood, SaveIngredientUnit
from tests.utils import TestUser, api_routes
from tests.utils.factories import random_string

pytestmark = pytest.mark.usefixtures("allow_managed_upstream_for_service_policy_tests")


def _configure_managed(user: TestUser, *capabilities: HealthLabsCapability) -> None:
    session = user.repos.session
    model = session.get(User, user.user_id)
    assert model is not None
    if model.external_subject_id is None:
        model.external_subject_id = f"healthlabs-{uuid4()}"
    model.healthlabs_managed = True
    session.execute(sa.delete(HealthLabsCapabilityGrant).where(HealthLabsCapabilityGrant.user_id == user.user_id))
    session.add_all(
        [
            HealthLabsCapabilityGrant(
                user_id=user.user_id,
                capability=capability.value,
                reason="revision integration test",
            )
            for capability in capabilities
        ]
    )
    session.commit()


def _revisions(user: TestUser, recipe_id: str) -> list[HealthLabsRecipeRevision]:
    return list(
        user.repos.session.scalars(
            select(HealthLabsRecipeRevision)
            .where(HealthLabsRecipeRevision.recipe_id == recipe_id)
            .order_by(HealthLabsRecipeRevision.revision_number)
        )
    )


def test_recipe_revisions_are_monotonic_immutable_and_skip_equivalent_retry(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
) -> None:
    _configure_managed(unique_user_fn_scoped, HealthLabsCapability.RECIPES_WRITE_OWN)
    name = random_string()
    response = api_client.post(api_routes.recipes, json={"name": name}, headers=unique_user_fn_scoped.token)
    assert response.status_code == 201
    recipe_url = api_routes.recipes_slug(name)
    recipe = api_client.get(recipe_url, headers=unique_user_fn_scoped.token).json()

    assert recipe["healthlabsRevisionNumber"] == 1
    revisions = _revisions(unique_user_fn_scoped, recipe["id"])
    assert len(revisions) == 1
    first_snapshot = deepcopy(revisions[0].snapshot)
    assert first_snapshot["recipe_ingredient"]
    assert first_snapshot["recipe_instructions"]
    assert "tags" in first_snapshot
    assert "image" in first_snapshot
    assert (
        revisions[0].content_sha256
        == hashlib.sha256(
            json.dumps(first_snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    )

    equivalent = api_client.put(recipe_url, json=recipe, headers=unique_user_fn_scoped.token)
    assert equivalent.status_code == 200
    equivalent_revisions = _revisions(unique_user_fn_scoped, recipe["id"])
    assert equivalent_revisions[0].snapshot == equivalent_revisions[-1].snapshot
    assert equivalent.json()["healthlabsRevisionNumber"] == 1
    assert len(_revisions(unique_user_fn_scoped, recipe["id"])) == 1

    food = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(name="Tomato", group_id=unique_user_fn_scoped.group_id)
    )
    unit = unique_user_fn_scoped.repos.ingredient_units.create(
        SaveIngredientUnit(name="gram", abbreviation="g", group_id=unique_user_fn_scoped.group_id)
    )
    changed = equivalent.json()
    changed["description"] = "Revision two"
    changed["extras"] = {"healthlabs.ephra": "source-v1", "private-note": "must not leak"}
    changed["recipeIngredient"] = [
        {
            "quantity": 125,
            "food": {"id": str(food.id), "name": food.name},
            "unit": {"id": str(unit.id), "name": unit.name, "abbreviation": unit.abbreviation},
            "note": "ripe",
            "originalText": "125 g ripe tomato",
        }
    ]
    updated = api_client.put(recipe_url, json=changed, headers=unique_user_fn_scoped.token)
    assert updated.status_code == 200
    assert updated.json()["healthlabsRevisionNumber"] == 2

    revisions = _revisions(unique_user_fn_scoped, recipe["id"])
    assert [row.revision_number for row in revisions] == [1, 2]
    assert revisions[0].snapshot == first_snapshot
    assert revisions[0].content_sha256 != revisions[1].content_sha256
    assert revisions[1].snapshot["recipe_ingredient"][0]["food"]["id"] == str(food.id)
    assert revisions[1].snapshot["recipe_ingredient"][0]["unit"]["id"] == str(unit.id)
    assert revisions[1].snapshot["extras"] == {"healthlabs.ephra": "source-v1"}


def test_catalogue_copy_pins_source_revision_and_is_not_overwritten(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
) -> None:
    _configure_managed(
        unique_user_fn_scoped,
        HealthLabsCapability.CATALOGUE_READ,
        HealthLabsCapability.CATALOGUE_WRITE,
        HealthLabsCapability.RECIPES_WRITE_OWN,
    )
    source_name = random_string()
    created = api_client.post(
        api_routes.recipes,
        json={"name": source_name, "healthlabsScope": "catalogue"},
        headers=unique_user_fn_scoped.token,
    )
    assert created.status_code == 201
    source = api_client.get(api_routes.recipes_slug(source_name), headers=unique_user_fn_scoped.token).json()
    source_revision = _revisions(unique_user_fn_scoped, source["id"])[0]

    copy_name = random_string()
    copied = api_client.post(
        api_routes.recipes_slug_duplicate(source_name),
        json={"name": copy_name},
        headers=unique_user_fn_scoped.token,
    )
    assert copied.status_code == 201
    copy_recipe = copied.json()
    assert copy_recipe["healthlabsScope"] == "personal"
    assert copy_recipe["copiedFromRecipeId"] == source["id"]
    assert copy_recipe["copiedFromRevisionId"] == str(source_revision.id)
    assert copy_recipe["copiedAt"] is not None
    assert copy_recipe["healthlabsRevisionNumber"] == 1

    forged_copy = deepcopy(copy_recipe)
    forged_copy["copiedFromRevisionId"] = str(uuid4())
    immutable = api_client.put(
        api_routes.recipes_slug(copy_name),
        json=forged_copy,
        headers=unique_user_fn_scoped.token,
    )
    assert immutable.status_code == 403

    source["description"] = "catalogue changed later"
    updated_source = api_client.put(
        api_routes.recipes_slug(source_name),
        json=source,
        headers=unique_user_fn_scoped.token,
    )
    assert updated_source.status_code == 200
    copy_after_source_update = api_client.get(
        api_routes.recipes_slug(copy_name),
        headers=unique_user_fn_scoped.token,
    ).json()
    assert copy_after_source_update["description"] != "catalogue changed later"
    assert copy_after_source_update["copiedFromRevisionId"] == str(source_revision.id)
    assert len(_revisions(unique_user_fn_scoped, copy_recipe["id"])) == 1
