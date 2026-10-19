from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from mealie.core.config import get_app_settings
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsNutritionCuratorDecision,
    HealthLabsNutritionProviderUsage,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import RecipeIngredientModel
from mealie.db.models.users import User, UserType
from mealie.schema.healthlabs.nutrition_curator import NutritionCuratorProviderDecision
from mealie.schema.recipe import Recipe, SaveIngredientFood, SaveIngredientUnit
from mealie.services.healthlabs.nutrition_curator_prompt import NutritionCuratorPrompt
from mealie.services.healthlabs.nutrition_curator_service import NutritionCuratorService
from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorWorker
from mealie.services.healthlabs.nutrition_fingerprint_service import NutritionFingerprintService
from mealie.services.healthlabs.nutrition_provider_budget import NutritionProviderBudget
from mealie.services.openai import OpenAIService
from mealie.services.openai.openai import OpenAIRequestRejected
from tests.utils import TestUser

NOW = datetime(2000, 1, 1, 12, tzinfo=UTC)


@pytest.fixture
def curator_settings(monkeypatch: pytest.MonkeyPatch):
    values = {
        "HEALTHLABS_NUTRITION_CURATOR_ENABLED": "true",
        "HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT": "10",
        "HEALTHLABS_NUTRITION_PROVIDER_DAILY_BUDGET_USD": "1",
        "HEALTHLABS_NUTRITION_PROVIDER_INPUT_USD_PER_MILLION_TOKENS": "1",
        "HEALTHLABS_NUTRITION_PROVIDER_OUTPUT_USD_PER_MILLION_TOKENS": "1",
        "HEALTHLABS_NUTRITION_PROVIDER_MAX_OUTPUT_TOKENS": "64",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_app_settings.cache_clear()
    yield
    get_app_settings.cache_clear()


def _provider(user: TestUser) -> AIProvider:
    session = user.repos.session
    settings = session.scalar(sa.select(AIProviderSettings).where(AIProviderSettings.group_id == user.group_id))
    assert settings is not None
    provider = AIProvider(
        session=session,
        settings_id=settings.id,
        name=f"curator-{uuid4()}",
        base_url="https://provider.example/v1",
        api_key="offline-test-key",
        model="test/model-v1",
        timeout=30,
    )
    session.add(provider)
    session.flush()
    settings.default_provider_id = provider.id
    session.commit()
    return provider


def _food_work(user: TestUser, *, provider: bool = True):
    if provider:
        _provider(user)
    food = user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=f"raw apple {uuid4().hex[:8]}")
    )
    from mealie.db.models.healthlabs import HealthLabsNutrientFood, HealthLabsNutrientSourceRelease

    release = HealthLabsNutrientSourceRelease(
        provider=f"source-{uuid4()}",
        version="2026-10",
        country="GB",
        source_url="https://source.example/data",
        license_url="https://source.example/licence",
        attribution="Public test source",
        sha256=uuid4().hex.ljust(64, "0"),
    )
    user.repos.session.add(release)
    user.repos.session.flush()
    nutrient_food = HealthLabsNutrientFood(
        source_release_id=release.id,
        source_food_id=str(uuid4()),
        name=food.name,
        preparation_state="raw",
        nutrient_values={"kcal": {"amount": 50, "unit": "kcal"}},
    )
    user.repos.session.add(nutrient_food)
    user.repos.session.commit()
    fingerprint = NutritionFingerprintService(
        user.repos.session,
        country="GB",
        provider_enabled=True,
    ).food(food, None)
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="food_match",
        target_id=str(food.id),
        input_sha256=fingerprint,
        state="review_required",
        next_attempt_at=NOW,
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    return food, nutrient_food, work


def _ingredient_work(user: TestUser, *, provider: bool = True, food=None, unit=None):
    if provider:
        _provider(user)
    food = food or user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=f"shared apple {uuid4().hex[:8]}")
    )
    unit = unit or user.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=user.group_id, name=f"shared gram {uuid4().hex[:8]}")
    )
    source = f"1 {unit.name} {food.name}"
    recipe = user.repos.recipes.create(
        Recipe(
            user_id=user.user_id,
            group_id=user.group_id,
            household_id=user.household_id,
            name=f"Reuse recipe {uuid4()}",
        )
    )
    ingredient = RecipeIngredientModel(
        session=user.repos.session,
        group_id=user.group_id,
        recipe_id=recipe.id,
        position=0,
        original_text=source,
    )
    user.repos.session.add(ingredient)
    user.repos.session.commit()
    fingerprint = NutritionFingerprintService(
        user.repos.session,
        country="GB",
        provider_enabled=True,
    ).ingredient(ingredient, user.group_id)
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256=fingerprint,
        state="review_required",
        next_attempt_at=NOW,
        result={"suggestion": {"quantity": 1, "food": food.name, "unit": unit.name}},
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    return ingredient, food, unit, work


