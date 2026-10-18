import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from mealie.db.models.recipe import IngredientFoodModel, RecipeIngredientModel
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
from mealie.services.openai.openai import OpenAIRequestRejected
from tests.fixtures.fixture_users import build_unique_user
from tests.utils import TestUser

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
PROVIDER_BUDGET = {
    "provider_daily_budget_usd": Decimal("1"),
    "provider_input_usd_per_million_tokens": Decimal("1"),
    "provider_output_usd_per_million_tokens": Decimal("1"),
}


def _complete_provider_request(parser) -> None:
    assert parser.request_guard is not None
    assert parser.max_completion_tokens is not None
    if not parser.request_guard(1, parser.max_completion_tokens):
        raise OpenAIRequestRejected
    parser.last_usage = {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}


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
    user: TestUser,
    *,
    batch_size: int = 20,
    provider_daily_request_limit: int = 0,
    provider_budget: dict | None = None,
    now: datetime = NOW,
):
    return asyncio.run(
        NutritionReconciliationWorker(
            user.repos.session,
            group_id=user.group_id,
            batch_size=batch_size,
            provider_daily_request_limit=provider_daily_request_limit,
            **(provider_budget if provider_budget is not None else PROVIDER_BUDGET),
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
        reports.append(_run(user, batch_size=2))
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
    worker = NutritionReconciliationWorker(user.repos.session, group_id=user.group_id, batch_size=2, now=NOW)
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
            update_at=datetime(2000, 1, 1, index, tzinfo=UTC),
        )
        for index in range(4)
    ]
    user.repos.session.add_all(stale)
    user.repos.session.commit()
    stale_ids = [row.id for row in stale]

    NutritionReconciliationWorker(
        user.repos.session, group_id=user.group_id, batch_size=2, now=NOW
    )._delete_stale_work()
    user.repos.session.commit()

    remaining = user.repos.session.scalar(
        sa.select(sa.func.count()).select_from(HealthLabsNutritionWork).where(HealthLabsNutritionWork.id.in_(stale_ids))
    )
    assert remaining == 2


