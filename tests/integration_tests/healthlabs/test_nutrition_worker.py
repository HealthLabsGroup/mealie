import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderHeaders, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionProviderUsage,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import RecipeIngredientModel
from mealie.schema.recipe import (
    CreateIngredientFood,
    CreateIngredientUnit,
    IngredientConfidence,
    ParsedIngredient,
    Recipe,
    RecipeIngredient,
    SaveIngredientFood,
    SaveIngredientUnit,
)
from mealie.services.healthlabs.nutrition_service import NutritionService
from mealie.services.healthlabs.nutrition_worker import NutritionReconciliationWorker
from tests.fixtures.fixture_users import build_unique_user
from tests.utils import TestUser

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def _recipe_ingredients(
    user: TestUser,
    original_texts: list[str],
    *,
    food_id=None,
    unit_id=None,
    quantity: float | None = None,
) -> list[RecipeIngredientModel]:
    session = user.repos.session
    recipe = user.repos.recipes.create(
        Recipe(
            user_id=user.user_id,
            group_id=user.group_id,
            household_id=user.household_id,
            name=f"Worker recipe {uuid4()}",
        )
    )
    rows = [
        RecipeIngredientModel(
            session=session,
            group_id=user.group_id,
            recipe_id=recipe.id,
            position=index,
            original_text=text,
            quantity=quantity,
            food_id=food_id,
            unit_id=unit_id,
        )
        for index, text in enumerate(original_texts)
    ]
    session.add_all(rows)
    session.commit()
    return rows


def _source_food(user: TestUser, name: str) -> tuple[HealthLabsNutrientSourceRelease, HealthLabsNutrientFood]:
    session = user.repos.session
    release = HealthLabsNutrientSourceRelease(
        provider=f"worker-{uuid4()}",
        version="2026-10",
        country="GB",
        source_url="https://example.test/source",
        license_url="https://example.test/licence",
        attribution="Test source",
        sha256=uuid4().hex.ljust(64, "0"),
    )
    session.add(release)
    session.flush()
    source_food = HealthLabsNutrientFood(
        source_release_id=release.id,
        source_food_id=str(uuid4()),
        name=name,
        nutrient_values={"kcal": {"amount": 100, "unit": "kcal"}},
    )
    session.add(source_food)
    session.commit()
    return release, source_food


def _run(
    session: Session,
    *,
    batch_size: int = 20,
    provider_daily_request_limit: int = 0,
    now: datetime = NOW,
):
    return asyncio.run(
        NutritionReconciliationWorker(
            session,
            batch_size=batch_size,
            provider_daily_request_limit=provider_daily_request_limit,
            now=now,
        ).run()
    )


def _configure_provider(user: TestUser) -> AIProvider:
    session = user.repos.session
    settings = session.scalar(sa.select(AIProviderSettings).where(AIProviderSettings.group_id == user.group_id))
    assert settings is not None
    provider = AIProvider(
        session=session,
        settings_id=settings.id,
        name=f"worker-provider-{uuid4()}",
        base_url="https://openrouter.example/api/v1",
        api_key="test-runtime-key",
        model="qwen/test",
        timeout=30,
    )
    session.add(provider)
    session.flush()
    settings.default_provider_id = provider.id
    session.commit()
    return provider


def _parsed(source: str, *, quantity: float, unit: str, food: str) -> ParsedIngredient:
    return ParsedIngredient(
        input=source,
        confidence=IngredientConfidence(average=1, quantity=1, unit=1, food=1, comment=1),
        ingredient=RecipeIngredient(
            original_text=source,
            quantity=quantity,
            unit=CreateIngredientUnit(name=unit),
            food=CreateIngredientFood(name=food),
        ),
    )


