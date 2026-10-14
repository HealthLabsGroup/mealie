import asyncio
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.core.config import get_app_dirs
from mealie.db.models.recipe import Category, IngredientFoodModel, IngredientUnitModel, RecipeModel, Tag, Tool
from mealie.db.models.recipe.recipe import HealthLabsRecipeRevision
from mealie.pkgs import safehttp
from mealie.repos.repository_factory import AllRepositories

RECIPE_HTML = b"""
<!doctype html><html><head><script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Recipe",
  "name": "Safe tomato soup",
  "description": "A bounded import",
  "recipeYield": "2 servings",
  "recipeIngredient": ["2 cups tomatoes", "1 tsp salt"],
  "recipeInstructions": [
    {"@type": "HowToStep", "name": "Blend", "text": "Blend the tomatoes."},
    {"@type": "HowToStep", "text": "Add salt."}
  ]
}
</script></head><body></body></html>
"""


def _result(*, content: bytes = RECIPE_HTML, content_type: str = "text/html") -> safehttp.FetchResult:
    return safehttp.FetchResult(
        content=content,
        status_code=200,
        url="https://recipes.example/final",
        headers=httpx.Headers({"content-type": content_type}),
        encoding="utf-8",
    )


def _counts(database: AllRepositories) -> tuple[int, ...]:
    session = database.session
    models = (RecipeModel, HealthLabsRecipeRevision, IngredientFoodModel, IngredientUnitModel, Category, Tag, Tool)
    return tuple(session.scalar(sa.select(sa.func.count()).select_from(model)) or 0 for model in models)


def test_structured_import_draft_polling_is_idempotent_and_commits_no_recipe_rows(
    api_client: TestClient,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, headers = healthlabs_provision()

    async def fetch(*_args, **_kwargs):
        return _result()

    monkeypatch.setattr(safehttp, "resilient_fetch", fetch)
    before = _counts(unfiltered_database)
    recipe_data_dir = get_app_dirs().RECIPE_DATA_DIR
    directories_before = {path.name for path in recipe_data_dir.iterdir() if path.is_dir()}
    key = f"draft-{uuid4()}"
    request_headers = {**headers, "Idempotency-Key": key}

    created = api_client.post(
        "/api/healthlabs/v1/import-drafts",
        json={"url": "https://recipes.example/start"},
        headers=request_headers,
    )
    replay = api_client.post(
        "/api/healthlabs/v1/import-drafts",
        json={"url": "https://recipes.example/start"},
        headers=request_headers,
    )

    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    draft = created.json()
    assert draft["state"] == "ready"
    assert draft["recipe"]["name"] == "Safe tomato soup"
    assert draft["recipe"]["sourceUrl"] == "https://recipes.example/final"
    assert [item["originalText"] for item in draft["recipe"]["ingredients"]] == [
        "2 cups tomatoes",
        "1 tsp salt",
    ]
    assert draft["recipe"]["ingredients"][0]["quantity"] == 2
    assert draft["recipe"]["ingredients"][0]["food"]["name"] == "tomatoes"
    assert draft["recipe"]["ingredients"][0]["unit"]["name"] == "cup"
    assert [step["text"] for step in draft["recipe"]["instructions"]] == [
        "Blend the tomatoes.",
        "Add salt.",
    ]

    polled = api_client.get(f"/api/healthlabs/v1/import-drafts/{draft['id']}", headers=headers)
    assert polled.status_code == 200
    assert polled.json() == draft
    assert _counts(unfiltered_database) == before
    assert {path.name for path in recipe_data_dir.iterdir() if path.is_dir()} == directories_before


def test_import_draft_is_private_to_its_owner(
    api_client: TestClient,
    healthlabs_provision: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner_headers = healthlabs_provision()
    _, other_headers = healthlabs_provision()

    async def fetch(*_args, **_kwargs):
        return _result()

    monkeypatch.setattr(safehttp, "resilient_fetch", fetch)
    created = api_client.post(
        "/api/healthlabs/v1/import-drafts",
        json={"url": "https://recipes.example/private"},
        headers={**owner_headers, "Idempotency-Key": f"draft-{uuid4()}"},
    )
    assert created.status_code == 201
    assert (
        api_client.get(f"/api/healthlabs/v1/import-drafts/{created.json()['id']}", headers=other_headers).status_code
        == 404
    )


def test_import_accepts_direct_schema_org_json(
    api_client: TestClient,
    healthlabs_provision: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, headers = healthlabs_provision()

    async def fetch(*_args, **_kwargs):
        return _result(
            content=b'{"@context":"https://schema.org","@type":"Recipe","name":"JSON soup",'
            b'"recipeIngredient":["1 cup water"],"recipeInstructions":["Boil."]}',
            content_type="application/ld+json",
        )

    monkeypatch.setattr(safehttp, "resilient_fetch", fetch)
    response = api_client.post(
        "/api/healthlabs/v1/import-drafts",
        json={"url": "https://recipes.example/recipe.json"},
        headers={**headers, "Idempotency-Key": f"draft-{uuid4()}"},
    )

    assert response.status_code == 201
    assert response.json()["recipe"]["name"] == "JSON soup"


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (safehttp.ResponseTooLargeError("too large"), 413),
        (httpx.TooManyRedirects("too many redirects"), 422),
    ],
)
def test_import_rejects_oversize_and_redirect_overflow(
    failure: Exception,
    expected_status: int,
    api_client: TestClient,
    healthlabs_provision: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, headers = healthlabs_provision()

    async def fetch(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(safehttp, "resilient_fetch", fetch)
    response = api_client.post(
        "/api/healthlabs/v1/import-drafts",
        json={"url": "https://recipes.example/failure"},
        headers={**headers, "Idempotency-Key": f"draft-{uuid4()}"},
    )
    assert response.status_code == expected_status


def test_import_rejects_invalid_mime_and_timeout(
    api_client: TestClient,
    healthlabs_provision: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, headers = healthlabs_provision()

    async def invalid_mime(*_args, **_kwargs):
        return _result(content=b"not html", content_type="application/octet-stream")

    monkeypatch.setattr(safehttp, "resilient_fetch", invalid_mime)
    invalid = api_client.post(
        "/api/healthlabs/v1/import-drafts",
        json={"url": "https://recipes.example/binary"},
        headers={**headers, "Idempotency-Key": f"draft-{uuid4()}"},
    )
    assert invalid.status_code == 415

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return _result()

    monkeypatch.setattr(safehttp, "resilient_fetch", slow)
    monkeypatch.setattr("mealie.services.healthlabs.import_draft_service.IMPORT_TIMEOUT_SECONDS", 0.001)
    timed_out = api_client.post(
        "/api/healthlabs/v1/import-drafts",
        json={"url": "https://recipes.example/slow"},
        headers={**headers, "Idempotency-Key": f"draft-{uuid4()}"},
    )
    assert timed_out.status_code == 408