def test_cleanup_removes_work_when_existing_targets_are_no_longer_eligible(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"unit-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"food-{token}"))
    ingredient = _recipe_ingredients(
        user,
        [f"1 {unit.name} {food.name}"],
        food_id=food.id,
        unit_id=unit.id,
        quantity=1,
    )[0]
    work = [
        HealthLabsNutritionWork(
            group_id=user.group_id,
            kind=kind,
            target_id=str(ingredient.id),
            input_sha256=kind.ljust(64, "0"),
            state="review_required",
            next_attempt_at=NOW,
        )
        for kind in ("ingredient_structure", "portion_resolution")
    ]
    user.repos.session.add_all(work)
    user.repos.session.commit()
    work_ids = [row.id for row in work]

    NutritionReconciliationWorker(
        user.repos.session, group_id=user.group_id, batch_size=20, now=NOW
    )._delete_stale_work()
    user.repos.session.commit()

    assert (
        user.repos.session.scalar(
            sa.select(sa.func.count())
            .select_from(HealthLabsNutritionWork)
            .where(HealthLabsNutritionWork.id.in_(work_ids))
        )
        == 0
    )


def test_existing_changed_ingredient_is_scanned_before_new_backlog(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    existing = _recipe_ingredients(user, [f"changed-{uuid4()}"])[0]
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(existing.id),
        input_sha256="0" * 64,
        state="review_required",
        next_attempt_at=NOW,
        update_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    _recipe_ingredients(user, [f"new-{uuid4()}" for _ in range(5)])

    NutritionReconciliationWorker(
        user.repos.session, group_id=user.group_id, batch_size=3, now=NOW
    )._discover_ingredients(1)
    user.repos.session.commit()

    assert work.input_sha256 != "0" * 64
    assert work.state == "pending"


def test_quantity_only_ingredient_work_is_ineligible_and_removed(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    ingredient = _recipe_ingredients(user, [f"quantity-only-{uuid4()}"], quantity=2)[0]
    ingredient.update_at = datetime(2000, 1, 1, tzinfo=UTC)
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256="0" * 64,
        state="pending",
        next_attempt_at=NOW,
        update_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    work_id = work.id

    worker = NutritionReconciliationWorker(user.repos.session, group_id=user.group_id, batch_size=20, now=NOW)
    assert not worker._ingredient_needs_structure(ingredient)
    worker._discover_ingredients(20)
    worker._delete_stale_work()
    user.repos.session.commit()

    assert user.repos.session.get(HealthLabsNutritionWork, work_id) is None


def test_discovery_preserves_an_active_changed_claim(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    ingredient = _recipe_ingredients(user, [f"active-claim-{uuid4()}"])[0]
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256="0" * 64,
        state="running",
        next_attempt_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
        update_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    user.repos.session.add(work)
    user.repos.session.commit()

    NutritionReconciliationWorker(
        user.repos.session, group_id=user.group_id, batch_size=20, now=NOW
    )._discover_ingredients(20)
    user.repos.session.commit()
    user.repos.session.refresh(work)

    assert work.state == "running"
    assert work.input_sha256 == "0" * 64
    assert work.lease_expires_at == NOW + timedelta(minutes=1)


def test_stale_cleanup_preserves_an_active_claim(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    ingredient = _recipe_ingredients(user, [f"active-cleanup-{uuid4()}"])[0]
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256="0" * 64,
        state="running",
        next_attempt_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    ingredient.quantity = 2
    user.repos.session.commit()

    NutritionReconciliationWorker(
        user.repos.session, group_id=user.group_id, batch_size=20, now=NOW
    )._delete_stale_work()
    user.repos.session.commit()

    assert user.repos.session.get(HealthLabsNutritionWork, work.id) is not None


def test_small_discovery_batches_rotate_between_work_kinds(session: Session, monkeypatch) -> None:
    called: list[str] = []
    for tick in range(3):
        worker = NutritionReconciliationWorker(
            session,
            batch_size=1,
            now=NOW + timedelta(minutes=5 * tick),
        )
        monkeypatch.setattr(worker, "_delete_stale_work", lambda: None)
        monkeypatch.setattr(worker, "_discover_ingredients", lambda _limit: called.append("ingredient") or 0)
        monkeypatch.setattr(worker, "_discover_foods", lambda _limit: called.append("food") or 0)
        monkeypatch.setattr(worker, "_discover_portions", lambda _limit: called.append("portion") or 0)
        worker._discover()

    assert set(called) == {"ingredient", "food", "portion"}


def test_worker_requeues_changed_input_and_removes_deleted_targets(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"cup-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"rice-{token}"))
    ingredient = _recipe_ingredients(user, [f"unresolved-{token}"])[0]

    reports = []
    work = None
    for _ in range(100):
        reports.append(_run(user))
        work = user.repos.session.scalar(
            sa.select(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == "ingredient_structure",
                HealthLabsNutritionWork.target_id == str(ingredient.id),
            )
        )
        if work is not None:
            break
    assert work is not None
    first_fingerprint = work.input_sha256
    assert work.state == "review_required"

    second = _run(user)
    assert second.processed == 0

    ingredient.original_text = f"2 {unit.name} {food.name}"
    user.repos.session.commit()
    for _ in range(100):
        _run(user)
        user.repos.session.refresh(work)
        if work.state == "completed":
            break
    assert work.input_sha256 != first_fingerprint
    assert work.state == "completed"

    work_id = work.id
    user.repos.session.delete(ingredient)
    user.repos.session.commit()
    _run(user)
    assert user.repos.session.get(HealthLabsNutritionWork, work_id) is None
    assert any(report.processed >= 1 for report in reports)


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
    other_ingredient = _recipe_ingredients(other, [f"1 {unit.name} {food.name}"])[0]

    _run(owner)
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
    assert (
        session.scalar(
            sa.select(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == "ingredient_structure",
                HealthLabsNutritionWork.target_id == str(other_ingredient.id),
            )
        )
        is None
    )


def test_worker_auto_applies_unique_deterministic_source_matches(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    _, exact_source = _source_food(user, f"apple-{token}")
    _, alias_source = _source_food(user, f"courgette-{token}")
    _, multilingual_source = _source_food(user, f"crème brûlée-{token}")
    exact = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=exact_source.name))
    alias = user.repos.ingredient_foods.create(
        SaveIngredientFood(
            group_id=user.group_id,
            name=f"zucchini-{token}",
            aliases=[{"name": alias_source.name}],
        )
    )
    multilingual = user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=multilingual_source.name)
    )
    unknown = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"unknown-{token}"))
    user.repos.session.commit()

    _run(user)
    matches = user.repos.session.scalars(
        sa.select(HealthLabsFoodMatch).where(
            HealthLabsFoodMatch.ingredient_food_id.in_([exact.id, alias.id, multilingual.id, unknown.id])
        )
    ).all()
    works = user.repos.session.scalars(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "food_match",
            HealthLabsNutritionWork.target_id.in_(
                [str(exact.id), str(alias.id), str(multilingual.id), str(unknown.id)]
            ),
        )
    ).all()

    assert {(row.ingredient_food_id, row.match_method) for row in matches} == {
        (exact.id, "exact"),
        (alias.id, "alias"),
        (multilingual.id, "exact"),
    }
    assert all(row.reviewed_at is None and row.reviewed_by_user_id is None for row in matches)
    assert len(works) == 4
    assert {row.target_id: row.state for row in works} == {
        str(exact.id): "completed",
        str(alias.id): "completed",
        str(multilingual.id): "completed",
        str(unknown.id): "review_required",
    }
    assert NutritionService(user.repos.session).match_food(exact.id, "GB").status == "matched"
    assert NutritionService(user.repos.session).match_food(alias.id, "GB").status == "matched"
    assert _run(user).processed == 0
    assert user.repos.session.scalar(sa.select(sa.func.count()).select_from(HealthLabsFoodMatch)) >= 2


