from collections.abc import Callable, Generator
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Depends, Request
from fastapi.testclient import TestClient
from pytest import MonkeyPatch, fixture

from mealie.app import app
from mealie.core.config import get_app_settings
from mealie.core.dependencies.dependencies import get_auth_token, get_current_user
from mealie.db.db_setup import generate_session
from mealie.db.models.healthlabs import (
    HealthLabsCapabilityGrant,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.db.models.users import User
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability


def _healthlabs_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": "/api/healthlabs/v1/service-policy-test",
            "raw_path": b"/api/healthlabs/v1/service-policy-test",
            "query_string": b"",
            "headers": [],
        }
    )


async def _get_managed_user_for_service_policy_test(
    token: str = Depends(get_auth_token),
    session=Depends(generate_session),
):
    return await get_current_user(_healthlabs_request(), token, session)


@fixture
def allow_managed_upstream_for_service_policy_tests() -> Generator[None]:
    """Expose upstream controllers only while testing their lower-level managed-user policy."""
    app.dependency_overrides[get_current_user] = _get_managed_user_for_service_policy_test
    yield
    app.dependency_overrides.pop(get_current_user, None)


@fixture
def healthlabs_provision(
    api_client: TestClient,
    monkeypatch: MonkeyPatch,
) -> Callable[[str | None], tuple[dict, dict[str, str]]]:
    secret = f"healthlabs-test-secret-{uuid4()}"
    monkeypatch.setattr(get_app_settings(), "HEALTHLABS_PROVISIONING_SECRET", secret)

    def provision(subject: str | None = None) -> tuple[dict, dict[str, str]]:
        response = api_client.post(
            "/api/healthlabs/v1/subjects",
            json={"externalSubjectId": subject or f"subject-{uuid4()}"},
            headers={"X-HealthLabs-Provisioning-Secret": secret},
        )
        assert response.status_code in {200, 201}
        body = response.json()
        return body, {"Authorization": f"Bearer {body['token']}"}

    return provision


@fixture
def healthlabs_set_capabilities(
    unfiltered_database: AllRepositories,
) -> Callable[..., None]:
    def set_capabilities(
        user_id: str,
        *capabilities: HealthLabsCapability,
        admin: bool = False,
        revoked: bool = False,
    ) -> None:
        session = unfiltered_database.session
        user = session.get(User, user_id)
        assert user is not None
        user.admin = admin
        session.query(HealthLabsCapabilityGrant).filter(HealthLabsCapabilityGrant.user_id == user_id).delete()
        session.add_all(
            [
                HealthLabsCapabilityGrant(
                    user_id=user_id,
                    capability=capability.value,
                    reason="plan API test",
                    revoked_at=user.created_at if revoked else None,
                )
                for capability in capabilities
            ]
        )
        session.commit()

    return set_capabilities


@fixture
def healthlabs_recipe_food(api_client: TestClient) -> Callable[[dict[str, str], str], tuple[dict, str]]:
    def create(headers: dict[str, str], name: str) -> tuple[dict, str]:
        response = api_client.post(
            "/api/healthlabs/v1/recipes",
            headers={**headers, "Idempotency-Key": f"food-{uuid4()}"},
            json={
                "name": f"Recipe for {name}",
                "servings": 1,
                "ingredients": [
                    {
                        "quantity": 100,
                        "food": {"name": name},
                        "unit": {"name": "gram", "abbreviation": "g"},
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return body, body["ingredients"][0]["food"]["id"]

    return create


@fixture
def healthlabs_nutrition_source(unfiltered_database: AllRepositories) -> Callable[..., tuple[str, str]]:
    def create(*, name: str, country: str = "GB", provider: str | None = None) -> tuple[str, str]:
        session = unfiltered_database.session
        source = HealthLabsNutrientSourceRelease(
            provider=provider or f"test-source-{uuid4()}",
            version="2026-10",
            country=country,
            source_url="https://example.test/source",
            license_url="https://example.test/licence",
            attribution="Test source",
            sha256=uuid4().hex.ljust(64, "0"),
        )
        session.add(source)
        session.flush()
        food = HealthLabsNutrientFood(
            source_release_id=source.id,
            source_food_id=str(uuid4()),
            name=name,
            nutrient_values={
                "kcal": {"amount": 50, "unit": "kcal"},
                "protein_g": {"amount": 10, "unit": "g"},
                "fibre_g": {"amount": 2, "unit": "g"},
                "carbs_g": {"amount": 5, "unit": "g"},
                "fat_g": {"amount": 1, "unit": "g"},
                "water_ml": {"amount": 80, "unit": "ml"},
            },
        )
        session.add(food)
        session.commit()
        return str(source.id), str(food.id)

    return create


@fixture
def healthlabs_plan_payload() -> Callable[..., dict]:
    def payload(
        *,
        expected_publication_id: str | None = None,
        entry_id: str | None = None,
        title: str = "Vegetable soup",
        recipe_id: str | None = None,
        recipe_revision_id: str | None = None,
    ) -> dict:
        body = {
            "from": datetime.now(UTC).date().isoformat(),
            "days": 3,
            "entries": [
                {
                    "id": entry_id or str(uuid4()),
                    "date": datetime.now(UTC).date().isoformat(),
                    "entryType": "dinner",
                    "position": 0,
                    "servings": 2,
                    "title": title,
                    "recipeId": recipe_id,
                    "recipeRevisionId": recipe_revision_id,
                }
            ],
        }
        if expected_publication_id is not None:
            body["expectedPublicationId"] = expected_publication_id
        return body

    return payload
