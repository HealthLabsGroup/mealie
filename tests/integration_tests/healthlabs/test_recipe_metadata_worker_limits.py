"""Review regressions use real SQL and SDK HTTP transport with no external I/O."""

import asyncio
import json
from datetime import timedelta

import httpx2
import pytest
import sqlalchemy as sa

from mealie.db.models.healthlabs import HealthLabsNutritionProviderUsage, HealthLabsRecipeMetadataEstimate
from mealie.services.healthlabs.recipe_metadata_estimator import MetadataRequestLimits
from tests.integration_tests.healthlabs.test_recipe_metadata_worker import (
    NOW,
    _fake_client,
    _recipe,
    _schedule,
    _worker,
)
from tests.integration_tests.healthlabs.test_recipe_metadata_worker import (
    curator_settings as _curator_settings,
)
from tests.integration_tests.healthlabs.test_recipe_metadata_worker import (
    isolated_budget_day as _isolated_budget_day,
)

curator_settings = _curator_settings
isolated_budget_day = _isolated_budget_day


def test_metadata_sdk_does_not_retry_a_physical_request(unique_user_fn_scoped, curator_settings):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    requests = []

    def http(request):
        assert not user.repos.session.in_transaction()
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(500, json={"error": {"message": "offline transient failure"}})
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
                "plants_rationale": "All ingredients inspected",
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

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(http)) as client:
            return await _worker(user, http_client=client, limits=MetadataRequestLimits(max_requests=1)).run_work(
                work_id
            )

    result = asyncio.run(run())
    assert len(requests) == 1
    assert result.failed == 1 and result.requests == 1
    usage = user.repos.session.scalar(
        sa.select(HealthLabsNutritionProviderUsage).where(HealthLabsNutritionProviderUsage.usage_date == NOW.date())
    )
    assert usage.request_count == 1 and usage.reserved_microusd > 0 and usage.spent_microusd == 0
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.last_error_code == "provider_failure"
    assert "offline-secret" not in json.dumps(
        {"payload": row.payload, "usage": row.usage, "error": row.last_error_code}
    )


def test_oversize_input_is_terminal_not_a_daily_budget_deferral(unique_user_fn_scoped, curator_settings, monkeypatch):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    worker = _worker(user, limits=MetadataRequestLimits(max_input_tokens=1024))
    result = asyncio.run(worker.run_work(work_id))
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "rejected" and row.last_error_code == "input_limit_exceeded"
    assert result.rejected == 1 and not result.provider_exhausted
    assert row.attempt_count == 0 and not calls
    worker._fixed_now = NOW + timedelta(days=1)
    assert worker.work_ids() == []


@pytest.mark.parametrize(
    ("caps", "reason"),
    [({"max_requests": 0}, "operator_request_limit"), ({"max_cost_microusd": 0}, "operator_cost_limit")],
)
def test_operator_caps_pause_for_same_day_resume(unique_user_fn_scoped, curator_settings, monkeypatch, caps, reason):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    report = asyncio.run(_worker(user, limits=MetadataRequestLimits(**caps)).run_work(work_id))
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.next_attempt_at == NOW and row.attempt_count == 0 and row.state == "pending"
    assert row.last_error_code == reason
    assert report.local_limit_reached and report.limit_reason == reason and not report.provider_exhausted
    assert not calls
    assert asyncio.run(_worker(user).run_work(work_id)).ready == 1