def test_worker_rechecks_historical_unreviewed_match_candidates(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    _, source = _source_food(user, f"historical-{uuid4()}")
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=source.name))
    match = HealthLabsFoodMatch(
        ingredient_food_id=food.id,
        nutrient_food_id=source.id,
        country="GB",
        match_method="exact",
        confidence=1,
    )
    user.repos.session.add(match)
    user.repos.session.commit()

    _run(user)
    user.repos.session.refresh(match)
    work = user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "food_match",
            HealthLabsNutritionWork.target_id == str(food.id),
        )
    )

    assert match.auto_applied is True
    assert work is not None
    assert work.state == "completed"


def test_worker_preserves_review_completed_while_candidate_was_computed(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    _, automatic_source = _source_food(user, f"concurrent-{uuid4()}")
    _, reviewed_source = _source_food(user, f"reviewed-{uuid4()}")
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=automatic_source.name))
    match = HealthLabsFoodMatch(
        ingredient_food_id=food.id,
        nutrient_food_id=automatic_source.id,
        country="GB",
        match_method="exact",
        confidence=1,
    )
    user.repos.session.add(match)
    user.repos.session.commit()
    original = NutritionService.suggest_food_match

    def review_during_match(service, food_id, country):
        candidate = original(service, food_id, country)
        match.nutrient_food_id = reviewed_source.id
        match.match_method = "manual"
        match.auto_applied = False
        match.reviewed_by_user_id = user.user_id
        match.reviewed_at = get_utc_now()
        service.session.flush()
        return candidate

    monkeypatch.setattr(
        "mealie.services.healthlabs.nutrition_worker.NutritionService.suggest_food_match",
        review_during_match,
    )

    _run(user)
    user.repos.session.refresh(match)

    assert match.nutrient_food_id == reviewed_source.id
    assert match.match_method == "manual"
    assert match.auto_applied is False
    assert match.reviewed_by_user_id == user.user_id


