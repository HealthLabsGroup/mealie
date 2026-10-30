import asyncio
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate as Job
from tests.integration_tests.healthlabs.test_recipe_metadata_reads import _recipe
from tests.integration_tests.healthlabs.test_recipe_metadata_worker import _fake_client, _resolver
from tests.integration_tests.healthlabs.test_recipe_metadata_worker import curator_settings as _curator_settings

curator_settings = _curator_settings


def _manifest(recipe):
    from mealie.services.healthlabs.recipe_metadata_operator import RecipeManifest

    return RecipeManifest.model_validate([{"recipe_id": recipe.id, "group_id": recipe.group_id, "scope": "personal"}])


def _factory(user):
    @contextmanager
    def factory():
        with Session(user.repos.session.get_bind()) as session:
            yield session

    return factory


def test_dry_run_has_no_writes_or_credential_reads(unique_user_fn_scoped, curator_settings):
    from mealie.services.healthlabs.recipe_metadata_operator import run_manifest

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)

    def forbidden(*_args):
        raise AssertionError("dry run resolved credentials")

    result = asyncio.run(run_manifest(_manifest(recipe), provider_resolver=forbidden, session_factory=_factory(user)))
    assert result["denominator"] == 1
    assert result["complete"] == 0
    assert result["nutrients"]["protein_g"]["source_backed"] == 1
    assert user.repos.session.scalar(sa.select(sa.func.count()).select_from(Job).where(Job.recipe_id == recipe.id)) == 0


def test_manifest_duplicate_and_resume_tamper_rejected(unique_user_fn_scoped, curator_settings):
    from mealie.services.healthlabs.recipe_metadata_operator import RecipeManifest, run_manifest

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    row = {"recipe_id": recipe.id, "group_id": recipe.group_id}
    with pytest.raises(ValueError):
        RecipeManifest.model_validate([row, row])
    with pytest.raises(ValueError, match="manifest"):
        asyncio.run(
            run_manifest(_manifest(recipe), checkpoint={"manifest_sha256": "bad"}, session_factory=_factory(user))
        )


def test_scope_moved_and_missing_are_honest_and_cannot_apply(unique_user_fn_scoped, curator_settings):
    from mealie.services.healthlabs.recipe_metadata_operator import RecipeManifest, run_manifest

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    manifest = RecipeManifest.model_validate(
        [{"recipe_id": recipe.id, "group_id": uuid4()}, {"recipe_id": uuid4(), "group_id": user.group_id}]
    )
    result = asyncio.run(run_manifest(manifest, session_factory=_factory(user)))
    assert result["denominator"] == 2
    assert {r["status"] for r in result["recipes"]} == {"scope_moved", "missing"}
    with pytest.raises(ValueError, match="scope"):
        asyncio.run(
            run_manifest(manifest, apply=True, provider_resolver=_resolver(user), session_factory=_factory(user))
        )


def test_apply_and_resume_use_existing_jobs_and_safe_checkpoint(unique_user_fn_scoped, curator_settings, monkeypatch):
    from mealie.services.healthlabs.recipe_metadata_operator import run_manifest

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    calls = []
    # The operator owns different sessions; this assertion checks no user-session transaction spans network.
    user.repos.session.commit()
    _fake_client(monkeypatch, user.repos.session, calls)
    options = {"apply": True, "provider_resolver": _resolver(user), "session_factory": _factory(user)}
    result = asyncio.run(run_manifest(_manifest(recipe), **options))
    assert result["complete"] == 1
    assert result["plant_quality"] == {"estimated": 1}
    assert result["requests"] == 1
    assert len(calls) == 1
    result2 = asyncio.run(run_manifest(_manifest(recipe), checkpoint=result, **options))
    assert result2["complete"] == 1 and result2["requests"] == 0
    assert len(calls) == 1
    assert "offline-secret" not in str(result)
    assert "Simple recipe" not in str(result)
    assert UUID(result["recipes"][0]["recipe_id"]) == recipe.id