def test_worker_discovers_bounded_batches_and_structures_exact_ingredients(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"scoop-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"oats-{token}"))
    texts = [f"{amount} {unit.name} {food.name}" for amount in (1, 2, 3)]
    ingredients = _recipe_ingredients(user, texts)

    reports = []
    for _ in range(100):
        reports.append(_run(user.repos.session, batch_size=2))
        user.repos.session.expire_all()
        if all(user.repos.session.get(RecipeIngredientModel, row.id).food_id == food.id for row in ingredients):
            break

    assert reports[0].discovered == 2
    assert all(report.discovered <= 2 for report in reports)
    assert [user.repos.session.get(RecipeIngredientModel, row.id).original_text for row in ingredients] == texts
    assert all(user.repos.session.get(RecipeIngredientModel, row.id).food_id == food.id for row in ingredients)


def test_discovery_scans_at_most_one_batch_of_unchanged_ingredients(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    _recipe_ingredients(user, [f"unchanged-{uuid4()}" for _ in range(5)])
    worker = NutritionReconciliationWorker(user.repos.session, batch_size=2, now=NOW)
    scanned: list[int] = []

    monkeypatch.setattr(worker, "ingredient_fingerprint", lambda ingredient, _group_id: str(ingredient.id))

    def unchanged(_kind, target_id, _group_id, _fingerprint, **_kwargs):
        scanned.append(int(target_id))
        return 0

    monkeypatch.setattr(worker, "_upsert", unchanged)

    assert worker._discover_ingredients(2) == 0
    assert len(scanned) == 2


def test_stale_cleanup_is_bounded_to_one_batch(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    stale = [
        HealthLabsNutritionWork(
            group_id=user.group_id,
            kind="ingredient_structure",
            target_id=str(9_000_000 + index),
            input_sha256=str(index).zfill(64),
            state="completed",
            next_attempt_at=NOW,
        )
        for index in range(4)
    ]
    user.repos.session.add_all(stale)
    user.repos.session.commit()
    stale_ids = [row.id for row in stale]

    NutritionReconciliationWorker(user.repos.session, batch_size=2, now=NOW)._delete_stale_work()
    user.repos.session.commit()

    remaining = user.repos.session.scalar(
        sa.select(sa.func.count()).select_from(HealthLabsNutritionWork).where(HealthLabsNutritionWork.id.in_(stale_ids))
    )
    assert remaining == 2


def test_worker_requeues_changed_input_and_removes_deleted_targets(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"cup-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"rice-{token}"))
    ingredient = _recipe_ingredients(user, [f"unresolved-{token}"])[0]

    first = _run(user.repos.session)
    work = user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "ingredient_structure",
            HealthLabsNutritionWork.target_id == str(ingredient.id),
        )
    )
    assert work is not None
    first_fingerprint = work.input_sha256
    assert work.state == "review_required"

    second = _run(user.repos.session)
    assert second.processed == 0

    ingredient.original_text = f"2 {unit.name} {food.name}"
    user.repos.session.commit()
    _run(user.repos.session)
    user.repos.session.refresh(work)
    assert work.input_sha256 != first_fingerprint
    assert work.state == "completed"

    work_id = work.id
    user.repos.session.delete(ingredient)
    user.repos.session.commit()
    _run(user.repos.session)
    assert user.repos.session.get(HealthLabsNutritionWork, work_id) is None
    assert first.processed >= 1


def test_worker_does_not_resolve_ingredient_from_another_group(
    unique_user_fn_scoped: TestUser,
    session: Session,
    api_client: TestClient,
) -> None:
    owner = unique_user_fn_scoped
    other = build_unique_user(session, "", api_client)
    token = uuid4().hex
    unit = other.repos.ingredient_units.create(SaveIngredientUnit(group_id=other.group_id, name=f"jar-{token}"))
    food = other.repos.ingredient_foods.create(SaveIngredientFood(group_id=other.group_id, name=f"beans-{token}"))
    ingredient = _recipe_ingredients(owner, [f"1 {unit.name} {food.name}"])[0]

    _run(session)
    session.expire_all()
    work = session.scalar(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "ingredient_structure",
            HealthLabsNutritionWork.target_id == str(ingredient.id),
        )
    )

    assert work is not None
    assert str(work.group_id) == owner.group_id
    assert work.state == "review_required"
    assert session.get(RecipeIngredientModel, ingredient.id).food_id is None


def test_worker_creates_only_unreviewed_unique_source_matches(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    _, exact_source = _source_food(user, f"apple-{token}")
    _, alias_source = _source_food(user, f"courgette-{token}")
    exact = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=exact_source.name))
    alias = user.repos.ingredient_foods.create(
        SaveIngredientFood(
            group_id=user.group_id,
            name=f"zucchini-{token}",
            aliases=[{"name": alias_source.name}],
        )
    )
    unknown = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"unknown-{token}"))
    user.repos.session.commit()

    _run(user.repos.session)
    matches = user.repos.session.scalars(
        sa.select(HealthLabsFoodMatch).where(
            HealthLabsFoodMatch.ingredient_food_id.in_([exact.id, alias.id, unknown.id])
        )
    ).all()
    works = user.repos.session.scalars(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "food_match",
            HealthLabsNutritionWork.target_id.in_([str(exact.id), str(alias.id), str(unknown.id)]),
        )
    ).all()

    assert {(row.ingredient_food_id, row.match_method) for row in matches} == {
        (exact.id, "exact"),
        (alias.id, "alias"),
    }
    assert all(row.reviewed_at is None and row.reviewed_by_user_id is None for row in matches)
    assert len(works) == 3
    assert all(row.state == "review_required" for row in works)
    assert _run(user.repos.session).processed == 0
    assert user.repos.session.scalar(sa.select(sa.func.count()).select_from(HealthLabsFoodMatch)) >= 2