def test_worker_leaves_ambiguous_and_missing_source_matches_for_review(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    ambiguous_name = f"ambiguous-{uuid4()}"
    _source_food(user, ambiguous_name)
    _source_food(user, ambiguous_name)
    ambiguous = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=ambiguous_name))
    missing = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"missing-{uuid4()}"))
    user.repos.session.commit()

    _run(user)
    works = user.repos.session.scalars(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "food_match",
            HealthLabsNutritionWork.target_id.in_([str(ambiguous.id), str(missing.id)]),
        )
    ).all()

    assert {work.target_id: work.result for work in works} == {
        str(ambiguous.id): {"reason": "no_unique_source_match", "food_id": str(ambiguous.id)},
        str(missing.id): {"reason": "no_unique_source_match", "food_id": str(missing.id)},
    }
    assert not user.repos.session.scalars(
        sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id.in_([ambiguous.id, missing.id]))
    ).all()


def test_worker_stops_before_claiming_the_next_target(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    foods = [
        user.repos.ingredient_foods.create(
            SaveIngredientFood(group_id=user.group_id, name=f"shutdown-{index}-{uuid4()}")
        )
        for index in range(2)
    ]
    user.repos.session.commit()
    checks = 0

    def should_stop() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    report = asyncio.run(
        NutritionReconciliationWorker(
            user.repos.session,
            group_id=user.group_id,
            batch_size=20,
            should_stop=should_stop,
            now=NOW,
        ).run()
    )
    works = user.repos.session.scalars(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "food_match",
            HealthLabsNutritionWork.target_id.in_([str(food.id) for food in foods]),
        )
    ).all()

    assert report.processed == 1
    assert sorted(work.state for work in works) == ["pending", "review_required"]


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

    _run(user)
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
    _run(user)
    user.repos.session.refresh(work)

    assert work.state == "completed"
    assert work.result == {"resolved_grams": 300.0}


def test_source_managed_portion_wins_over_group_override(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"tofu-{token}"))
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"block-{token}"))
    _, source_food = _source_food(user, food.name)
    user.repos.session.add_all(
        [
            HealthLabsFoodMatch(
                ingredient_food_id=food.id,
                nutrient_food_id=source_food.id,
                country="GB",
                match_method="manual",
                confidence=1,
                reviewed_by_user_id=user.user_id,
                reviewed_at=get_utc_now(),
            ),
            HealthLabsFoodPortion(
                nutrient_food_id=source_food.id,
                description=unit.name,
                amount=1,
                unit=unit.name,
                grams=150,
                source_managed=True,
            ),
            HealthLabsFoodPortion(
                nutrient_food_id=source_food.id,
                group_id=user.group_id,
                description=unit.name,
                amount=1,
                unit=unit.name,
                grams=120,
                source_managed=False,
            ),
        ]
    )
    user.repos.session.commit()

    facts = NutritionService(user.repos.session).food_facts(food.id, unit.id, 2)

    assert facts.resolved_grams == 300
    assert facts.food_match is not None
    assert [(portion.unit, portion.grams) for portion in facts.food_match.portions] == [(unit.name, 150)]


