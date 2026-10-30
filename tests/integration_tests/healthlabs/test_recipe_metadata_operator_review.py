import asyncio
import json
from contextlib import contextmanager
from threading import Event
from uuid import UUID, uuid4

import httpx2
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models.healthlabs import HealthLabsFoodMatch
from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate as Job
from mealie.db.models.recipe import RecipeModel
from mealie.schema.recipe import Recipe, RecipeIngredient
from mealie.services.healthlabs import nutrition_curator_runner as runner
from mealie.services.healthlabs.recipe_metadata_operator import RecipeManifest, run_manifest, run_source_manifest
from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService
from tests.integration_tests.healthlabs.test_nutrition_curator_worker import _food_work, _install_success, _provider
from tests.integration_tests.healthlabs.test_recipe_metadata_operator import _factory
from tests.integration_tests.healthlabs.test_recipe_metadata_operator import (
    isolated_operator_budget as _isolated_budget,
)
from tests.integration_tests.healthlabs.test_recipe_metadata_worker import _fake_client, _recipe, _resolver, _schedule
from tests.integration_tests.healthlabs.test_recipe_metadata_worker import curator_settings as _curator_settings

curator_settings = _curator_settings
isolated_operator_budget = _isolated_budget


@pytest.mark.parametrize("change", ["personal_to_catalogue", "catalogue_to_personal", "authoring", "input", "revision"])
def test_source_revalidates_full_frozen_scope_and_pins_during_io(
    unique_user_fn_scoped, curator_settings, monkeypatch, change
):
    from mealie.services.healthlabs.revision_service import RecipeRevisionService
    from mealie.services.openai import OpenAIService

    user = unique_user_fn_scoped
    food, source, _ = _food_work(user)
    scope = "catalogue" if change == "catalogue_to_personal" else "personal"
    recipe = user.repos.recipes.create(
        Recipe(
            name=f"frozen source {uuid4()}",
            user_id=user.user_id,
            group_id=user.group_id,
            household_id=user.household_id,
            healthlabs_scope=scope,
            recipe_servings=2,
            recipe_ingredient=[RecipeIngredient(reference_id=uuid4(), food=food, quantity=100)],
        )
    )
    entry = {"recipe_id": recipe.id, "group_id": user.group_id, "scope": scope}
    if change in {"authoring", "input"}:
        key = f"{change}_fingerprint"
        entry[key] = getattr(RecipeMetadataService(user.repos.session).inputs(recipe), key)
    if change == "revision":
        entry["revision_id"] = RecipeRevisionService(user.repos.session).record(recipe, actor_user_id=user.user_id).id
    manifest = RecipeManifest.model_validate([entry])
    calls = []
    _install_success(monkeypatch, calls)
    original = OpenAIService.get_response

    async def mutate(self, *args, **kwargs):
        response = await original(self, *args, **kwargs)
        with _factory(user)() as session:
            if "_to_" in change:
                session.execute(
                    sa.update(RecipeModel)
                    .where(RecipeModel.id == recipe.id)
                    .values(healthlabs_scope="personal" if scope == "catalogue" else "catalogue")
                )
            elif change == "input":
                from mealie.db.models.healthlabs import HealthLabsNutrientFood

                session.execute(
                    sa.update(HealthLabsNutrientFood)
                    .where(HealthLabsNutrientFood.id == source.id)
                    .values(nutrient_values={"kcal": {"amount": 80, "unit": "kcal"}})
                )
            else:
                session.execute(
                    sa.update(RecipeModel)
                    .where(RecipeModel.id == recipe.id)
                    .values(description="Changed after authorization")
                )
                if change == "revision":
                    from mealie.repos.repository_factory import AllRepositories

                    fresh = AllRepositories(session, group_id=user.group_id, household_id=None).recipes.get_one(
                        recipe.id, "id"
                    )
                    RecipeRevisionService(session).record(fresh, actor_user_id=user.user_id)
            session.commit()
        return response

    monkeypatch.setattr(OpenAIService, "get_response", mutate)
    result = asyncio.run(
        run_source_manifest(
            manifest,
            apply=True,
            provider_resolver=_resolver(user),
            session_factory=_factory(user),
            reconciliation_rounds=0,
        )
    )
    assert result["source"]["approved"] == 0
    assert len(calls) == 1
    assert not user.repos.session.scalar(
        sa.select(HealthLabsFoodMatch.id).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
    )
    assert not user.repos.session.scalar(sa.select(Job.id).where(Job.recipe_id == recipe.id))


