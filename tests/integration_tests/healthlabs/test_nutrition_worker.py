import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import RecipeIngredientModel
from mealie.schema.recipe import Recipe, SaveIngredientFood, SaveIngredientUnit
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


def _run(session: Session, *, batch_size: int = 20):
    return asyncio.run(NutritionReconciliationWorker(session, batch_size=batch_size, now=NOW).run())


def test_worker_discovers_bounded_batches_and_structures_exact_ingredients(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"scoop-{token}"))
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"oats-{token}"))
    texts = [f"{amount} {unit.name} {food.name}" for amount in (1, 2, 3)]
    ingredients = _recipe_ingredients(user, texts)

    first = _run(user.repos.session, batch_size=2)
    first_rows = user.repos.session.scalars(
        sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.kind == "ingredient_structure",
            HealthLabsNutritionWork.target_id.in_([str(row.id) for row in ingredients]),
        )
    ).all()

    assert first.discovered == 2
    assert len(first_rows) == 2
    assert all(row.state == "completed" for row in first_rows)
    user.repos.session.expire_all()
    assert [user.repos.session.get(RecipeIngredientModel, row.id).original_text for row in ingredients[:2]] == texts[:2]
    assert [user.repos.session.get(RecipeIngredientModel, row.id).food_id for row in ingredients[:2]] == [
        food.id,
        food.id,
    ]

    _run(user.repos.session, batch_size=2)
    user.repos.session.expire_all()
    assert user.repos.session.get(RecipeIngredientModel, ingredients[2].id).food_id == food.id


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

    user.repos.session.delete(ingredient)
    user.repos.session.commit()
    _run(user.repos.session)
    assert user.repos.session.get(HealthLabsNutritionWork, work.id) is None
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
