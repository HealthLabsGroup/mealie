"""Durable jobs exercise real SQL and the actual OpenAI response/guard path."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate
from mealie.services.healthlabs.nutrition_provider_resolution import SessionProviderResolver
from mealie.services.openai import OpenAIService
from tests.integration_tests.healthlabs.test_nutrition_curator_worker import curator_settings as _curator_settings
from tests.integration_tests.healthlabs.test_recipe_metadata_reads import _recipe

curator_settings = _curator_settings

NOW = datetime(2001, 1, 1, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_budget_day(unique_user_fn_scoped):
    from mealie.db.models.healthlabs import HealthLabsNutritionProviderUsage

    session = unique_user_fn_scoped.repos.session
    session.execute(
        sa.delete(HealthLabsNutritionProviderUsage).where(HealthLabsNutritionProviderUsage.usage_date == NOW.date())
    )
    session.commit()


def _resolver(user):
    return SessionProviderResolver(
        identity_ids={user.group_id: uuid4()},
        name="offline Azure",
        base_url="https://azure.example/v1",
        model="offline-deployment",
        credential=lambda: "offline-secret",
        timeout=30,
    )


def _worker(user, **kwargs):
    from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataWorker

    return RecipeMetadataWorker(
        user.repos.session, group_ids={user.group_id}, provider_resolver=_resolver(user), now=NOW, **kwargs
    )


def _schedule(user, recipe):
    from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler

    work_id = RecipeMetadataScheduler(user.repos.session, now=NOW).schedule(recipe)
    user.repos.session.commit()
    return work_id


def _fake_client(
    monkeypatch,
    session,
    calls,
    *,
    mutate=None,
    invalid=False,
    transform=None,
    usage=True,
    expected_model="offline-deployment",
):
    class Response:
        async def __aenter__(self):
            assert not session.in_transaction()
            if mutate:
                await mutate()
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            context = calls[-1]["context"]
            result = {
                "ingredients": [],
                "effective_servings": None,
                "servings_assumption": None,
                "general_metadata": None,
            }
            if context["phase"] == "general":
                result["general_metadata"] = {
                    "total_minutes": 20,
                    "prep_minutes": 5,
                    "active_minutes": 15,
                    "effort": 1,
                    "rationales": dict.fromkeys(
                        ("total_minutes", "prep_minutes", "active_minutes", "effort"), "Simple recipe"
                    ),
                    "plants": [],
                    "plant_ingredient_references": context["plant_ingredient_references"],
                    "plants_rationale": "Every supplied ingredient reviewed",
                }
            for row in context["ingredients"]:
                result["ingredients"].append(
                    {
                        "reference_id": row["reference_id"],
                        "effective_grams": None,
                        "quantity_assumption": None,
                        "source_food_id": None,
                        "portion_or_density_id": None,
                        "assumed_portion_amount": None,
                        "rationale": "Missing nutrient estimate",
                        "nutrients_per_100g": {
                            "kcal": None,
                            "protein_g": None,
                            "carbs_g": None,
                            "fat_g": None,
                            "fibre_g": -1 if invalid else 2,
                            "water_ml": None,
                        },
                    }
                )
            if transform:
                transform(result)
            return json.dumps(
                {
                    "id": "offline",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "offline-deployment",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(result)},
                        }
                    ],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150} if usage else None,
                }
            )

    def parse(**kwargs):
        schema = kwargs["response_format"].model_json_schema()

        def strict(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False
                    assert set(node.get("required", [])) == set(node.get("properties", {}))
                for value in node.values():
                    strict(value)
            elif isinstance(node, list):
                for value in node:
                    strict(value)

        strict(schema)
        assert kwargs["max_completion_tokens"] > 256
        assert kwargs["model"] == expected_model
        context = json.loads(kwargs["messages"][1]["content"][0]["text"])
        calls.append({"context": context, "request": kwargs})
        return Response()

    monkeypatch.setattr(
        OpenAIService,
        "get_client",
        lambda self, provider: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(with_streaming_response=SimpleNamespace(parse=parse)))
        ),
    )


def test_general_gaps_schedule_and_actual_strict_transport_finishes_without_source_changes(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    original = recipe.model_dump(mode="json")
    source_values = dict(source.nutrient_values)
    work_id = _schedule(user, recipe)
    assert work_id is not None  # All core nutrition already exists, but general metadata is absent.
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    result = asyncio.run(_worker(user).run_work(work_id))
    assert result.ready == 1
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "ready"
    assert row.provider_name == "offline Azure"
    assert row.provider_model == "offline-deployment"
    assert row.payload["general_metadata"]["prep_minutes"] == 5
    assert len(calls) == 1
    assert calls[0]["context"]["ingredients"] == []
    assert user.repos.recipes.get_one(recipe.id, "id").model_dump(mode="json") == original
    user.repos.session.refresh(source)
    assert source.nutrient_values == source_values
    assert _schedule(user, recipe) == work_id
    assert asyncio.run(_worker(user).run_work(work_id)).claimed == 0
    assert len(calls) == 1


def test_two_workers_share_claim_and_no_transaction_during_network(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    from sqlalchemy.orm import Session

    from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataWorker

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    calls = []

    async def competing():
        with Session(user.repos.session.get_bind()) as session:
            contender = RecipeMetadataWorker(
                session, group_ids={user.group_id}, provider_resolver=_resolver(user), now=NOW
            )
            assert (await contender.run_work(work_id)).claimed == 0

    _fake_client(monkeypatch, user.repos.session, calls, mutate=competing)
    assert asyncio.run(_worker(user).run_work(work_id)).ready == 1
    assert len(calls) == 1


def test_foreign_group_cannot_claim(unique_user_fn_scoped, curator_settings):
    from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataWorker

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    worker = RecipeMetadataWorker(user.repos.session, group_ids={uuid4()}, provider_resolver=_resolver(user), now=NOW)
    assert asyncio.run(worker.run_work(work_id)).claimed == 0
    assert user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id).state == "pending"


@pytest.mark.parametrize("change", ["authoring", "source"])
def test_dependency_edit_during_request_supersedes_without_applying(
    unique_user_fn_scoped, curator_settings, monkeypatch, change
):
    from sqlalchemy.orm import Session

    from mealie.db.models.healthlabs import HealthLabsNutrientFood
    from mealie.db.models.recipe import RecipeModel

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    source_id = source.id
    updated_values = {**source.nutrient_values, "protein_g": {"amount": 20, "unit": "g"}}
    work_id = _schedule(user, recipe)

    async def edit():
        with Session(user.repos.session.get_bind()) as session:
            if change == "authoring":
                session.execute(
                    sa.update(RecipeModel).where(RecipeModel.id == recipe.id).values(description="New recipe context")
                )
            else:
                session.execute(
                    sa.update(HealthLabsNutrientFood)
                    .where(HealthLabsNutrientFood.id == source_id)
                    .values(nutrient_values=updated_values)
                )
            session.commit()

    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, mutate=edit)
    assert asyncio.run(_worker(user).run_work(work_id)).ready == 0
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "superseded" and row.payload is None
    assert (
        user.repos.session.scalar(
            sa.select(sa.func.count())
            .select_from(HealthLabsRecipeMetadataEstimate)
            .where(
                HealthLabsRecipeMetadataEstimate.recipe_id == recipe.id,
                HealthLabsRecipeMetadataEstimate.state == "pending",
            )
        )
        == 1
    )


def test_budget_denial_defers_without_attempt(unique_user_fn_scoped, curator_settings, monkeypatch):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    worker = _worker(user)
    worker.budget.daily_request_limit = 0
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    result = asyncio.run(worker.run_work(work_id))
    assert result.provider_exhausted
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "pending" and row.attempt_count == 0
    assert row.next_attempt_at > NOW
    assert not calls


def test_transport_error_has_bounded_backoff_and_sanitized_error(unique_user_fn_scoped, curator_settings, monkeypatch):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)

    def fail(self, provider):
        raise RuntimeError("secret key and response must never be persisted")

    monkeypatch.setattr(OpenAIService, "get_client", fail)
    assert asyncio.run(_worker(user).run_work(work_id)).failed == 1
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "failed" and row.attempt_count == 1
    assert row.next_attempt_at > NOW
    assert row.last_error_code == "provider_failure"
    assert (row.provider_name, row.provider_model) == ("offline Azure", "offline-deployment")


def test_source_improvement_reuses_complete_plan_and_preserves_inference_origin(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    worker = _worker(user)
    worker._fixed_now = NOW + timedelta(minutes=1)
    assert asyncio.run(worker.run_work(work_id)).ready == 1
    old = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    original_fp = old.input_fingerprint
    inference_time = old.updated_at
    assert inference_time != old.created_at
    before = RecipeMetadataService(user.repos.session).project(recipe)[1]
    source.nutrient_values = {**source.nutrient_values, "protein_g": {"amount": 20, "unit": "g"}}
    user.repos.session.commit()
    replacement = _schedule(user, recipe)
    assert replacement != work_id
    current = user.repos.session.get(HealthLabsRecipeMetadataEstimate, replacement)
    assert current.state == "ready"
    assert current.attempt_count == 0
    assert current.payload["reuse_origin"]["estimate_id"] == str(work_id)
    assert current.payload["reuse_origin"]["input_fingerprint"] == original_fp
    assert datetime.fromisoformat(current.payload["reuse_origin"]["inferred_at"]) == inference_time
    assert current.payload["validation"]["input_fingerprint"] != original_fp
    after = RecipeMetadataService(user.repos.session).project(recipe)
    assert after[0].per_serving.protein_g.verified == 10
    assert after[1].prep_minutes.provenance_ids == before.prep_minutes.provenance_ids
    assert old.state == "ready"
    assert len(calls) == 1


def test_scanner_persists_cursor_wraps_and_rechecks_prior_pages(unique_user_fn_scoped):
    from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler

    user = unique_user_fn_scoped
    first, _ = _recipe(user)
    second, _ = _recipe(user)
    scheduler = RecipeMetadataScheduler(user.repos.session, now=NOW)
    first_page = scheduler.scan_page(group_ids={user.group_id}, page_size=1)
    second_page = RecipeMetadataScheduler(user.repos.session, now=NOW).scan_page(group_ids={user.group_id}, page_size=1)
    assert first_page.scanned == second_page.scanned == 1
    assert first_page.recipe_ids != second_page.recipe_ids
    wrapped = scheduler.scan_page(group_ids={user.group_id}, page_size=1)
    assert wrapped.wrapped
    next_cycle = scheduler.scan_page(group_ids={user.group_id}, page_size=1)
    assert next_cycle.recipe_ids == first_page.recipe_ids
    assert {first.id, second.id} == set(first_page.recipe_ids + second_page.recipe_ids)


def test_expired_reclaimed_lease_rejects_late_completion(unique_user_fn_scoped, curator_settings, monkeypatch):
    from sqlalchemy.orm import Session

    from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataWorker

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    calls = []
    reclaimed_token = uuid4()

    async def reclaim():
        with Session(user.repos.session.get_bind()) as session:
            session.execute(
                sa.update(HealthLabsRecipeMetadataEstimate)
                .where(HealthLabsRecipeMetadataEstimate.id == work_id)
                .values(lease_token=reclaimed_token, lease_expires_at=NOW + timedelta(minutes=30))
            )
            session.commit()

    _fake_client(monkeypatch, user.repos.session, calls, mutate=reclaim)
    result = asyncio.run(_worker(user).run_work(work_id))
    assert result.ready == 0
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "running" and row.lease_token == reclaimed_token and row.payload is None
    # An expired abandoned claim is recoverable through the same real claim path.
    worker = RecipeMetadataWorker(
        user.repos.session,
        group_ids={user.group_id},
        provider_resolver=_resolver(user),
        now=NOW + timedelta(minutes=31),
    )
    _fake_client(monkeypatch, user.repos.session, calls)
    assert asyncio.run(worker.run_work(work_id)).ready == 1


def test_invalid_nutrients_are_corrected_then_rejected_without_source_writes(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    source.nutrient_values = {key: value for key, value in source.nutrient_values.items() if key != "fibre_g"}
    user.repos.session.commit()
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, invalid=True)
    assert asyncio.run(_worker(user).run_work(work_id)).rejected == 1
    assert len(calls) == 2
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "rejected" and row.payload is None and row.last_error_code == "invalid_plan"
    assert "fibre_g" not in source.nutrient_values


def test_post_revision_hook_schedules_after_committed_write(unique_user_fn_scoped):
    from mealie.lang.providers import get_locale_provider
    from mealie.services.recipe.recipe_service import RecipeService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    service = RecipeService(
        user.repos,
        user.repos.users.get_one(user.user_id),
        user.repos.households.get_one(user.household_id),
        get_locale_provider("en-US"),
    )
    service._record_revision(recipe)
    row = user.repos.session.scalar(
        sa.select(HealthLabsRecipeMetadataEstimate).where(HealthLabsRecipeMetadataEstimate.recipe_id == recipe.id)
    )
    assert row is not None and row.state == "pending"


def test_taxonomy_change_cannot_keep_old_inferred_plant(unique_user_fn_scoped, curator_settings, monkeypatch):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    assert asyncio.run(_worker(user).run_work(work_id)).ready == 1
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    payload = dict(row.payload)
    payload["general_metadata"] = {
        **payload["general_metadata"],
        "plants": [{"plant_id": "peanut", "name": "Peanut", "quantities": []}],
    }
    row.payload = payload
    match = user.repos.session.scalar(
        sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.nutrient_food_id == source.id)
    )
    match.plant_id = "almond"
    user.repos.session.commit()
    plants = RecipeMetadataService(user.repos.session).project(recipe)[1].plants
    assert [plant.plant_id for plant in plants.recorded] == ["almond"]
    assert plants.estimated == []


def test_known_usage_persists(unique_user_fn_scoped, curator_settings, monkeypatch):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    assert asyncio.run(_worker(user).run_work(work_id)).ready == 1
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.usage == {"requests": 1, "input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 100}


def test_missing_usage_cannot_finalize(unique_user_fn_scoped, curator_settings, monkeypatch):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, usage=False)
    result = asyncio.run(_worker(user).run_work(work_id))
    assert result.failed == 1
    assert user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id).payload is None


@pytest.mark.parametrize("fault", ["missing_ref", "extra_ref", "fixed_rewrite", "nan", "bad_macro"])
def test_invalid_ingredient_plans_cannot_reach_ready(unique_user_fn_scoped, curator_settings, monkeypatch, fault):
    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    source.nutrient_values = {key: value for key, value in source.nutrient_values.items() if key != "fibre_g"}
    user.repos.session.commit()
    work_id = _schedule(user, recipe)

    def corrupt(result):
        if fault == "missing_ref":
            result["ingredients"] = []
        elif fault == "extra_ref":
            result["ingredients"].append({**result["ingredients"][0], "reference_id": str(uuid4())})
        elif fault == "fixed_rewrite":
            result["ingredients"][0]["nutrients_per_100g"]["protein_g"] = 99
        elif fault == "nan":
            result["ingredients"][0]["nutrients_per_100g"]["fibre_g"] = float("nan")
        else:
            result["ingredients"][0]["nutrients_per_100g"]["fibre_g"] = 200

    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, transform=corrupt)
    assert asyncio.run(_worker(user).run_work(work_id)).rejected == 1
    assert len(calls) == 2
    assert user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id).payload is None


def test_87_ingredients_are_chunked_with_real_guard_accounting(unique_user_fn_scoped, curator_settings, monkeypatch):
    from mealie.schema.recipe import Recipe

    user = unique_user_fn_scoped
    original, source = _recipe(user)
    source.nutrient_values = {key: value for key, value in source.nutrient_values.items() if key != "fibre_g"}
    user.repos.session.commit()
    recipe = user.repos.recipes.create(
        Recipe(
            name=f"87 ingredients {uuid4()}",
            user_id=user.user_id,
            group_id=user.group_id,
            household_id=user.household_id,
            recipe_servings=2,
            recipe_ingredient=[
                original.recipe_ingredient[0].model_copy(update={"reference_id": uuid4()}) for _ in range(87)
            ],
        )
    )
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    worker = _worker(user)
    worker.budget.daily_request_limit = 100
    worker.budget.daily_budget_microusd = 10_000_000
    result = asyncio.run(worker.run_work(work_id))
    assert result.ready == 1
    assert len(calls) == 12
    assert all(len(call["context"]["ingredients"]) <= 8 for call in calls)
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert len(row.payload["ingredients"]) == 87
    assert row.usage["requests"] == 12


def test_normal_default_uses_openrouter_and_skips_providerless_prefix(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataWorker
    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import _provider

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    worker = RecipeMetadataWorker(user.repos.session, group_ids={user.group_id}, now=NOW)
    assert worker.work_ids() == []
    user.repos.session.commit()
    provider = _provider(user)
    provider.name = "OpenRouter"
    provider.model = "qwen/qwen3.6-flash"
    provider.base_url = "https://openrouter.ai/api/v1"
    user.repos.session.commit()
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, expected_model="qwen/qwen3.6-flash")
    assert worker.work_ids() == [work_id]
    user.repos.session.commit()
    assert asyncio.run(worker.run_once()).ready == 1
    assert calls[0]["request"]["extra_body"] == {"reasoning": {"enabled": False}}
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert (row.provider_name, row.provider_model) == ("OpenRouter", "qwen/qwen3.6-flash")


def test_all_recorded_and_source_metadata_creates_no_job(unique_user_fn_scoped):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    recipe.total_time_seconds = 1200
    recipe.prep_time_seconds = 300
    recipe.perform_time_seconds = 900
    recipe.total_time = "PT20M"
    recipe.prep_time = "PT5M"
    recipe.perform_time = "PT15M"
    recipe.extras = {"healthlabs.ephra": json.dumps({"ephraEffort1": True})}
    match = user.repos.session.scalar(
        sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.nutrient_food_id == source.id)
    )
    match.plant_id = "rice"
    user.repos.session.commit()
    recipe = user.repos.recipes.update(recipe.slug, recipe)
    assert _schedule(user, recipe) is None
    assert (
        user.repos.session.scalar(
            sa.select(sa.func.count())
            .select_from(HealthLabsRecipeMetadataEstimate)
            .where(HealthLabsRecipeMetadataEstimate.recipe_id == recipe.id)
        )
        == 0
    )


def test_postwrite_enqueue_failure_keeps_committed_authoring(unique_user_fn_scoped, monkeypatch):
    from mealie.lang.providers import get_locale_provider
    from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler
    from mealie.services.recipe.recipe_service import RecipeService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)

    def fail(*args):
        raise RuntimeError("offline scheduling failure")

    monkeypatch.setattr(RecipeMetadataScheduler, "schedule", fail)
    service = RecipeService(
        user.repos,
        user.repos.users.get_one(user.user_id),
        user.repos.households.get_one(user.household_id),
        get_locale_provider("en-US"),
    )
    assert service._record_revision(recipe).id == recipe.id
    assert user.repos.recipes.get_one(recipe.id, "id").name == recipe.name


def test_periodic_tick_enqueues_before_nutrition_budget_stop(unique_user_fn_scoped, monkeypatch):
    import importlib
    from contextlib import contextmanager

    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import _provider

    user = unique_user_fn_scoped
    _provider(user)
    recipe, _ = _recipe(user)
    task = importlib.import_module("mealie.services.scheduler.tasks.reconcile_healthlabs_nutrition")

    @contextmanager
    def sessions():
        yield user.repos.session

    class NutritionDone:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            assert (
                user.repos.session.scalar(
                    sa.select(HealthLabsRecipeMetadataEstimate.id).where(
                        HealthLabsRecipeMetadataEstimate.recipe_id == recipe.id
                    )
                )
                is not None
            )
            return SimpleNamespace(
                discovered=0, processed=0, completed=0, review_required=0, failed=0, provider_exhausted=True
            )

    monkeypatch.setattr(task, "session_context", sessions)
    monkeypatch.setattr(task, "NutritionReconciliationWorker", NutritionDone)
    task.resume_nutrition_reconciliation()
    task.reconcile_healthlabs_nutrition()
    assert (
        user.repos.session.scalar(
            sa.select(HealthLabsRecipeMetadataEstimate.state).where(
                HealthLabsRecipeMetadataEstimate.recipe_id == recipe.id
            )
        )
        == "pending"
    )


def test_returning_to_prior_input_reactivates_superseded_pending_job(unique_user_fn_scoped):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    old_id = _schedule(user, recipe)
    original_description = recipe.description
    recipe.description = "Changed input"
    new_id = _schedule(user, recipe)
    assert new_id != old_id
    assert user.repos.session.get(HealthLabsRecipeMetadataEstimate, old_id).state == "superseded"
    recipe.description = original_description
    assert _schedule(user, recipe) == old_id
    assert user.repos.session.get(HealthLabsRecipeMetadataEstimate, old_id).state == "pending"


def test_generic_source_improvement_replaces_old_model_nutrient_without_request(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    user.repos.session.execute(sa.delete(HealthLabsFoodMatch).where(HealthLabsFoodMatch.nutrient_food_id == source.id))
    source.nutrient_values = {key: value for key, value in source.nutrient_values.items() if key != "fibre_g"}
    user.repos.session.commit()
    work_id = _schedule(user, recipe)
    source_id = str(source.id)

    def choose(result):
        for row in result["ingredients"]:
            row["source_food_id"] = source_id

    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, transform=choose)
    assert asyncio.run(_worker(user).run_work(work_id)).ready == 1
    source.nutrient_values = {**source.nutrient_values, "fibre_g": {"amount": 3, "unit": "g"}}
    user.repos.session.commit()
    new_id = _schedule(user, recipe)
    assert user.repos.session.get(HealthLabsRecipeMetadataEstimate, new_id).state == "ready"
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    assert RecipeMetadataService(user.repos.session).project(recipe)[0].per_serving.fibre_g.estimated == 1.5
    assert len(calls) == 2


def test_complete_source_plants_cannot_be_augmented_by_unrequested_inference(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    match = user.repos.session.scalar(
        sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.nutrient_food_id == source.id)
    )
    match.plant_id = "rice"
    user.repos.session.commit()
    work_id = _schedule(user, recipe)
    reference = str(recipe.recipe_ingredient[0].reference_id)

    def invent(result):
        result["general_metadata"]["plants"] = [{"plant_id": "peanut", "name": "Peanut", "quantities": []}]
        result["general_metadata"]["plant_ingredient_references"] = [reference]

    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, transform=invent)
    assert asyncio.run(_worker(user).run_work(work_id)).rejected == 1
    assert len(calls) == 2
    assert user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id).payload is None


@pytest.mark.parametrize("food_group", ["meat", "fish", "eggs", "dairy", "water", "salt"])
def test_unknown_to_accepted_nonplant_drops_inference_on_read_and_rematerialization(
    unique_user_fn_scoped, curator_settings, monkeypatch, food_group
):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    match = user.repos.session.scalar(
        sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.nutrient_food_id == source.id)
    )
    match.food_group = None
    match.plant_id = None
    user.repos.session.commit()
    work_id = _schedule(user, recipe)

    def peanut(result):
        result["general_metadata"]["plants"] = [{"plant_id": "peanut", "name": "Peanut", "quantities": []}]

    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, transform=peanut)
    assert asyncio.run(_worker(user).run_work(work_id)).ready == 1
    old = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    old_payload = json.dumps(old.payload, sort_keys=True)
    service = RecipeMetadataService(user.repos.session)
    assert service.project(recipe)[1].plants.estimated[0].plant_id == "peanut"
    match.food_group = food_group
    user.repos.session.commit()
    nutrition, metadata = service.project(recipe)
    assert metadata.plants.estimated == [] and metadata.plants.status == "complete"
    assert metadata.prep_minutes.estimated == 5 and nutrition.complete
    fresh_id = _schedule(user, recipe)
    fresh = user.repos.session.get(HealthLabsRecipeMetadataEstimate, fresh_id)
    assert fresh_id != work_id and fresh.state == "ready" and fresh.attempt_count == 0
    assert fresh.payload["general_metadata"]["plants"] == []
    assert fresh.payload["ingredients"][0]["accepted_plant_classified"] is True
    assert fresh.payload["ingredients"][0]["accepted_food_group"] == food_group
    assert json.dumps(old.payload, sort_keys=True) == old_payload and len(calls) == 1