def test_worker_tracks_only_unresolved_authoritative_portions(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"tofu-{token}"))
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"block-{token}"))
    _, source_food = _source_food(user, food.name)
    user.repos.session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=food.id,
            nutrient_food_id=source_food.id,
            country="GB",
            match_method="manual",
            confidence=1,
            reviewed_by_user_id=user.user_id,
            reviewed_at=get_utc_now(),
        )
    )
    ingredient = _recipe_ingredients(
        user,
        [f"2 {unit.name} {food.name}"],
        food_id=food.id,
        unit_id=unit.id,
        quantity=2,
    )[0]

    _run(user.repos.session)
    work = user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "portion_resolution",
            HealthLabsNutritionWork.target_id == str(ingredient.id),
        )
    )
    assert work is not None
    assert work.state == "review_required"

    user.repos.session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=source_food.id,
            description=unit.name,
            amount=1,
            unit=unit.name,
            grams=150,
            source_managed=True,
        )
    )
    user.repos.session.commit()
    _run(user.repos.session)
    user.repos.session.refresh(work)

    assert work.state == "completed"
    assert work.result == {"resolved_grams": 300.0}


def test_worker_commits_each_target_and_continues_after_one_failure(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    failing = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"failing-{uuid4()}"))
    succeeding = user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=f"succeeding-{uuid4()}")
    )
    user.repos.session.commit()
    original = NutritionService.match_food

    def fail_one(service, food_id, country):
        if food_id == failing.id:
            raise RuntimeError("controlled failure")
        return original(service, food_id, country)

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.NutritionService.match_food", fail_one)

    _run(user.repos.session)
    works = {
        row.target_id: row
        for row in user.repos.session.scalars(
            sa.select(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == "food_match",
                HealthLabsNutritionWork.target_id.in_([str(failing.id), str(succeeding.id)]),
            )
        )
    }

    assert works[str(failing.id)].state == "pending"
    assert works[str(failing.id)].attempt_count == 1
    assert works[str(failing.id)].next_attempt_at == NOW + timedelta(minutes=5)
    assert works[str(failing.id)].last_error == "RuntimeError"
    assert works[str(succeeding.id)].state == "review_required"


