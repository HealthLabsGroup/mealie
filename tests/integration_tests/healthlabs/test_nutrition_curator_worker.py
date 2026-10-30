from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from mealie.core import exceptions
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
from mealie.services.healthlabs.nutrition_curator_prompt import PROMPT_VERSION, NutritionCuratorPrompt
from mealie.services.healthlabs.nutrition_curator_service import DECISION_VERSION, NutritionCuratorService
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
        del provider
        assert not self.repos.session.in_transaction()
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


def test_due_provider_retry_is_ordered_before_ordinary_work(
    unique_user_fn_scoped: TestUser,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    _, _, ordinary_work = _food_work(user)
    _, _, retry_work = _food_work(user, provider=False)
    ordinary_work.next_attempt_at = NOW - timedelta(days=1)
    started = NutritionCuratorService(user.repos.session, now=NOW).start(retry_work.id)
    assert started is not None
    decision = user.repos.session.get(HealthLabsNutritionCuratorDecision, started.decision_id)
    assert decision is not None
    decision.state = "pending"
    decision.reason_code = "provider_failure"
    decision.next_attempt_at = NOW
    decision.lease_expires_at = None
    user.repos.session.commit()

    work_ids = NutritionCuratorWorker(user.repos.session, now=NOW).work_ids()

    assert work_ids[:2] == [retry_work.id, ordinary_work.id]


def test_rate_limit_freezes_supervisor_growth_signal(
    unique_user_fn_scoped: TestUser,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    _, _, work = _food_work(user)
    started = NutritionCuratorService(user.repos.session, now=NOW).start(work.id)
    assert started is not None
    user.repos.session.commit()
    worker = NutritionCuratorWorker(user.repos.session, now=NOW)

    report = worker._fail(started.decision_id, exceptions.RateLimitError("provider throttled"), NOW)

    assert report.provider_error is True
    assert report.throttled is True


def test_failed_previous_response_contract_gets_one_new_audited_attempt(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    curator_settings,
) -> None:
    user = unique_user_fn_scoped
    food, _, work = _food_work(user)
    _, _, earlier_work = _food_work(user, provider=False)
    work.input_sha256 = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True).food(
        food, None
    )
    earlier_work.next_attempt_at = NOW - timedelta(days=1)
    user.repos.session.commit()
    service = NutritionCuratorService(user.repos.session, now=NOW)
    started = service.start(work.id)
    assert started is not None
    failed = user.repos.session.get(HealthLabsNutritionCuratorDecision, started.decision_id)
    assert failed is not None
    failed.state = "failed"
    failed.reason_code = "provider_failure"
    failed.prompt_version = PROMPT_VERSION
    failed.lease_expires_at = None
    user.repos.session.commit()
    calls: list[tuple[str, str]] = []
    _install_success(monkeypatch, calls)

    report = NutritionCuratorWorker(user.repos.session, now=NOW + timedelta(days=1)).run_once()

    assert (report.claimed, report.approved) == (1, 1)
    assert len(calls) == 1
    assert user.repos.session.get(HealthLabsNutritionWork, earlier_work.id).state == "review_required"
    decisions = user.repos.session.scalars(
        sa.select(HealthLabsNutritionCuratorDecision)
        .where(HealthLabsNutritionCuratorDecision.work_id == work.id)
        .order_by(HealthLabsNutritionCuratorDecision.created_at, HealthLabsNutritionCuratorDecision.id)
    ).all()
    assert len(decisions) == 2
    assert decisions[0].state == "failed"
    assert decisions[1].state == "approved"
    assert decisions[1].curator_sha256 != decisions[0].curator_sha256
    assert decisions[1].prompt_version == DECISION_VERSION


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


def test_session_override_without_default_is_scoped_truthful_and_memory_only(
    unique_user_fn_scoped: TestUser,
    curator_settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    from mealie.services.healthlabs.nutrition_provider_resolution import SessionProviderResolver

    user = unique_user_fn_scoped
    _, nutrient_food, work = _food_work(user, provider=False)
    identity = uuid4()
    resolver = SessionProviderResolver(
        identity_ids={user.group_id: identity},
        name="session-provider",
        base_url="https://azure.example/openai/v1",
        model="gpt-5.6-sol",
        credential=lambda: "fake-session-secret",
    )
    statements = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    engine = user.repos.session.get_bind()
    sa.event.listen(engine, "before_cursor_execute", capture)
    calls = []
    _install_success(monkeypatch, calls)
    original = OpenAIService.get_response

    async def inspect_transport(self, *args, provider, **kwargs):
        assert provider.id == identity
        assert provider.model == "gpt-5.6-sol"
        assert provider.base_url == "https://azure.example/openai/v1"
        assert provider.api_key == "fake-session-secret"
        return await original(self, *args, provider=provider, **kwargs)

    monkeypatch.setattr(OpenAIService, "get_response", inspect_transport)
    try:
        worker = NutritionCuratorWorker(
            user.repos.session, now=NOW + timedelta(days=365), provider_resolver=resolver, group_ids={user.group_id}
        )
        assert work.id in worker.work_ids()
        assert (
            NutritionCuratorWorker(
                user.repos.session, now=NOW + timedelta(days=365), provider_resolver=resolver, group_ids={uuid4()}
            ).work_ids()
            == []
        )
        report = worker.run_once()
        assert report.approved == 1
        decision = user.repos.session.scalar(
            sa.select(HealthLabsNutritionCuratorDecision).where(HealthLabsNutritionCuratorDecision.work_id == work.id)
        )
        assert decision.provider_id is None
        assert decision.provider_name == "session-provider"
        assert decision.provider_model == "gpt-5.6-sol"
        assert len(calls) == 1
        assert "fake-session-secret" not in repr(statements) + caplog.text + repr(decision.candidate_snapshot)
        assert (
            user.repos.session.scalar(
                sa.select(sa.func.count(AIProvider.id))
                .join(AIProviderSettings, AIProvider.settings_id == AIProviderSettings.id)
                .where(AIProviderSettings.group_id == user.group_id)
            )
            == 0
        )
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("change", ["model", "identity", "work_input", "default_path"])
def test_runtime_apply_rejects_changed_identity_or_stale_work(unique_user_fn_scoped: TestUser, change: str):
    from mealie.db.models.healthlabs import HealthLabsFoodMatch
    from mealie.services.healthlabs.nutrition_curator_service import StartedCuratorDecision
    from mealie.services.healthlabs.nutrition_provider_resolution import SessionProviderResolver

    user = unique_user_fn_scoped
    food, nutrient_food, work = _food_work(user, provider=False)
    identity = uuid4()
    values = {
        "identity_ids": {user.group_id: identity},
        "name": "session-provider",
        "base_url": "https://azure.example/openai/v1",
        "model": "gpt-5.6-sol",
        "credential": lambda: "fake-session-secret",
    }
    service = NutritionCuratorService(user.repos.session, now=NOW, provider_resolver=SessionProviderResolver(**values))
    started = service.start(work.id)
    assert isinstance(started, StartedCuratorDecision)
    user.repos.session.commit()
    response = NutritionCuratorProviderDecision(
        decision="select",
        kind="food_match",
        nutrient_food_id=nutrient_food.id,
        confidence=0.99,
        runner_up_confidence=0.1,
        reason_code="name_and_context_match",
        rationale="Supplied candidate.",
    )
    if change == "model":
        values["model"] = "other/model"
    elif change == "identity":
        values["identity_ids"] = {user.group_id: uuid4()}
    elif change == "work_input":
        work.input_sha256 = "0" * 64
        user.repos.session.commit()
    final_resolver = None if change == "default_path" else SessionProviderResolver(**values)
    finalizer = NutritionCuratorService(user.repos.session, now=NOW, provider_resolver=final_resolver)
    assert finalizer.apply(started.decision_id, response, {}) == "insufficient_evidence"
    user.repos.session.commit()
    assert (
        user.repos.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
        )
        is None
    )


def test_missing_session_key_or_foreign_group_cannot_create_decision_or_agent(unique_user_fn_scoped: TestUser):
    from mealie.services.healthlabs.nutrition_provider_resolution import SessionProviderResolver

    user = unique_user_fn_scoped
    _, _, work = _food_work(user, provider=False)
    for groups, key in [({uuid4(): uuid4()}, "fake-session-secret"), ({user.group_id: uuid4()}, "")]:
        resolver = SessionProviderResolver(
            identity_ids=groups,
            name="session-provider",
            base_url="https://azure.example/openai/v1",
            model="gpt-5.6-sol",
            credential=lambda key=key: key,
        )
        service = NutritionCuratorService(user.repos.session, now=NOW, provider_resolver=resolver)
        if key:
            assert service.start(work.id) is None
        else:
            with pytest.raises(ValueError, match="credential unavailable"):
                service.start(work.id)
    assert (
        user.repos.session.scalar(
            sa.select(HealthLabsNutritionCuratorDecision.id).where(
                HealthLabsNutritionCuratorDecision.work_id == work.id
            )
        )
        is None
    )
    assert (
        user.repos.session.scalar(
            sa.select(User.id).where(User.group_id == user.group_id, User.user_type == UserType.AGENT)
        )
        is None
    )


def test_session_manifest_dry_run_respects_work_bound_and_needs_no_key(
    unique_user_fn_scoped: TestUser,
    curator_settings,
    monkeypatch: pytest.MonkeyPatch,
):
    import asyncio
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).parents[3] / "scripts/healthlabs/run-session-nutrition-curator.py"
    spec = importlib.util.spec_from_file_location("session_curator_operator", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    user = unique_user_fn_scoped
    _, _, work = _food_work(user, provider=False)
    monkeypatch.delenv("FAKE_SESSION_TEST_KEY", raising=False)
    manifest = module.SessionManifest(
        identity_ids={user.group_id: uuid4()},
        work_ids=[work.id, work.id, uuid4()],
        name="session-provider",
        base_url="https://azure.example/openai/v1",
        model="gpt-5.6-sol",
    )
    counts = asyncio.run(module.run_manifest(manifest, credential_env="FAKE_SESSION_TEST_KEY", apply=False, max_work=1))
    assert counts == {"eligible": 1, "claimed": 0, "approved": 0, "insufficient_evidence": 0, "failed": 0}
    assert (
        user.repos.session.scalar(
            sa.select(HealthLabsNutritionCuratorDecision.id).where(
                HealthLabsNutritionCuratorDecision.work_id == work.id
            )
        )
        is None
    )
    with pytest.raises(ValueError, match="credential unavailable"):
        asyncio.run(module.run_manifest(manifest, credential_env="FAKE_SESSION_TEST_KEY", apply=True, max_work=1))
    with pytest.raises(ValueError, match="max_work must be positive"):
        asyncio.run(module.run_manifest(manifest, credential_env="FAKE_SESSION_TEST_KEY", apply=False, max_work=0))


def _legacy_provider_context(provider):
    return {
        "id": str(provider.id),
        "name": provider.name,
        "baseUrl": provider.base_url,
        "model": provider.model,
        "timeout": provider.timeout,
        "headers": sorted(
            (item.key_name or "", item.update_at.isoformat() if item.update_at else "")
            for item in provider.request_headers
        ),
        "params": sorted(
            (item.key_name or "", item.update_at.isoformat() if item.update_at else "")
            for item in provider.request_params
        ),
    }


@pytest.mark.parametrize("outcome", ["insufficient_evidence", "failed", "pending", "approved_reuse"])
def test_persisted_legacy_hash_preserves_decisions_and_retry_timing(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
):
    from mealie.db.models.group.ai_providers import AIProviderHeaders, AIProviderParams
    from mealie.services.healthlabs.nutrition_curator_service import FinalizedCuratorDecision, StartedCuratorDecision
    from mealie.services.healthlabs.nutrition_provider_resolution import ResolvedCuratorProvider

    user = unique_user_fn_scoped
    _, food, unit, work = _ingredient_work(user)
    provider = user.repos.session.scalar(
        sa.select(AIProvider)
        .join(AIProviderSettings, AIProvider.settings_id == AIProviderSettings.id)
        .where(AIProviderSettings.group_id == user.group_id, AIProvider.id == AIProviderSettings.default_provider_id)
    )
    provider.request_headers.append(AIProviderHeaders("X-Client", "curator"))
    provider.request_params.append(AIProviderParams("temperature", "0"))
    user.repos.session.commit()
    work.input_sha256 = NutritionFingerprintService(
        user.repos.session, country="GB", provider_enabled=True
    ).current_work_input(work)
    user.repos.session.commit()
    second_work = None
    if outcome == "approved_reuse":
        _, _, _, second_work = _ingredient_work(user, provider=False, food=food, unit=unit)
    legacy_context = _legacy_provider_context(provider)
    with monkeypatch.context() as old_code:
        old_code.setattr(ResolvedCuratorProvider, "identity_context", lambda self: legacy_context)
        service = NutritionCuratorService(user.repos.session, now=NOW)
        started = service.start(work.id)
        assert isinstance(started, StartedCuratorDecision)
        if outcome in {"failed", "pending"}:
            assert service.fail(started.decision_id, RuntimeError("offline failure")) == "pending"
            user.repos.session.commit()
            if outcome == "failed":
                for due in (NOW + timedelta(minutes=5), NOW + timedelta(minutes=35)):
                    retry_service = NutritionCuratorService(user.repos.session, now=due)
                    retry = retry_service.start(work.id)
                    assert retry.decision_id == started.decision_id
                    retry_service.fail(retry.decision_id, RuntimeError("offline failure"))
                    user.repos.session.commit()
        else:
            response = NutritionCuratorProviderDecision(
                decision="select" if outcome == "approved_reuse" else "insufficient_evidence",
                kind="ingredient_structure",
                food_id=food.id if outcome == "approved_reuse" else None,
                unit_id=unit.id if outcome == "approved_reuse" else None,
                confidence=0.99 if outcome == "approved_reuse" else 0,
                runner_up_confidence=0,
                reason_code="name_and_context_match" if outcome == "approved_reuse" else "insufficient_candidates",
                rationale="Supplied existing candidates only.",
            )
            assert service.apply(started.decision_id, response, {}) == (
                "approved" if outcome == "approved_reuse" else "insufficient_evidence"
            )
            user.repos.session.commit()
    upgraded = NutritionCuratorService(user.repos.session, now=NOW + timedelta(minutes=1))
    if outcome == "approved_reuse":
        reused = upgraded.start(second_work.id)
        assert isinstance(reused, FinalizedCuratorDecision) and reused.state == "approved"
        decision = user.repos.session.get(HealthLabsNutritionCuratorDecision, reused.decision_id)
        assert decision.reused_from_decision_id == started.decision_id
    else:
        assert upgraded.start(work.id) is None
        decision = user.repos.session.get(HealthLabsNutritionCuratorDecision, started.decision_id)
        assert decision.state == outcome
        assert decision.attempt_count == (3 if outcome == "failed" else 1)
        if outcome == "pending":
            assert decision.next_attempt_at == NOW + timedelta(minutes=5)
            user.repos.session.commit()
            retry = NutritionCuratorService(user.repos.session, now=NOW + timedelta(minutes=5)).start(work.id)
            assert isinstance(retry, StartedCuratorDecision) and retry.decision_id == started.decision_id
    user.repos.session.commit()


@pytest.mark.parametrize("prefix_state", ["insufficient_evidence", "running", "pending"])
def test_session_manifest_advances_past_unclaimable_prefix(
    unique_user_fn_scoped: TestUser,
    curator_settings,
    monkeypatch: pytest.MonkeyPatch,
    prefix_state: str,
):
    import asyncio
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).parents[3] / "scripts/healthlabs/run-session-nutrition-curator.py"
    spec = importlib.util.spec_from_file_location("session_curator_resume_operator", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    user = unique_user_fn_scoped
    works = [_food_work(user, provider=False)[2] for _ in range(3)]
    fingerprints = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True)
    for work in works:
        work.input_sha256 = fingerprints.current_work_input(work)
    user.repos.session.commit()
    monkeypatch.setenv("FAKE_SESSION_RESUME_KEY", "fake-session-secret")
    manifest = module.SessionManifest(
        identity_ids={user.group_id: uuid4()},
        work_ids=[work.id for work in works],
        name="session-provider",
        base_url="https://azure.example/openai/v1",
        model="gpt-5.6-sol",
    )
    now = datetime.now(UTC)
    service = NutritionCuratorService(
        user.repos.session, now=now, provider_resolver=manifest.resolver("FAKE_SESSION_RESUME_KEY")
    )
    started = service.start(works[0].id)
    if prefix_state == "insufficient_evidence":
        response = NutritionCuratorProviderDecision(
            decision="insufficient_evidence",
            kind="food_match",
            confidence=0,
            runner_up_confidence=0,
            reason_code="insufficient_candidates",
            rationale="No safe choice.",
        )
        assert service.apply(started.decision_id, response, {}) == prefix_state
    elif prefix_state == "pending":
        assert service.fail(started.decision_id, RuntimeError("offline failure")) == prefix_state
    user.repos.session.commit()
    calls = []
    _install_success(monkeypatch, calls)
    for index in (1, 2):
        counts = asyncio.run(
            module.run_manifest(manifest, credential_env="FAKE_SESSION_RESUME_KEY", apply=True, max_work=1)
        )
        assert counts["claimed"] == 1 and counts["approved"] == 1
        assert len(calls) == index
        user.repos.session.expire_all()
        decision = user.repos.session.scalar(
            sa.select(HealthLabsNutritionCuratorDecision).where(
                HealthLabsNutritionCuratorDecision.work_id == works[index].id
            )
        )
        assert decision.state == "approved"
    unchanged = user.repos.session.get(HealthLabsNutritionCuratorDecision, started.decision_id)
    assert unchanged.state == prefix_state and unchanged.attempt_count == 1