def test_request_cap_stops_dispatch_and_resume_finishes(unique_user_fn_scoped, curator_settings, monkeypatch):
    from mealie.services.healthlabs.recipe_metadata_estimator import MetadataRequestLimits
    from mealie.services.healthlabs.recipe_metadata_operator import RecipeManifest, run_manifest

    user = unique_user_fn_scoped
    recipes = [_recipe(user)[0] for _ in range(3)]
    manifest = RecipeManifest.model_validate([{"recipe_id": r.id, "group_id": r.group_id} for r in recipes])
    calls = []
    user.repos.session.commit()
    _fake_client(monkeypatch, user.repos.session, calls)
    options = {"apply": True, "provider_resolver": _resolver(user), "session_factory": _factory(user), "concurrency": 1}
    result = asyncio.run(run_manifest(manifest, limits=MetadataRequestLimits(max_requests=1), **options))
    assert result["requests"] == len(calls) == 1
    assert result["halt_reason"] == "operator_request_limit"
    result2 = asyncio.run(run_manifest(manifest, checkpoint=result, **options))
    assert result2["complete"] == 3 and len(calls) == 3


def test_source_dry_run_is_pure_and_reconciliation_excludes_other_recipes(unique_user_fn_scoped, curator_settings):
    from mealie.db.models.healthlabs import HealthLabsNutritionWork
    from mealie.services.healthlabs.nutrition_worker import NutritionReconciliationWorker
    from mealie.services.healthlabs.recipe_metadata_operator import run_source_manifest
    from tests.integration_tests.healthlabs.test_nutrition_worker import _recipe_ingredients

    user = unique_user_fn_scoped
    first = _recipe_ingredients(user, ["impossible target a"])[0]
    other = _recipe_ingredients(user, ["impossible private b"])[0]
    from mealie.services.healthlabs.recipe_metadata_operator import RecipeManifest

    manifest = RecipeManifest.model_validate([{"recipe_id": first.recipe_id, "group_id": user.group_id}])
    result = asyncio.run(run_source_manifest(manifest, session_factory=_factory(user)))
    assert result["source"]["targets"]["ingredient_structure"] == 1
    assert not user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork.id).where(
            HealthLabsNutritionWork.target_id.in_([str(first.id), str(other.id)])
        )
    )
    worker = NutritionReconciliationWorker(
        user.repos.session,
        group_id=user.group_id,
        target_ids={"ingredient_structure": frozenset({str(first.id)})},
        deterministic_only=True,
    )
    asyncio.run(worker.run())
    assert user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork.id).where(HealthLabsNutritionWork.target_id == str(first.id))
    )
    assert not user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork.id).where(HealthLabsNutritionWork.target_id == str(other.id))
    )


@pytest.mark.parametrize("notification_fails", [False, True])
def test_source_approval_refresh_is_scoped_and_failure_cannot_undo_facts(
    unique_user_fn_scoped, curator_settings, monkeypatch, notification_fails
):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch
    from mealie.schema.recipe import Recipe, RecipeIngredient
    from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorWorker
    from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler
    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import NOW, _food_work, _install_success

    user = unique_user_fn_scoped
    food, source, work = _food_work(user)

    def recipe():
        return user.repos.recipes.create(
            Recipe(
                name=f"source hook {uuid4()}",
                user_id=user.user_id,
                group_id=user.group_id,
                household_id=user.household_id,
                recipe_servings=2,
                recipe_ingredient=[
                    RecipeIngredient(reference_id=uuid4(), food=food, quantity=100, original_text="100 g apple")
                ],
            )
        )

    allowed, private = recipe(), recipe()
    original = allowed.model_dump(mode="json")
    _install_success(monkeypatch, [])
    if notification_fails:

        def fail(*args, **kwargs):
            raise RuntimeError("enqueue unavailable")

        monkeypatch.setattr(RecipeMetadataScheduler, "schedule", fail)
    result = asyncio.run(
        NutritionCuratorWorker(user.repos.session, now=NOW, recipe_ids=frozenset({allowed.id})).run_work(work.id)
    )
    assert result.approved == 1
    assert user.repos.session.scalar(
        sa.select(HealthLabsFoodMatch.id).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
    )
    assert user.repos.recipes.get_one(allowed.id, "id").model_dump(mode="json") == original
    assert not user.repos.session.scalar(sa.select(Job.id).where(Job.recipe_id == private.id))
    assert bool(user.repos.session.scalar(sa.select(Job.id).where(Job.recipe_id == allowed.id))) == (
        not notification_fails
    )