def test_stop_during_request_preparation_is_resumable_without_budget_deferral(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    from mealie.services.openai import OpenAIService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    stopped = False
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    original_client = OpenAIService.get_client

    def stop_on_client(self, provider):
        nonlocal stopped
        stopped = True
        return original_client(self, provider)

    monkeypatch.setattr(OpenAIService, "get_client", stop_on_client)
    report = asyncio.run(_worker(user, should_stop=lambda: stopped).run_work(work_id))
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "pending" and row.next_attempt_at == NOW and row.attempt_count == 0
    assert row.last_error_code == "operator_stopped"
    assert report.stopped and not report.provider_exhausted and not calls


def test_empty_recipes_are_skipped_by_schedule_postwrite_and_periodic_scan(unique_user_fn_scoped):
    from mealie.lang.providers import get_locale_provider
    from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler
    from mealie.services.recipe.recipe_service import RecipeService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    old_id = _schedule(user, recipe)
    recipe.recipe_ingredient = []
    recipe = user.repos.recipes.update(recipe.slug, recipe)
    assert _schedule(user, recipe) is None
    assert user.repos.session.get(HealthLabsRecipeMetadataEstimate, old_id).state == "superseded"
    service = RecipeService(
        user.repos,
        user.repos.users.get_one(user.user_id),
        user.repos.households.get_one(user.household_id),
        get_locale_provider("en-US"),
    )
    service._record_revision(recipe)
    RecipeMetadataScheduler(user.repos.session, now=NOW).scan_page(group_ids={user.group_id})
    assert (
        user.repos.session.scalar(
            sa.select(sa.func.count())
            .select_from(HealthLabsRecipeMetadataEstimate)
            .where(
                HealthLabsRecipeMetadataEstimate.recipe_id == recipe.id,
                HealthLabsRecipeMetadataEstimate.state != "superseded",
            )
        )
        == 0
    )


@pytest.mark.parametrize("historical_fingerprint", [False, True])
def test_existing_empty_jobs_close_before_any_provider_resolution(
    unique_user_fn_scoped, curator_settings, historical_fingerprint
):
    from mealie.services.healthlabs.recipe_metadata_service import POLICY_VERSION, RecipeMetadataService

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    if historical_fingerprint:
        work_id = _schedule(user, recipe)
    recipe.recipe_ingredient = []
    recipe = user.repos.recipes.update(recipe.slug, recipe)
    if not historical_fingerprint:
        inputs = RecipeMetadataService(user.repos.session).inputs(recipe)
        row = HealthLabsRecipeMetadataEstimate(
            group_id=user.group_id,
            recipe_id=recipe.id,
            input_fingerprint=inputs.input_fingerprint,
            authoring_fingerprint=inputs.authoring_fingerprint,
            policy_version=POLICY_VERSION,
            state="pending",
            next_attempt_at=NOW,
        )
        user.repos.session.add(row)
        user.repos.session.commit()
        work_id = row.id
    worker = _worker(user)

    def forbidden(*args):
        raise AssertionError("empty drafts must not resolve credentials")

    worker.provider_resolver = forbidden
    result = asyncio.run(worker.run_work(work_id))
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "superseded" and row.last_error_code == "empty_ingredients"
    assert row.attempt_count == 0 and result.requests == 0 and row.payload is None
    nutrition, _ = RecipeMetadataService(user.repos.session).project(recipe)
    assert nutrition.per_serving.kcal.value is None and not nutrition.complete


def test_per_job_request_bound_rejects_impossible_shape_before_inference(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    source.nutrient_values = {k: v for k, v in source.nutrient_values.items() if k != "fibre_g"}
    user.repos.session.commit()
    work_id = _schedule(user, recipe)
    calls = []
    _fake_client(monkeypatch, user.repos.session, calls)
    report = asyncio.run(_worker(user, limits=MetadataRequestLimits(max_requests_per_job=1)).run_work(work_id))
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "rejected" and row.last_error_code == "job_request_limit_exceeded"
    assert report.rejected == 1 and report.requests == 0 and row.attempt_count == 0 and not calls


@pytest.mark.parametrize("failure", ["schema", "missing_nutrient"])
def test_per_job_request_bound_also_counts_corrections(unique_user_fn_scoped, curator_settings, monkeypatch, failure):
    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    source.nutrient_values = {k: v for k, v in source.nutrient_values.items() if k != "fibre_g"}
    user.repos.session.commit()
    work_id = _schedule(user, recipe)
    calls = []

    def first_invalid(result):
        if len(calls) == 1:
            result["ingredients"][0]["nutrients_per_100g"]["fibre_g"] = -1 if failure == "schema" else None

    _fake_client(monkeypatch, user.repos.session, calls, transform=first_invalid)
    report = asyncio.run(_worker(user, limits=MetadataRequestLimits(max_requests_per_job=2)).run_work(work_id))
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "rejected" and row.last_error_code == "job_request_limit_exceeded"
    assert report.requests == len(calls) == 2 and not report.provider_exhausted
    if failure == "missing_nutrient":
        assert calls[1]["context"]["correction"] == {
            "code": "missing_required_nutrients",
            "reference_id": str(recipe.recipe_ingredient[0].reference_id),
            "fields": ["nutrients_per_100g.fibre_g"],
        }


def test_stop_after_a_chunk_preserves_same_day_resume_and_usage(unique_user_fn_scoped, curator_settings, monkeypatch):
    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    source.nutrient_values = {k: v for k, v in source.nutrient_values.items() if k != "fibre_g"}
    user.repos.session.commit()
    work_id = _schedule(user, recipe)
    stopped = False

    async def stop():
        nonlocal stopped
        stopped = True

    calls = []
    _fake_client(monkeypatch, user.repos.session, calls, mutate=stop)
    report = asyncio.run(_worker(user, should_stop=lambda: stopped).run_work(work_id))
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert report.stopped and report.requests == len(calls) == 1 and not report.provider_exhausted
    assert row.state == "pending" and row.next_attempt_at == NOW and row.attempt_count == 0
    assert row.usage["requests"] == 1
    _fake_client(monkeypatch, user.repos.session, calls)
    assert asyncio.run(_worker(user).run_work(work_id)).ready == 1


def test_stopped_run_once_reports_halt_without_claim(unique_user_fn_scoped, curator_settings):
    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    report = asyncio.run(_worker(user, should_stop=lambda: True).run_once())
    assert report.stopped and report.limit_reason == "operator_stopped" and report.claimed == 0
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "pending" and row.attempt_count == 0


@pytest.mark.parametrize("failure", ["quantity_assumption", "portion_amount"])
def test_assumed_portion_correction_obeys_real_job_request_cap(
    unique_user_fn_scoped, curator_settings, monkeypatch, failure
):
    from mealie.db.models.healthlabs import HealthLabsFoodPortion

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    recipe.recipe_ingredient[0].unit = None
    recipe = user.repos.recipes.update(recipe.slug, recipe)
    portion = HealthLabsFoodPortion(
        nutrient_food_id=source.id,
        description="piece",
        amount=1,
        unit="piece",
        grams=25,
    )
    user.repos.session.add(portion)
    user.repos.session.commit()
    work_id = _schedule(user, recipe)
    calls = []

    def missing_then_corrected(result):
        row = result["ingredients"][0]
        context = calls[-1]["context"]["ingredients"][0]
        assert context["fixed_grams"] is None
        row["portion_or_density_id"] = str(portion.id)
        row["assumed_portion_amount"] = None if len(calls) == 1 and failure == "portion_amount" else 2
        row["quantity_assumption"] = (
            None if len(calls) == 1 and failure == "quantity_assumption" else "Assume two pieces"
        )
        row["nutrients_per_100g"]["fibre_g"] = None

    _fake_client(monkeypatch, user.repos.session, calls, transform=missing_then_corrected)
    report = asyncio.run(_worker(user, limits=MetadataRequestLimits(max_requests_per_job=2)).run_work(work_id))
    row = user.repos.session.get(HealthLabsRecipeMetadataEstimate, work_id)
    assert row.state == "rejected" and row.last_error_code == "job_request_limit_exceeded"
    assert report.requests == len(calls) == 2 and not report.provider_exhausted
    assert calls[1]["context"]["correction"] == {
        "code": f"missing_{failure}",
        "reference_id": str(recipe.recipe_ingredient[0].reference_id),
        "fields": ["quantity_assumption"]
        if failure == "quantity_assumption"
        else ["assumed_portion_amount", "quantity_assumption"],
    }
    assert portion.grams == 25 and portion.amount == 1