def _install_success(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, str]]) -> None:
    async def complete(
        self,
        prompt,
        message,
        *,
        response_schema,
        provider,
        max_completion_tokens,
        request_guard,
        usage_callback,
        disable_reasoning,
        **_kwargs,
    ):
        del self, provider
        calls.append((prompt, message))
        assert response_schema is NutritionCuratorProviderDecision
        assert max_completion_tokens == 64
        assert disable_reasoning is True
        if not request_guard(10, max_completion_tokens):
            raise OpenAIRequestRejected("budget exhausted")
        usage_callback({"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5})
        payload = message.removeprefix("<untrusted_context>\n").removesuffix("\n</untrusted_context>")
        import json

        context = json.loads(payload)
        selection = (
            {"nutrient_food_id": context["candidates"]["foods"][0]["id"]}
            if context["kind"] == "food_match"
            else {
                "food_id": context["candidates"]["foods"][0]["id"],
                "unit_id": context["candidates"]["units"][0]["id"],
            }
        )
        return response_schema.model_validate(
            {
                "decision": "select",
                "kind": context["kind"],
                **selection,
                "confidence": 0.99,
                "runner_up_confidence": 0.1,
                "reason_code": "name_and_context_match",
                "rationale": "I selected the supplied compatible candidates.",
            }
        )

    monkeypatch.setattr(OpenAIService, "get_response", complete)


def test_disabled_and_providerless_groups_make_no_claim_or_call(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = unique_user_fn_scoped
    _, _, work = _food_work(user, provider=False)
    calls: list[tuple[str, str]] = []
    _install_success(monkeypatch, calls)

    monkeypatch.setenv("HEALTHLABS_NUTRITION_CURATOR_ENABLED", "false")
    get_app_settings.cache_clear()
    report = NutritionCuratorWorker(user.repos.session, now=NOW).run_once()
    assert report.claimed == 0

    monkeypatch.setenv("HEALTHLABS_NUTRITION_CURATOR_ENABLED", "true")
    monkeypatch.setenv("HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT", "10")
    monkeypatch.setenv("HEALTHLABS_NUTRITION_PROVIDER_DAILY_BUDGET_USD", "1")
    monkeypatch.setenv("HEALTHLABS_NUTRITION_PROVIDER_INPUT_USD_PER_MILLION_TOKENS", "1")
    get_app_settings.cache_clear()
    report = NutritionCuratorWorker(user.repos.session, now=NOW).run_once()

    assert report.claimed == 0
    assert calls == []
    assert user.repos.session.get(HealthLabsNutritionWork, work.id) is not None
    assert (
        user.repos.session.scalar(
            sa.select(User.id).where(User.group_id == user.group_id, User.user_type == UserType.AGENT)
        )
        is None
    )


def test_worker_calls_only_bounded_prompt_and_finalizes_one_decision(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    _, _, first_work = _food_work(user)
    _, _, second_work = _food_work(user, provider=False)
    calls: list[tuple[str, str]] = []
    _install_success(monkeypatch, calls)

    report = NutritionCuratorWorker(user.repos.session, now=NOW).run_once()

    assert (report.claimed, report.approved, report.insufficient_evidence, report.failed) == (1, 1, 0, 0)
    assert len(calls) == 1
    prompt, message = calls[0]
    assert prompt == NutritionCuratorPrompt.system()
    assert message.startswith("<untrusted_context>\n") and message.endswith("\n</untrusted_context>")
    assert "offline-test-key" not in prompt + message
    assert "nutrient_values" not in message and "kcal" not in message
    decisions = user.repos.session.scalars(
        sa.select(HealthLabsNutritionCuratorDecision).where(
            HealthLabsNutritionCuratorDecision.group_id == user.group_id
        )
    ).all()
    assert len(decisions) == 1 and decisions[0].state == "approved"
    untouched = second_work if decisions[0].work_id == first_work.id else first_work
    assert user.repos.session.get(HealthLabsNutritionWork, untouched.id).state == "review_required"
    untouched.state = "completed"
    user.repos.session.commit()


def test_worker_reuses_identical_approved_decision_without_provider_call(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    first_ingredient, food, unit, first_work = _ingredient_work(user)
    second_ingredient, _, _, second_work = _ingredient_work(user, provider=False, food=food, unit=unit)
    calls: list[tuple[str, str]] = []
    _install_success(monkeypatch, calls)

    first = NutritionCuratorWorker(user.repos.session, now=NOW).run_once()
    second = NutritionCuratorWorker(user.repos.session, now=NOW).run_once()

    assert (first.approved, second.approved) == (1, 1)
    assert len(calls) == 1
    decisions = user.repos.session.scalars(
        sa.select(HealthLabsNutritionCuratorDecision)
        .where(HealthLabsNutritionCuratorDecision.group_id == user.group_id)
        .order_by(HealthLabsNutritionCuratorDecision.created_at, HealthLabsNutritionCuratorDecision.id)
    ).all()
    assert len(decisions) == 2
    assert {decision.target_id for decision in decisions} == {str(first_ingredient.id), str(second_ingredient.id)}
    reused = next(decision for decision in decisions if decision.reused_from_decision_id is not None)
    source = next(decision for decision in decisions if decision.id == reused.reused_from_decision_id)
    assert reused.id != source.id
    assert reused.work_id == second_work.id or reused.work_id == first_work.id
    assert reused.candidate_snapshot == source.candidate_snapshot
    assert reused.work_input_sha256 == source.work_input_sha256
    assert reused.selected_ids == source.selected_ids == {"food_id": str(food.id), "unit_id": str(unit.id)}
    assert reused.usage == {}


def test_worker_abstains_when_identical_approved_decisions_conflict(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    _, food, unit, _ = _ingredient_work(user)
    _, _, _, second_work = _ingredient_work(user, provider=False, food=food, unit=unit)
    calls: list[tuple[str, str]] = []
    _install_success(monkeypatch, calls)
    assert NutritionCuratorWorker(user.repos.session, now=NOW).run_once().approved == 1
    source = user.repos.session.scalar(
        sa.select(HealthLabsNutritionCuratorDecision).where(
            HealthLabsNutritionCuratorDecision.group_id == user.group_id,
            HealthLabsNutritionCuratorDecision.state == "approved",
        )
    )
    assert source is not None and source.reuse_sha256 is not None
    user.repos.session.add(
        HealthLabsNutritionCuratorDecision(
            group_id=source.group_id,
            kind=source.kind,
            target_id=f"historical-{uuid4()}",
            work_input_sha256=source.work_input_sha256,
            curator_sha256=uuid4().hex.ljust(64, "0"),
            reuse_sha256=source.reuse_sha256,
            agent_user_id=source.agent_user_id,
            state="approved",
            attempt_count=1,
            next_attempt_at=NOW,
            candidate_snapshot=source.candidate_snapshot,
            selected_ids={"food_id": str(uuid4()), "unit_id": str(unit.id)},
            confidence=0.99,
            runner_up_confidence=0.1,
            reason_code="name_and_context_match",
            rationale="I selected different supplied candidates.",
            provider_id=source.provider_id,
            provider_name=source.provider_name,
            provider_model=source.provider_model,
            prompt_version=source.prompt_version,
            requested_at=NOW,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    )
    user.repos.session.commit()

    report = NutritionCuratorWorker(user.repos.session, now=NOW).run_once()

    assert (report.claimed, report.approved, report.insufficient_evidence) == (1, 0, 1)
    assert len(calls) == 1
    decision = user.repos.session.scalar(
        sa.select(HealthLabsNutritionCuratorDecision).where(
            HealthLabsNutritionCuratorDecision.work_id == second_work.id
        )
    )
    assert decision is not None
    assert decision.state == "insufficient_evidence" and decision.reason_code == "reuse_conflict"
    assert decision.reused_from_decision_id is None
    second_work.state = "completed"
    user.repos.session.commit()


def test_terminal_decision_is_skipped_before_next_eligible_work(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    skipped_food, _, skipped_work = _food_work(user)
    _, _, eligible_work = _food_work(user, provider=False)
    skipped_work.next_attempt_at = NOW - timedelta(minutes=1)
    skipped_work.input_sha256 = NutritionFingerprintService(
        user.repos.session, country="GB", provider_enabled=True
    ).food(skipped_food, None)
    service = NutritionCuratorService(user.repos.session, now=NOW)
    skipped = service.start(skipped_work.id)
    assert skipped is not None
    decision = user.repos.session.get(HealthLabsNutritionCuratorDecision, skipped.decision_id)
    assert decision is not None
    decision.state = "insufficient_evidence"
    decision.lease_expires_at = None
    user.repos.session.commit()
    calls: list[tuple[str, str]] = []
    _install_success(monkeypatch, calls)

    report = NutritionCuratorWorker(user.repos.session, now=NOW).run_once()

    assert (report.claimed, report.approved) == (1, 1)
    assert len(calls) == 1
    assert user.repos.session.get(HealthLabsNutritionWork, eligible_work.id).state == "completed"


def test_provider_failures_retry_after_five_and_thirty_minutes_then_stop(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = unique_user_fn_scoped
    _, _, work = _food_work(user)

    async def fail(*_args, request_guard, max_completion_tokens, **_kwargs):
        assert request_guard(1, max_completion_tokens)
        raise Exception("secret provider wrapper") from ValueError("secret provider body must not be stored")

    monkeypatch.setattr(OpenAIService, "get_response", fail)
    times = (NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=35))
    expected = (
        ("pending", 1, NOW + timedelta(minutes=5), 0),
        ("pending", 2, NOW + timedelta(minutes=35), 0),
        ("failed", 3, NOW + timedelta(minutes=35), 1),
    )
    decision_id = None
    for now, expected_values in zip(times, expected, strict=True):
        report = NutritionCuratorWorker(user.repos.session, now=now).run_once()
        decision = user.repos.session.scalar(
            sa.select(HealthLabsNutritionCuratorDecision).where(HealthLabsNutritionCuratorDecision.work_id == work.id)
        )
        assert decision is not None
        decision_id = decision.id
        assert (decision.state, decision.attempt_count, decision.next_attempt_at, report.failed) == expected_values

    report = NutritionCuratorWorker(user.repos.session, now=NOW + timedelta(days=1)).run_once()
    assert report.claimed == 0
    assert user.repos.session.get(HealthLabsNutritionCuratorDecision, decision_id).state == "failed"
    assert caplog.text.count("Nutrition curator provider failure error_type=ValueError") == 3
    assert "secret provider wrapper" not in caplog.text
    assert "secret provider body" not in caplog.text


def test_lease_bounds_call_and_retry_starts_after_provider_finishes(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    _, _, work = _food_work(user)
    with pytest.raises(ValueError, match="lease_minutes"):
        NutritionCuratorWorker(user.repos.session, lease_minutes=1, now=NOW)

    worker = NutritionCuratorWorker(user.repos.session, lease_minutes=7, now=NOW)

    async def fail(*_args, request_guard, max_completion_tokens, **_kwargs):
        assert request_guard(1, max_completion_tokens)
        worker._fixed_now = NOW + timedelta(minutes=10)
        raise RuntimeError("provider failed after a long bounded call")

    monkeypatch.setattr(OpenAIService, "get_response", fail)
    worker.run_once()

    decision = user.repos.session.scalar(
        sa.select(HealthLabsNutritionCuratorDecision).where(HealthLabsNutritionCuratorDecision.work_id == work.id)
    )
    assert decision is not None
    assert decision.next_attempt_at == NOW + timedelta(minutes=15)


def test_active_lease_is_skipped_and_expired_lease_is_reclaimed(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    _, _, work = _food_work(user)
    started = NutritionCuratorService(user.repos.session, now=NOW).start(work.id)
    assert started is not None
    user.repos.session.commit()
    calls: list[tuple[str, str]] = []
    _install_success(monkeypatch, calls)

    active = NutritionCuratorWorker(user.repos.session, now=NOW + timedelta(minutes=1)).run_once()
    expired = NutritionCuratorWorker(user.repos.session, lease_minutes=7, now=NOW + timedelta(minutes=16)).run_once()

    assert active.claimed == 0
    assert (expired.claimed, expired.approved) == (1, 1)
    assert len(calls) == 1
    decision = user.repos.session.get(HealthLabsNutritionCuratorDecision, started.decision_id)
    assert decision is not None and decision.attempt_count == 2 and decision.state == "approved"


def test_budget_exhaustion_defers_without_consuming_attempt(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    _, _, work = _food_work(user)
    settings = get_app_settings()
    budget = NutritionProviderBudget.from_settings(user.repos.session, settings, now=lambda: NOW)
    assert budget.reserve(1, 1) is not None
    initial_usage = user.repos.session.scalar(
        sa.select(HealthLabsNutritionProviderUsage).where(HealthLabsNutritionProviderUsage.usage_date == NOW.date())
    )
    assert initial_usage is not None
    initial_request_count = initial_usage.request_count

    async def reject(*_args, request_guard, max_completion_tokens, **_kwargs):
        if not request_guard(1, max_completion_tokens):
            raise OpenAIRequestRejected("budget exhausted")
        raise AssertionError("shared request budget was not exhausted")

    monkeypatch.setattr(OpenAIService, "get_response", reject)
    monkeypatch.setenv("HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT", "1")
    get_app_settings.cache_clear()
    report = NutritionCuratorWorker(user.repos.session, now=NOW).run_once()

    assert (report.claimed, report.provider_exhausted) == (1, True)
    decision = user.repos.session.scalar(
        sa.select(HealthLabsNutritionCuratorDecision).where(HealthLabsNutritionCuratorDecision.work_id == work.id)
    )
    assert decision is not None
    assert (decision.state, decision.attempt_count, decision.next_attempt_at) == (
        "pending",
        0,
        datetime(2000, 1, 2, tzinfo=UTC),
    )
    usage = user.repos.session.scalar(
        sa.select(HealthLabsNutritionProviderUsage).where(HealthLabsNutritionProviderUsage.usage_date == NOW.date())
    )
    assert usage is not None and usage.request_count == initial_request_count