def test_bounded_source_sdk_physical_request_is_not_retried(unique_user_fn_scoped, curator_settings):
    import httpx2

    from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorWorker
    from mealie.services.healthlabs.recipe_metadata_estimator import MetadataRequestLimits
    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import NOW, _food_work

    user = unique_user_fn_scoped
    _, _, work = _food_work(user)
    seen = []
    limits = MetadataRequestLimits(max_requests=1)

    def http(request):
        seen.append(request)
        # A retry would get a different 200 response; it must never happen.
        return httpx2.Response(500 if len(seen) == 1 else 200, json={"error": {"message": "offline"}})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(http)) as client:
            return await NutritionCuratorWorker(
                user.repos.session,
                now=NOW,
                work_ids={work.id},
                request_limits=limits,
                sdk_max_retries=0,
                http_client=client,
            ).run_work(work.id)

    report = asyncio.run(run())
    assert len(seen) == limits.requests == 1
    assert report.provider_error is True
    assert seen[0].url.host == "provider.example"


def test_terminal_retry_requires_matching_current_fingerprint_state_reason(unique_user_fn_scoped, curator_settings):
    from mealie.services.healthlabs.recipe_metadata_estimator import MetadataRequestLimits
    from mealie.services.healthlabs.recipe_metadata_operator import retry_terminal
    from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataWorker
    from tests.integration_tests.healthlabs.test_recipe_metadata_worker import _schedule

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    work_id = _schedule(user, recipe)
    # Preflight input policy rejection is a true terminal state.
    asyncio.run(
        RecipeMetadataWorker(
            user.repos.session,
            provider_resolver=_resolver(user),
            group_ids={user.group_id},
            limits=MetadataRequestLimits(max_input_tokens=1024),
        ).run_work(work_id)
    )
    row = user.repos.session.get(Job, work_id)
    assert row.state == "rejected"
    expectation = {
        "work_id": work_id,
        "expected_fingerprint": row.input_fingerprint,
        "expected_state": "rejected",
        "expected_reason": "input_limit_exceeded",
        "operator_reason": "input_limit_corrected",
        "session_factory": _factory(user),
    }
    user.repos.session.commit()
    assert not retry_terminal(_manifest(recipe), **{**expectation, "expected_fingerprint": "0" * 64})
    assert not retry_terminal(_manifest(recipe), **{**expectation, "expected_reason": "wrong_reason"})
    assert retry_terminal(_manifest(recipe), **expectation)
    assert not retry_terminal(_manifest(recipe), **expectation)


@pytest.fixture(autouse=True)
def isolated_operator_budget(unique_user_fn_scoped):
    from mealie.db.models.healthlabs import HealthLabsNutritionProviderUsage

    session = unique_user_fn_scoped.repos.session
    session.execute(sa.delete(HealthLabsNutritionProviderUsage))
    session.commit()


@pytest.mark.parametrize("session_override", [False, True])
def test_operator_actual_http_identity_and_credential_redaction(
    unique_user_fn_scoped, curator_settings, session_override
):
    import json

    import httpx2

    from mealie.services.healthlabs.recipe_metadata_operator import run_manifest
    from mealie.services.healthlabs.revision_service import RecipeRevisionService
    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import _provider

    user = unique_user_fn_scoped
    provider = _provider(user)
    provider.base_url = "https://openrouter.ai/api/v1"
    provider.model = "normal/model"
    user.repos.session.commit()
    recipe, _ = _recipe(user)
    revision = RecipeRevisionService(user.repos.session).record(recipe, actor_user_id=user.user_id)
    old_snapshot = json.dumps(revision.snapshot, sort_keys=True)
    old_hash = revision.content_sha256
    seen = []
    sql = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        sql.append(str(parameters))

    def http(request):
        body = json.loads(request.content)
        seen.append((request.url.host, body["model"], request.headers["authorization"]))
        context = json.loads(body["messages"][1]["content"][0]["text"])
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
                "plants_rationale": "All references inspected",
            },
        }
        return httpx2.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(plan)}}
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150},
            },
        )

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(http)) as client:
            return await run_manifest(
                _manifest(recipe),
                apply=True,
                provider_resolver=_resolver(user) if session_override else None,
                session_factory=_factory(user),
                http_client=client,
            )

    user.repos.session.commit()
    engine = user.repos.session.get_bind()
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        result = asyncio.run(run())
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert result["complete"] == 1
    assert (
        seen == [("azure.example", "offline-deployment", "Bearer offline-secret")]
        if session_override
        else seen == [("openrouter.ai", "normal/model", "Bearer offline-test-key")]
    )
    assert "offline-secret" not in str(sql) + json.dumps(result)
    user.repos.session.refresh(revision)
    assert json.dumps(revision.snapshot, sort_keys=True) == old_snapshot and revision.content_sha256 == old_hash