def test_discovery_reserves_capacity_for_each_work_kind(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    _recipe_ingredients(user, [f"ingredient-backlog-{uuid4()}" for _ in range(3)])
    user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"unmatched-{uuid4()}"))
    portion_food = user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=f"portion-{uuid4()}")
    )
    portion_unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=user.group_id, name=f"portion-unit-{uuid4()}")
    )
    _, source_food = _source_food(user, portion_food.name)
    user.repos.session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=portion_food.id,
            nutrient_food_id=source_food.id,
            country="GB",
            match_method="manual",
            confidence=1,
            reviewed_by_user_id=user.user_id,
            reviewed_at=get_utc_now(),
        )
    )
    _recipe_ingredients(
        user,
        [f"1 {portion_unit.name} {portion_food.name}"],
        food_id=portion_food.id,
        unit_id=portion_unit.id,
        quantity=1,
    )

    kinds: set[str] = set()
    for _ in range(100):
        NutritionReconciliationWorker(user.repos.session, group_id=user.group_id, batch_size=3, now=NOW)._discover()
        user.repos.session.commit()
        kinds = set(
            user.repos.session.scalars(
                sa.select(HealthLabsNutritionWork.kind).where(HealthLabsNutritionWork.group_id == user.group_id)
            )
        )
        if kinds == {"ingredient_structure", "food_match", "portion_resolution"}:
            break

    assert kinds == {"ingredient_structure", "food_match", "portion_resolution"}


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
    original = NutritionService.suggest_food_match

    def fail_one(service, food_id, country):
        if food_id == failing.id:
            raise RuntimeError("controlled failure")
        return original(service, food_id, country)

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.NutritionService.suggest_food_match", fail_one)

    works = {}
    for _ in range(100):
        _run(user)
        works = {
            row.target_id: row
            for row in user.repos.session.scalars(
                sa.select(HealthLabsNutritionWork).where(
                    HealthLabsNutritionWork.kind == "food_match",
                    HealthLabsNutritionWork.target_id.in_([str(failing.id), str(succeeding.id)]),
                )
            )
        }
        if len(works) == 2:
            break

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
    _run(user)
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

    assert _run(user, batch_size=1).processed == 0

    for work in works:
        work.lease_expires_at = NOW - timedelta(seconds=1)
    user.repos.session.commit()
    _run(user, batch_size=1)
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
    _run(user)
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


def test_zero_dollar_budget_disables_provider_even_with_request_allowance(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    ingredient = _recipe_ingredients(user, [f"zero-budget-{uuid4()}"])[0]
    _configure_provider(user)

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("zero-dollar budget must not call provider")

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", unexpected_call)
    for _ in range(100):
        _run(
            user,
            provider_daily_request_limit=5,
            provider_budget={**PROVIDER_BUDGET, "provider_daily_budget_usd": Decimal("0")},
        )
        work = user.repos.session.scalar(
            sa.select(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == "ingredient_structure",
                HealthLabsNutritionWork.target_id == str(ingredient.id),
            )
        )
        if work is not None:
            break

    assert work is not None
    assert work.state == "review_required"
    assert work.result == {"reason": "provider_disabled"}
    assert work.provider_requested_at is None


def test_provider_budget_reservation_caps_spend_and_records_sanitized_usage(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    works = [
        HealthLabsNutritionWork(
            group_id=user.group_id,
            kind="ingredient_structure",
            target_id=str(8_100_000 + index),
            input_sha256=str(index).zfill(64),
            state="running",
            next_attempt_at=NOW,
        )
        for index in range(2)
    ]
    user.repos.session.add_all(works)
    user.repos.session.commit()
    worker = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=2,
        provider_daily_budget_usd=Decimal("0.000003"),
        provider_input_usd_per_million_tokens=Decimal("1"),
        provider_output_usd_per_million_tokens=Decimal("1"),
        now=NOW,
    )

    reservation = worker._reserve_provider_request(works[0], 1, 1)
    assert reservation == 2
    assert worker._reserve_provider_request(works[1], 1, 1) is None

    worker._record_provider_usage(
        works[0],
        reservation,
        {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
    )
    usage = user.repos.session.scalar(
        sa.select(HealthLabsNutritionProviderUsage).where(HealthLabsNutritionProviderUsage.usage_date == NOW.date())
    )
    assert usage is not None
    assert (
        usage.request_count,
        usage.reserved_microusd,
        usage.spent_microusd,
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
    ) == (1, 0, 2, 1, 0, 1)


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

    async def parse_one(parser, source: str):
        nonlocal calls
        _complete_provider_request(parser)
        calls += 1
        return _parsed(source, quantity=2, unit=unit.name, food=food.name)

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.asyncio.timeout", timeout)
    waiting = None
    for _ in range(100):
        _run(user, provider_daily_request_limit=1, now=wake_now)
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
        _run(user, provider_daily_request_limit=1, now=wake_now)
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
        **PROVIDER_BUDGET,
        now=NOW,
    )._provider_fingerprint(user.group_id)

    provider.request_headers.append(AIProviderHeaders("X-Provider-Mode", "structured"))
    user.repos.session.commit()

    after = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=1,
        **PROVIDER_BUDGET,
        now=NOW,
    )._provider_fingerprint(user.group_id)
    assert after != before