def test_worker_processes_due_rows_in_stable_order_and_reclaims_expired_leases(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    foods = [
        user.repos.ingredient_foods.create(
            SaveIngredientFood(group_id=user.group_id, name=f"ordered-{index}-{uuid4()}")
        )
        for index in range(2)
    ]
    user.repos.session.commit()
    _run(user.repos.session)
    works = list(
        user.repos.session.scalars(
            sa.select(HealthLabsNutritionWork)
            .where(
                HealthLabsNutritionWork.kind == "food_match",
                HealthLabsNutritionWork.target_id.in_([str(food.id) for food in foods]),
            )
            .order_by(HealthLabsNutritionWork.target_id)
        )
    )
    assert len(works) == 2
    for work in works:
        work.state = "running"
        work.next_attempt_at = NOW - timedelta(minutes=10)
        work.lease_expires_at = NOW + timedelta(minutes=1)
    user.repos.session.commit()

    assert _run(user.repos.session, batch_size=1).processed == 0

    for work in works:
        work.lease_expires_at = NOW - timedelta(seconds=1)
    user.repos.session.commit()
    _run(user.repos.session, batch_size=1)
    user.repos.session.refresh(works[0])
    user.repos.session.refresh(works[1])

    assert works[0].state == "review_required"
    assert works[1].state == "running"


def test_claim_uses_a_fresh_timestamp_for_its_lease(unique_user_fn_scoped: TestUser, monkeypatch) -> None:
    user = unique_user_fn_scoped
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id="9000001",
        input_sha256="a" * 64,
        state="pending",
        next_attempt_at=NOW,
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    claim_now = NOW + timedelta(minutes=5)
    worker = NutritionReconciliationWorker(user.repos.session, now=NOW)
    monkeypatch.setattr(worker, "_current_time", lambda: claim_now, raising=False)

    assert worker._claim(work.id)
    user.repos.session.refresh(work)

    assert work.lease_expires_at == claim_now + timedelta(minutes=15)


def test_provider_is_disabled_by_default_without_spending(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    ingredient = _recipe_ingredients(user, [f"provider-disabled-{uuid4()}"])[0]

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("provider must remain disabled")

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", unexpected_call)
    _run(user.repos.session)
    work = user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "ingredient_structure",
            HealthLabsNutritionWork.target_id == str(ingredient.id),
        )
    )

    assert work is not None
    assert work.state == "review_required"
    assert work.result == {"reason": "provider_disabled"}
    assert user.repos.session.scalar(sa.select(sa.func.count()).select_from(HealthLabsNutritionProviderUsage)) == 0


def test_provider_configuration_wakes_waiting_work_and_applies_only_exact_results(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"bowl-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"quinoa-{token}"))
    ingredient = _recipe_ingredients(user, [f"provider-needed-{token}"])[0]
    calls = 0
    timeouts: list[float] = []
    wake_now = datetime(2030, 10, 17, 12, tzinfo=UTC)

    class Timeout:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    def timeout(seconds: float):
        timeouts.append(seconds)
        return Timeout()

    async def parse_one(_parser, source: str):
        nonlocal calls
        calls += 1
        return _parsed(source, quantity=2, unit=unit.name, food=food.name)

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.asyncio.timeout", timeout)
    waiting = None
    for _ in range(100):
        _run(user.repos.session, provider_daily_request_limit=1, now=wake_now)
        waiting = user.repos.session.scalar(
            sa.select(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == "ingredient_structure",
                HealthLabsNutritionWork.target_id == str(ingredient.id),
            )
        )
        if waiting is not None:
            break
    assert waiting is not None
    assert waiting.result == {"reason": "provider_unconfigured"}
    assert calls == 0

    _configure_provider(user)
    for _ in range(100):
        _run(user.repos.session, provider_daily_request_limit=1, now=wake_now)
        user.repos.session.refresh(ingredient)
        if ingredient.food_id == food.id:
            break
    user.repos.session.refresh(ingredient)
    user.repos.session.refresh(waiting)

    assert calls == 1
    assert timeouts == [14 * 60]
    assert ingredient.quantity == 2
    assert ingredient.unit_id == unit.id
    assert ingredient.food_id == food.id
    assert waiting.state == "completed"
    usage = user.repos.session.scalar(
        sa.select(HealthLabsNutritionProviderUsage).where(
            HealthLabsNutritionProviderUsage.usage_date == wake_now.date()
        )
    )
    assert usage is not None
    assert usage.request_count == 1


def test_provider_headers_change_the_worker_fingerprint(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    provider = _configure_provider(user)
    before = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=1,
        now=NOW,
    )._provider_fingerprint(user.group_id)

    provider.request_headers.append(AIProviderHeaders("X-Provider-Mode", "structured"))
    user.repos.session.commit()

    after = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=1,
        now=NOW,
    )._provider_fingerprint(user.group_id)
    assert after != before