def test_operator_background_competitor_cannot_claim_active_job(unique_user_fn_scoped, curator_settings, monkeypatch):
    from mealie.services.healthlabs.recipe_metadata_operator import run_manifest
    from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataWorker
    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import _provider

    user = unique_user_fn_scoped
    _provider(user)
    recipe, _ = _recipe(user)
    calls = []

    async def competing():
        with _factory(user)() as session:
            worker = RecipeMetadataWorker(session, group_ids={user.group_id}, recipe_ids={recipe.id})
            assert (await worker.run_once()).claimed == 0

    user.repos.session.commit()
    _fake_client(monkeypatch, user.repos.session, calls, mutate=competing)
    result = asyncio.run(
        run_manifest(_manifest(recipe), apply=True, provider_resolver=_resolver(user), session_factory=_factory(user))
    )
    assert result["complete"] == 1 and len(calls) == 1


def test_scoped_curator_rejects_target_moved_outside_manifest_during_http(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    from mealie.db.models.recipe import RecipeIngredientModel
    from mealie.schema.recipe import Recipe
    from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorWorker
    from mealie.services.openai import OpenAIService
    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import NOW, _ingredient_work, _install_success

    user = unique_user_fn_scoped
    ingredient, _, _, work = _ingredient_work(user)
    allowed_id = ingredient.recipe_id
    private = user.repos.recipes.create(
        Recipe(
            name=f"private move {uuid4()}", user_id=user.user_id, group_id=user.group_id, household_id=user.household_id
        )
    )
    _install_success(monkeypatch, [])
    original = OpenAIService.get_response

    async def move(self, *args, **kwargs):
        response = await original(self, *args, **kwargs)
        with _factory(user)() as session:
            session.execute(
                sa.update(RecipeIngredientModel)
                .where(RecipeIngredientModel.id == ingredient.id)
                .values(recipe_id=private.id)
            )
            session.commit()
        return response

    monkeypatch.setattr(OpenAIService, "get_response", move)
    report = asyncio.run(
        NutritionCuratorWorker(user.repos.session, now=NOW, recipe_ids=frozenset({allowed_id})).run_work(work.id)
    )
    assert report.approved == 0 and report.insufficient_evidence == 1
    user.repos.session.refresh(ingredient)
    assert ingredient.food_id is None and ingredient.unit_id is None and ingredient.quantity in (None, 0)


def test_stop_before_apply_does_not_read_credentials_or_schedule(unique_user_fn_scoped, curator_settings):
    from mealie.services.healthlabs.recipe_metadata_operator import run_manifest

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)

    def forbidden(*args):
        raise AssertionError("stopped run read credential")

    result = asyncio.run(
        run_manifest(
            _manifest(recipe),
            apply=True,
            should_stop=lambda: True,
            provider_resolver=forbidden,
            session_factory=_factory(user),
        )
    )
    assert result["halt_reason"] == "operator_stopped" and result["requests"] == 0
    assert not user.repos.session.scalar(sa.select(Job.id).where(Job.recipe_id == recipe.id))