def test_provider_result_does_not_overwrite_a_concurrent_ingredient_edit(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"cup-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"rice-{token}"))
    ingredient = _recipe_ingredients(user, [f"provider-{token}"])[0]
    _configure_provider(user)
    edited_note = f"user-edited-{token}"
    calls = 0

    async def parse_one(parser, source: str):
        nonlocal calls
        _complete_provider_request(parser)
        calls += 1
        with Session(user.repos.session.get_bind()) as concurrent:
            edited = concurrent.get(RecipeIngredientModel, ingredient.id)
            assert edited is not None
            edited.quantity = 7
            edited.note = edited_note
            concurrent.commit()
        return _parsed(source, quantity=2, unit=unit.name, food=food.name)

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    for _ in range(100):
        _run(
            user,
            provider_daily_request_limit=1,
            now=datetime(2031, 10, 17, 12, tzinfo=UTC),
        )
        if calls:
            break
    user.repos.session.refresh(ingredient)
    work = user.repos.session.scalar(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "ingredient_structure",
            HealthLabsNutritionWork.target_id == str(ingredient.id),
        )
    )

    assert calls == 1
    assert ingredient.quantity == 7
    assert ingredient.note == edited_note
    assert ingredient.food_id is None
    assert work is None


def test_provider_result_is_requeued_after_a_concurrent_catalogue_change(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"cup-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"rice-{token}"))
    ingredient = _recipe_ingredients(user, [f"provider-catalogue-{token}"])[0]
    _configure_provider(user)
    original_food_name = food.name
    assert original_food_name is not None
    calls = 0

    async def parse_one(parser, source: str):
        nonlocal calls
        _complete_provider_request(parser)
        calls += 1
        with Session(user.repos.session.get_bind()) as concurrent:
            edited = concurrent.get(IngredientFoodModel, food.id)
            assert edited is not None
            edited.name = f"renamed-{token}"
            concurrent.commit()
        return _parsed(source, quantity=2, unit=unit.name, food=original_food_name)

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    worker = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=1,
        **PROVIDER_BUDGET,
        now=datetime(2032, 10, 17, 12, tzinfo=UTC),
    )
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256=worker.ingredient_fingerprint(ingredient, user.group_id),
        state="running",
        next_attempt_at=worker.now,
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    asyncio.run(worker._process_ingredient(work))
    user.repos.session.commit()
    user.repos.session.refresh(ingredient)
    user.repos.session.refresh(work)

    assert calls == 1
    assert ingredient.food_id is None
    assert work is not None
    assert work.state == "pending"