def test_ambiguous_provider_suggestion_is_sanitized_and_not_repeated(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    _configure_provider(user)
    source = f"3 mystery-unit-{uuid4()} mystery-food-{uuid4()}"
    ingredient = _recipe_ingredients(user, [source])[0]
    calls = 0

    async def parse_one(_parser, value: str):
        nonlocal calls
        calls += 1
        return _parsed(value, quantity=3, unit="mystery unit", food="mystery food")

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    _run(user.repos.session, provider_daily_request_limit=10)
    work = user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "ingredient_structure",
            HealthLabsNutritionWork.target_id == str(ingredient.id),
        )
    )
    assert work is not None
    assert work.state == "review_required"
    assert work.result == {
        "reason": "ingredient_suggestion",
        "suggestion": {"quantity": 3.0, "unit": "mystery unit", "food": "mystery food", "note": ""},
    }
    assert source not in json.dumps(work.result)

    assert _run(user.repos.session, provider_daily_request_limit=10).processed == 0
    assert calls == 1


def test_provider_allowance_defers_without_attempt_and_resets_next_utc_day(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    _configure_provider(user)
    ingredients = _recipe_ingredients(user, [f"budget-{uuid4()}", f"budget-{uuid4()}"])
    calls = 0
    budget_now = datetime(2026, 10, 20, tzinfo=UTC)

    async def parse_one(_parser, source: str):
        nonlocal calls
        calls += 1
        return _parsed(source, quantity=1, unit="unknown unit", food="unknown food")

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    _run(user.repos.session, provider_daily_request_limit=1, now=budget_now)
    works = list(
        user.repos.session.scalars(
            sa.select(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == "ingredient_structure",
                HealthLabsNutritionWork.target_id.in_([str(row.id) for row in ingredients]),
            )
        )
    )
    deferred = next(work for work in works if work.state == "pending")

    assert calls == 1
    assert deferred.attempt_count == 0
    assert deferred.next_attempt_at == datetime(2026, 10, 21, tzinfo=UTC)

    _run(
        user.repos.session,
        provider_daily_request_limit=1,
        now=datetime(2026, 10, 21, tzinfo=UTC),
    )
    assert calls == 2
    usage = user.repos.session.scalars(
        sa.select(HealthLabsNutritionProviderUsage)
        .where(
            HealthLabsNutritionProviderUsage.usage_date.in_((budget_now.date(), budget_now.date() + timedelta(days=1)))
        )
        .order_by(HealthLabsNutritionProviderUsage.usage_date)
    ).all()
    assert [(row.usage_date.isoformat(), row.request_count) for row in usage] == [
        ("2026-10-20", 1),
        ("2026-10-21", 1),
    ]


def test_provider_failures_retry_after_five_and_thirty_minutes_then_stop(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    _configure_provider(user)
    ingredient = _recipe_ingredients(user, [f"malformed-{uuid4()}"])[0]
    retry_now = datetime(2026, 10, 22, tzinfo=UTC)

    async def malformed(*_args, **_kwargs):
        raise ValueError("raw provider response must not be stored")

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", malformed)
    _run(user.repos.session, provider_daily_request_limit=3, now=retry_now)
    work = user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "ingredient_structure",
            HealthLabsNutritionWork.target_id == str(ingredient.id),
        )
    )
    assert work is not None
    assert (work.state, work.attempt_count, work.next_attempt_at, work.last_error) == (
        "pending",
        1,
        retry_now + timedelta(minutes=5),
        "ValueError",
    )

    _run(user.repos.session, provider_daily_request_limit=3, now=retry_now + timedelta(minutes=5))
    user.repos.session.refresh(work)
    assert (work.state, work.attempt_count, work.next_attempt_at) == (
        "pending",
        2,
        retry_now + timedelta(minutes=35),
    )

    _run(user.repos.session, provider_daily_request_limit=3, now=retry_now + timedelta(minutes=35))
    user.repos.session.refresh(work)
    assert (work.state, work.attempt_count, work.last_error) == ("failed", 3, "ValueError")
    assert "raw provider response" not in json.dumps(work.result)