def test_source_refresh_recomposes_retained_ready_evidence_without_new_inference(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    import copy

    from mealie.services.healthlabs.recipe_metadata_operator import run_manifest
    from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService
    from mealie.services.healthlabs.recipe_metadata_source_refresh import refresh_source_recipes

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    calls = []
    user.repos.session.commit()
    _fake_client(monkeypatch, user.repos.session, calls)
    assert (
        asyncio.run(
            run_manifest(
                _manifest(recipe), apply=True, provider_resolver=_resolver(user), session_factory=_factory(user)
            )
        )["complete"]
        == 1
    )
    historical = user.repos.session.scalar(sa.select(Job).where(Job.recipe_id == recipe.id))
    payload = copy.deepcopy(historical.payload)
    source.nutrient_values = {**source.nutrient_values, "protein_g": {"amount": 20, "unit": "g"}}
    user.repos.session.commit()
    assert (
        refresh_source_recipes(
            user.repos.session,
            group_id=user.group_id,
            kind="food_match",
            target_id=str(recipe.recipe_ingredient[0].food.id),
            recipe_ids=frozenset({recipe.id}),
        )
        == 1
    )
    user.repos.session.commit()
    jobs = list(user.repos.session.scalars(sa.select(Job).where(Job.recipe_id == recipe.id)))
    assert len(jobs) == 2 and all(j.state == "ready" and j.attempt_count in (0, 1) for j in jobs)
    user.repos.session.refresh(historical)
    assert historical.payload == payload and len(calls) == 1
    nutrition, metadata = RecipeMetadataService(user.repos.session).project(recipe)
    assert nutrition.per_serving.protein_g.value == 10 and metadata.prep_minutes.value == 5


def test_source_operator_applies_only_manifest_target_and_resumes_without_paid_replay(
    unique_user_fn_scoped, curator_settings, monkeypatch
):
    from mealie.db.models.healthlabs import HealthLabsNutritionWork
    from mealie.schema.recipe import Recipe, RecipeIngredient, SaveIngredientFood
    from mealie.services.healthlabs.recipe_metadata_operator import run_source_manifest
    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import NOW, _food_work, _install_success

    user = unique_user_fn_scoped
    food, _, work = _food_work(user)
    recipe = user.repos.recipes.create(
        Recipe(
            name=f"scoped source {uuid4()}",
            user_id=user.user_id,
            group_id=user.group_id,
            household_id=user.household_id,
            recipe_servings=2,
            recipe_ingredient=[RecipeIngredient(reference_id=uuid4(), food=food, quantity=100)],
        )
    )
    other = user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=f"private food {uuid4()}")
    )
    unrelated = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="food_match",
        target_id=str(other.id),
        input_sha256="0" * 64,
        state="pending",
        next_attempt_at=NOW,
    )
    user.repos.session.add(unrelated)
    user.repos.session.commit()
    calls = []
    _install_success(monkeypatch, calls)
    options = {
        "apply": True,
        "provider_resolver": _resolver(user),
        "session_factory": _factory(user),
        "max_work": 1,
        "reconciliation_rounds": 2,
    }
    result = asyncio.run(run_source_manifest(_manifest(recipe), **options))
    assert result["source"]["approved"] == result["requests"] == len(calls) == 1
    receipt = result["source"]["work"][0]
    assert receipt["work_id"] == str(work.id)
    assert receipt["decision"]["model"] == "offline-deployment"
    assert receipt["decision"]["usage"]["input_tokens"] == 10
    assert result["complete"] == 0  # A successful source choice alone cannot claim full metadata.
    assert asyncio.run(run_source_manifest(_manifest(recipe), **options))["requests"] == 0
    user.repos.session.refresh(unrelated)
    assert unrelated.state == "pending" and unrelated.input_sha256 == "0" * 64
    assert len(calls) == 1


def test_source_operator_stops_on_actual_http_throttle(unique_user_fn_scoped, curator_settings):
    import httpx2

    from mealie.schema.recipe import Recipe, RecipeIngredient
    from mealie.services.healthlabs.recipe_metadata_operator import run_source_manifest
    from tests.integration_tests.healthlabs.test_nutrition_curator_worker import _food_work

    user = unique_user_fn_scoped
    food, _, _ = _food_work(user)
    recipe = user.repos.recipes.create(
        Recipe(
            name=f"throttle source {uuid4()}",
            user_id=user.user_id,
            group_id=user.group_id,
            household_id=user.household_id,
            recipe_servings=2,
            recipe_ingredient=[RecipeIngredient(reference_id=uuid4(), food=food, quantity=100)],
        )
    )
    requests = []

    def http(request):
        requests.append(request)
        return httpx2.Response(429, json={"error": {"message": "offline throttle"}})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(http)) as client:
            return await run_source_manifest(
                _manifest(recipe),
                apply=True,
                provider_resolver=_resolver(user),
                session_factory=_factory(user),
                reconciliation_rounds=0,
                http_client=client,
            )

    result = asyncio.run(run())
    assert result["source"]["halt_reason"] == "provider_throttled"
    assert result["requests"] == len(requests) == result["source"]["provider_errors"] == 1
    assert result["source"]["work"][0]["decision"]["state"] == "pending"
    assert result["source"]["work"][0]["decision"]["reason"] == "provider_failure"