def test_provider_apply_requeues_when_a_selected_catalogue_row_changes(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"cup-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"rice-{token}"))
    ingredient = _recipe_ingredients(user, [f"provider-catalogue-guard-{token}"])[0]
    _configure_provider(user)

    async def parse_one(parser, source: str):
        _complete_provider_request(parser)
        return _parsed(source, quantity=2, unit=unit.name, food=food.name)

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    original_apply = NutritionReconciliationWorker._apply_provider_ingredient

    def rename_then_apply(worker, target, value, expected):
        with Session(user.repos.session.get_bind()) as concurrent:
            edited = concurrent.get(IngredientFoodModel, food.id)
            assert edited is not None
            edited.name = f"renamed-at-apply-{token}"
            concurrent.commit()
        return original_apply(worker, target, value, expected)

    monkeypatch.setattr(NutritionReconciliationWorker, "_apply_provider_ingredient", rename_then_apply)
    worker = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=1,
        **PROVIDER_BUDGET,
        now=datetime(2033, 10, 17, 12, tzinfo=UTC),
    )
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256=worker.ingredient_fingerprint(ingredient, user.group_id),
        state="running",
        next_attempt_at=worker.now,
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    asyncio.run(worker._process_ingredient(work))
    user.repos.session.commit()
    user.repos.session.refresh(ingredient)
    user.repos.session.refresh(work)

    assert ingredient.food_id is None
    assert work.state == "pending"


def test_provider_requests_crossing_midnight_use_each_utc_day(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    first_now = datetime(2040, 1, 1, 23, 59, tzinfo=UTC)
    second_now = datetime(2040, 1, 2, 0, 1, tzinfo=UTC)
    works = [
        HealthLabsNutritionWork(
            group_id=user.group_id,
            kind="ingredient_structure",
            target_id=str(8_000_000 + index),
            input_sha256=str(index).zfill(64),
            state="running",
            next_attempt_at=first_now,
        )
        for index in range(2)
    ]
    user.repos.session.add_all(works)
    user.repos.session.commit()
    worker = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=1,
        **PROVIDER_BUDGET,
        now=first_now,
    )
    times = iter((first_now, second_now))
    monkeypatch.setattr(worker, "_current_time", lambda: next(times))

    assert worker._reserve_provider_request(works[0], 1, 1)
    assert worker._reserve_provider_request(works[1], 1, 1)

    usage = user.repos.session.scalars(
        sa.select(HealthLabsNutritionProviderUsage)
        .where(HealthLabsNutritionProviderUsage.usage_date.in_((first_now.date(), second_now.date())))
        .order_by(HealthLabsNutritionProviderUsage.usage_date)
    ).all()
    assert [(row.usage_date, row.request_count) for row in usage] == [
        (first_now.date(), 1),
        (second_now.date(), 1),
    ]
    assert works[0].provider_requested_at == first_now
    assert works[1].provider_requested_at == second_now


def test_provider_apply_requeues_when_target_changes_after_the_final_check(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"cup-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"rice-{token}"))
    ingredient = _recipe_ingredients(user, [f"provider-final-check-{token}"])[0]
    _configure_provider(user)

    async def parse_one(parser, source: str):
        _complete_provider_request(parser)
        return _parsed(source, quantity=2, unit=unit.name, food=food.name)

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    original_apply = getattr(NutritionReconciliationWorker, "_apply_provider_ingredient", None)
    assert original_apply is not None

    def edit_then_apply(worker, target, value, expected):
        worker.session.execute(
            sa.update(RecipeIngredientModel)
            .where(RecipeIngredientModel.id == target.id)
            .values(note="last-moment edit")
        )
        worker.session.commit()
        return original_apply(worker, target, value, expected)

    monkeypatch.setattr(NutritionReconciliationWorker, "_apply_provider_ingredient", edit_then_apply)
    worker = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=1,
        **PROVIDER_BUDGET,
        now=datetime(2041, 10, 17, 12, tzinfo=UTC),
    )
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256=worker.ingredient_fingerprint(ingredient, user.group_id),
        state="running",
        next_attempt_at=worker.now,
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    asyncio.run(worker._process_ingredient(work))
    user.repos.session.commit()
    user.repos.session.refresh(ingredient)
    user.repos.session.refresh(work)

    assert ingredient.quantity is None
    assert ingredient.note == "last-moment edit"
    assert ingredient.food_id is None
    assert work is not None
    assert work.state == "pending"


