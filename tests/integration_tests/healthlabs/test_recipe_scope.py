from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.models.healthlabs import HealthLabsCapabilityGrant
from mealie.db.models.users import User
from mealie.schema.healthlabs.identity import HealthLabsCapability
from tests.utils import TestUser, api_routes
from tests.utils.factories import random_string

pytestmark = pytest.mark.usefixtures("allow_managed_upstream_for_service_policy_tests")


def _configure_managed(
    user: TestUser,
    *capabilities: HealthLabsCapability,
    admin: bool = False,
) -> None:
    session = user.repos.session
    model = session.get(User, user.user_id)
    assert model is not None
    if model.external_subject_id is None:
        model.external_subject_id = f"healthlabs-{uuid4()}"
    model.healthlabs_managed = True
    model.admin = admin
    session.execute(sa.delete(HealthLabsCapabilityGrant).where(HealthLabsCapabilityGrant.user_id == user.user_id))
    session.add_all(
        [
            HealthLabsCapabilityGrant(
                user_id=user.user_id,
                capability=capability.value,
                reason="scope integration test",
            )
            for capability in capabilities
        ]
    )
    session.commit()


def test_managed_user_requires_write_own_to_create_personal_recipe(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
) -> None:
    _configure_managed(unique_user_fn_scoped)
    denied = api_client.post(
        api_routes.recipes,
        json={"name": random_string()},
        headers=unique_user_fn_scoped.token,
    )
    assert denied.status_code == 403

    _configure_managed(unique_user_fn_scoped, HealthLabsCapability.RECIPES_WRITE_OWN)
    name = random_string()
    created = api_client.post(api_routes.recipes, json={"name": name}, headers=unique_user_fn_scoped.token)
    assert created.status_code == 201
    recipe = api_client.get(api_routes.recipes_slug(name), headers=unique_user_fn_scoped.token).json()
    assert recipe["healthlabsScope"] == "personal"
    assert recipe["userId"] == str(unique_user_fn_scoped.user_id)

    _configure_managed(
        unique_user_fn_scoped,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.CATALOGUE_WRITE,
    )
    recipe["healthlabsScope"] = "catalogue"
    immutable = api_client.put(api_routes.recipes_slug(name), json=recipe, headers=unique_user_fn_scoped.token)
    assert immutable.status_code == 403
    unchanged = api_client.get(api_routes.recipes_slug(name), headers=unique_user_fn_scoped.token).json()
    assert unchanged["healthlabsScope"] == "personal"


def test_managed_personal_recipe_is_writable_only_by_its_owner(
    api_client: TestClient,
    user_tuple: list[TestUser],
) -> None:
    owner, collaborator = user_tuple
    _configure_managed(owner, HealthLabsCapability.RECIPES_WRITE_OWN)
    _configure_managed(collaborator, HealthLabsCapability.RECIPES_WRITE_OWN)
    name = random_string()
    created = api_client.post(api_routes.recipes, json={"name": name}, headers=owner.token)
    assert created.status_code == 201

    recipe = api_client.get(api_routes.recipes_slug(name), headers=collaborator.token).json()
    recipe["description"] = "cross-owner mutation"
    denied = api_client.put(api_routes.recipes_slug(name), json=recipe, headers=collaborator.token)
    assert denied.status_code == 403

    owner_recipe = api_client.get(api_routes.recipes_slug(name), headers=owner.token).json()
    owner_recipe["description"] = "owner mutation"
    allowed = api_client.put(api_routes.recipes_slug(name), json=owner_recipe, headers=owner.token)
    assert allowed.status_code == 200


def test_catalogue_recipe_requires_catalogue_write_even_for_admin(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
) -> None:
    _configure_managed(
        unique_user_fn_scoped,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.CATALOGUE_READ,
        admin=True,
    )
    name = random_string()
    denied_create = api_client.post(
        api_routes.recipes,
        json={"name": name, "healthlabsScope": "catalogue"},
        headers=unique_user_fn_scoped.token,
    )
    assert denied_create.status_code == 403

    _configure_managed(
        unique_user_fn_scoped,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.CATALOGUE_READ,
        HealthLabsCapability.CATALOGUE_WRITE,
        admin=True,
    )
    created = api_client.post(
        api_routes.recipes,
        json={"name": name, "healthlabsScope": "catalogue"},
        headers=unique_user_fn_scoped.token,
    )
    assert created.status_code == 201
    recipe = api_client.get(api_routes.recipes_slug(name), headers=unique_user_fn_scoped.token).json()
    assert recipe["healthlabsScope"] == "catalogue"

    _configure_managed(
        unique_user_fn_scoped,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.CATALOGUE_READ,
        admin=True,
    )
    recipe["description"] = "admin shortcut must not work"
    denied_update = api_client.put(api_routes.recipes_slug(name), json=recipe, headers=unique_user_fn_scoped.token)
    assert denied_update.status_code == 403

    _configure_managed(unique_user_fn_scoped, HealthLabsCapability.CATALOGUE_WRITE, admin=False)
    allowed = api_client.put(api_routes.recipes_slug(name), json=recipe, headers=unique_user_fn_scoped.token)
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    ("route", "payload_key"),
    [
        (api_routes.recipes_bulk_actions_tag, "tags"),
        (api_routes.recipes_bulk_actions_categorize, "categories"),
        (api_routes.recipes_bulk_actions_settings, "settings"),
    ],
)
def test_catalogue_recipe_cannot_bypass_capabilities_through_bulk_mutations(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
    route: str,
    payload_key: str,
) -> None:
    _configure_managed(unique_user_fn_scoped, HealthLabsCapability.CATALOGUE_WRITE)
    name = random_string()
    created = api_client.post(
        api_routes.recipes,
        json={"name": name, "healthlabsScope": "catalogue"},
        headers=unique_user_fn_scoped.token,
    )
    assert created.status_code == 201
    recipe = api_client.get(api_routes.recipes_slug(name), headers=unique_user_fn_scoped.token).json()

    _configure_managed(unique_user_fn_scoped, admin=True)
    value = recipe["settings"] if payload_key == "settings" else []
    response = api_client.post(
        route,
        json={"recipes": [name], payload_key: value},
        headers=unique_user_fn_scoped.token,
    )
    assert response.status_code == 403