@pytest.mark.parametrize("pin", [False, True])
@pytest.mark.parametrize("chunked", [False, True])
def test_metadata_scope_or_pin_drift_cannot_enqueue_replacement(
    unique_user_fn_scoped, curator_settings, monkeypatch, pin, chunked
):
    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    if chunked:
        source.nutrient_values = {key: value for key, value in source.nutrient_values.items() if key != "fibre_g"}
        user.repos.session.commit()
    entry = {"recipe_id": recipe.id, "group_id": user.group_id, "scope": "personal"}
    if pin:
        entry["authoring_fingerprint"] = RecipeMetadataService(user.repos.session).inputs(recipe).authoring_fingerprint

    async def mutate():
        with _factory(user)() as session:
            session.execute(
                sa.update(RecipeModel)
                .where(RecipeModel.id == recipe.id)
                .values(**({"description": "pin changed"} if pin else {"healthlabs_scope": "catalogue"}))
            )
            session.commit()

    calls = []
    user.repos.session.commit()
    _fake_client(monkeypatch, user.repos.session, calls, mutate=mutate)
    result = asyncio.run(
        run_manifest(
            RecipeManifest.model_validate([entry]),
            apply=True,
            provider_resolver=_resolver(user),
            session_factory=_factory(user),
        )
    )
    jobs = list(user.repos.session.scalars(sa.select(Job).where(Job.recipe_id == recipe.id)))
    assert len(jobs) == 1 and all(j.state != "ready" for j in jobs)
    assert result["halt_reason"] == "manifest_changed"
    assert len(calls) == 1


@pytest.mark.parametrize("healthy", [False, True])
def test_actual_metadata_consumer_advances_unavailable_key_prefix_and_backs_off(
    unique_user_fn_scoped, curator_settings, monkeypatch, api_client, healthy
):
    from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataWorker
    from tests.fixtures.fixture_users import build_unique_user

    user = unique_user_fn_scoped
    other = build_unique_user(user.repos.session, "", api_client)
    bad_provider, good_provider = _provider(user), _provider(other)
    bad_provider.api_key = ""
    good_provider.model = "offline-deployment"
    user.repos.session.commit()
    ids = []
    base = uuid4().int // 256 * 256
    for index, owner in enumerate([user] * 8 + ([other] if healthy else [])):
        recipe, _ = _recipe(owner)
        work_id = _schedule(owner, recipe)
        row = user.repos.session.get(Job, work_id)
        row.id = UUID(int=base + index)
        ids.append(row.id)
        user.repos.session.commit()
    stop = Event()
    windows, pauses, requests = [], [], []

    @contextmanager
    def sessions():
        with Session(user.repos.session.get_bind()) as session:
            yield session

    class Worker(RecipeMetadataWorker):
        def __init__(self, session, **kwargs):
            super().__init__(session, group_ids={user.group_id, other.group_id}, **kwargs)

        def work_ids(self, **kwargs):
            result = super().work_ids(**kwargs)
            windows.append(result)
            if len(windows) >= 3:
                stop.set()
            return result

    async def pause(*args):
        pauses.append(True)
        stop.set()

    def http(request):
        requests.append(request)
        context = json.loads(json.loads(request.content)["messages"][1]["content"][0]["text"])
        plan = {
            "ingredients": [],
            "effective_servings": None,
            "servings_assumption": None,
            "general_metadata": {
                "total_minutes": 20,
                "prep_minutes": 5,
                "active_minutes": 15,
                "effort": 1,
                "rationales": dict.fromkeys(("total_minutes", "prep_minutes", "active_minutes", "effort"), "One pot"),
                "plants": [],
                "plant_ingredient_references": context["plant_ingredient_references"],
                "plants_rationale": "Every ingredient inspected",
            },
        }
        return httpx2.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 0,
                "model": "offline-deployment",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(plan)}}
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150},
            },
        )

    monkeypatch.setattr(runner, "STOP", stop)
    monkeypatch.setattr(runner, "session_context", sessions)
    monkeypatch.setattr(runner, "RecipeMetadataWorker", Worker)
    monkeypatch.setattr(runner, "_metadata_pause", pause, raising=False)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(http)) as client:
            await runner._metadata_loop(client)
        assert client.is_closed

    asyncio.run(run())
    assert windows[0] == ids[:8]
    assert pauses and len(windows) <= 3
    assert len(requests) == int(healthy)
    if healthy:
        user.repos.session.expire_all()
        assert user.repos.session.get(Job, ids[-1]).state == "ready"