def test_ambiguous_provider_suggestion_is_sanitized_and_not_repeated(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    _configure_provider(user)
    source = f"3 mystery-unit-{uuid4()} mystery-food-{uuid4()}"
    ingredient = _recipe_ingredients(user, [source])[0]
    calls = 0

    async def parse_one(parser, value: str):
        nonlocal calls
        _complete_provider_request(parser)
        calls += 1
        return _parsed(value, quantity=3, unit="mystery unit", food="mystery food")

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    worker = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=10,
        **PROVIDER_BUDGET,
        now=NOW,
    )
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256=worker.ingredient_fingerprint(ingredient, user.group_id),
        state="running",
        next_attempt_at=worker.now,
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    asyncio.run(worker._process_ingredient(work))
    user.repos.session.commit()
    assert work.state == "review_required"
    assert work.result == {
        "reason": "ingredient_suggestion",
        "suggestion": {"quantity": 3.0, "unit": "mystery unit", "food": "mystery food", "note": ""},
    }
    assert source not in json.dumps(work.result)

    assert work.id not in worker._due_work_ids()
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

    async def parse_one(parser, source: str):
        nonlocal calls
        _complete_provider_request(parser)
        calls += 1
        return _parsed(source, quantity=1, unit="unknown unit", food="unknown food")

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", parse_one)
    works = []
    for _ in range(100):
        _run(user, provider_daily_request_limit=1, now=budget_now)
        works = list(
            user.repos.session.scalars(
                sa.select(HealthLabsNutritionWork).where(
                    HealthLabsNutritionWork.kind == "ingredient_structure",
                    HealthLabsNutritionWork.target_id.in_([str(row.id) for row in ingredients]),
                )
            )
        )
        if len(works) == 2:
            break
    deferred = next(work for work in works if work.state == "pending")

    assert calls == 1
    assert deferred.attempt_count == 0
    assert deferred.next_attempt_at == datetime(2026, 10, 21, tzinfo=UTC)

    _run(
        user,
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

    async def malformed(parser, *_args, **_kwargs):
        _complete_provider_request(parser)
        raise ValueError("raw provider response must not be stored")

    monkeypatch.setattr("mealie.services.healthlabs.nutrition_worker.OpenAIParser.parse_one", malformed)
    worker = NutritionReconciliationWorker(
        user.repos.session,
        provider_daily_request_limit=3,
        **PROVIDER_BUDGET,
        now=retry_now,
    )
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="ingredient_structure",
        target_id=str(ingredient.id),
        input_sha256=worker.ingredient_fingerprint(ingredient, user.group_id),
        state="running",
        next_attempt_at=retry_now,
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    work_id = work.id

    def fail_at(now: datetime) -> HealthLabsNutritionWork:
        current_worker = NutritionReconciliationWorker(
            user.repos.session,
            provider_daily_request_limit=3,
            **PROVIDER_BUDGET,
            now=now,
        )
        current_work = user.repos.session.get(HealthLabsNutritionWork, work_id)
        assert current_work is not None
        current_work.state = "running"
        user.repos.session.commit()
        try:
            asyncio.run(current_worker._process_ingredient(current_work))
        except ValueError as exc:
            user.repos.session.rollback()
            current_worker._record_failure(work_id, exc)
        current_work = user.repos.session.get(HealthLabsNutritionWork, work_id)
        assert current_work is not None
        return current_work

    work = fail_at(retry_now)
    assert (work.state, work.attempt_count, work.next_attempt_at, work.last_error) == (
        "pending",
        1,
        retry_now + timedelta(minutes=5),
        "ValueError",
    )

    work = fail_at(retry_now + timedelta(minutes=5))
    assert (work.state, work.attempt_count, work.next_attempt_at) == (
        "pending",
        2,
        retry_now + timedelta(minutes=35),
    )

    work = fail_at(retry_now + timedelta(minutes=35))
    assert (work.state, work.attempt_count, work.last_error) == ("failed", 3, "ValueError")
    assert "raw provider response" not in json.dumps(work.result)
