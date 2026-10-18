from datetime import UTC, date, datetime
from math import inf, nan
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

import mealie.db.models.healthlabs as healthlabs_models
import mealie.db.models.users.users as user_models
from mealie.db.models.healthlabs import (
    HealthLabsBrandedProduct,
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionProviderUsage,
    HealthLabsNutritionWork,
)
from mealie.db.models.users import User
from mealie.schema.recipe import SaveIngredientFood, SaveIngredientUnit
from mealie.schema.user import PrivateUser, UserIn
from tests.utils import TestUser


def _source(user: TestUser, *, provider: str | None = None, version: str | None = None):
    provider = provider or f"provider-{uuid4()}"
    version = version or str(uuid4())
    source = HealthLabsNutrientSourceRelease(
        provider=provider,
        version=version,
        country="OTHER",
        source_url=f"https://example.test/{provider}/{version}",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        attribution="Test source",
        sha256=uuid4().hex.ljust(64, "0"),
    )
    user.repos.session.add(source)
    user.repos.session.flush()
    return source


def _nutrient_food(user: TestUser, source: HealthLabsNutrientSourceRelease):
    food = HealthLabsNutrientFood(
        source_release_id=source.id,
        source_food_id=str(uuid4()),
        name="Cooked red lentils",
        preparation_state="cooked",
        nutrient_values={
            "protein_g": {"amount": 9.02, "unit": "g"},
            "kcal": {"amount": 116, "unit": "kcal"},
        },
    )
    user.repos.session.add(food)
    user.repos.session.flush()
    return food


def _nutrition_work(
    user: TestUser,
    *,
    kind: str = "ingredient_structure",
    target_id: str | None = None,
    state: str = "pending",
) -> HealthLabsNutritionWork:
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind=kind,
        target_id=target_id or str(uuid4()),
        input_sha256="a" * 64,
        state=state,
        next_attempt_at=datetime(2026, 10, 17, 9, 30, tzinfo=UTC),
    )
    user.repos.session.add(work)
    return work


def _curator_decision(
    user: TestUser,
    *,
    state: str = "pending",
    target_id: str | None = None,
    curator_sha256: str | None = None,
    work: HealthLabsNutritionWork | None = None,
):
    decision_type = getattr(healthlabs_models, "HealthLabsNutritionCuratorDecision", None)
    assert decision_type is not None, "HealthLabsNutritionCuratorDecision is not implemented"
    if work is None:
        work = _nutrition_work(user, target_id=target_id)
        user.repos.session.flush()
    return decision_type(
        work_id=work.id,
        group_id=user.group_id,
        kind=work.kind,
        target_id=work.target_id,
        work_input_sha256=work.input_sha256,
        curator_sha256=curator_sha256 or uuid4().hex.ljust(64, "0"),
        agent_user_id=user.user_id,
        state=state,
        next_attempt_at=datetime(2026, 10, 18, 9, 30, tzinfo=UTC),
        candidate_snapshot={"foods": []},
        provider_name="test-provider",
        provider_model="test-model",
        prompt_version="healthlabs-nutrition-curator-v1",
    )


def test_user_type_defaults_to_human(unique_user_fn_scoped: TestUser) -> None:
    user_type = getattr(user_models, "UserType", None)
    assert user_type is not None, "UserType is not implemented"
    user = unique_user_fn_scoped.repos.session.get(User, unique_user_fn_scoped.user_id)
    assert user is not None

    assert user.user_type is user_type.HUMAN
    assert PrivateUser.model_validate(user).user_type == "human"


def test_user_type_is_read_only() -> None:
    assert "user_type" not in UserIn.model_fields
    assert "user_type" in PrivateUser.model_fields
    assert PrivateUser.model_fields["user_type"].is_required()


@pytest.mark.parametrize("state", ["approved", "insufficient_evidence", "failed"])
def test_curator_decision_enforces_identity_state_and_scores(
    unique_user_fn_scoped: TestUser,
    state: str,
) -> None:
    decision = _curator_decision(unique_user_fn_scoped, state=state)
    unique_user_fn_scoped.repos.session.add(decision)

    unique_user_fn_scoped.repos.session.flush()

    assert decision.agent_user_id == unique_user_fn_scoped.user_id
    assert decision.state == state
    unique_user_fn_scoped.repos.session.rollback()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "completed"),
        ("attempt_count", -1),
        ("confidence", -0.01),
        ("confidence", 1.01),
        ("confidence", inf),
        ("confidence", nan),
        ("runner_up_confidence", -0.01),
        ("runner_up_confidence", 1.01),
        ("runner_up_confidence", inf),
        ("runner_up_confidence", nan),
    ],
)
def test_curator_decision_rejects_invalid_control_values(
    unique_user_fn_scoped: TestUser,
    field: str,
    value: str | int | float,
) -> None:
    decision = _curator_decision(unique_user_fn_scoped)

    with pytest.raises((IntegrityError, ValueError)):
        setattr(decision, field, value)
        unique_user_fn_scoped.repos.session.add(decision)
        unique_user_fn_scoped.repos.session.flush()
    unique_user_fn_scoped.repos.session.rollback()


def test_curator_decision_is_unique_per_complete_fingerprint(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    target_id = str(uuid4())
    fingerprint = "f" * 64
    first = _curator_decision(unique_user_fn_scoped, target_id=target_id, curator_sha256=fingerprint)
    session.add(first)
    session.flush()

    work = session.get(HealthLabsNutritionWork, first.work_id)
    assert work is not None
    duplicate = _curator_decision(unique_user_fn_scoped, curator_sha256=fingerprint, work=work)
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_terminal_curator_evidence_is_immutable(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    decision = _curator_decision(unique_user_fn_scoped, state="insufficient_evidence")
    decision.rationale = "I am not confident enough."
    session.add(decision)
    session.commit()

    decision.rationale = "Changed"
    with pytest.raises(ValueError, match="terminal Curator decisions are immutable"):
        session.flush()
    session.rollback()

    decision = session.get(type(decision), decision.id)
    assert decision is not None
    session.delete(decision)
    with pytest.raises(ValueError, match="terminal Curator decisions are immutable"):
        session.flush()
    session.rollback()


def test_portion_mapping_is_group_scoped_and_unique(unique_user_fn_scoped: TestUser) -> None:
    mapping_type = getattr(healthlabs_models, "HealthLabsFoodPortionMapping", None)
    assert mapping_type is not None, "HealthLabsFoodPortionMapping is not implemented"
    session = unique_user_fn_scoped.repos.session
    ingredient_food = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=f"food-{uuid4()}")
    )
    ingredient_unit = unique_user_fn_scoped.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=unique_user_fn_scoped.group_id, name=f"unit-{uuid4()}")
    )
    nutrient_food = _nutrient_food(unique_user_fn_scoped, _source(unique_user_fn_scoped))
    portion = HealthLabsFoodPortion(
        nutrient_food_id=nutrient_food.id,
        description="one scoop",
        amount=1,
        unit="scoop",
        grams=40,
    )
    decision = _curator_decision(unique_user_fn_scoped, state="approved")
    session.add_all([portion, decision])
    session.commit()

    mapping = mapping_type(
        group_id=unique_user_fn_scoped.group_id,
        ingredient_food_id=ingredient_food.id,
        ingredient_unit_id=ingredient_unit.id,
        food_portion_id=portion.id,
        reviewed_by_user_id=unique_user_fn_scoped.user_id,
        curator_decision_id=decision.id,
    )
    session.add(mapping)
    session.commit()
    assert str(mapping.group_id) == unique_user_fn_scoped.group_id

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                mapping_type(
                    group_id=unique_user_fn_scoped.group_id,
                    ingredient_food_id=ingredient_food.id,
                    ingredient_unit_id=ingredient_unit.id,
                    food_portion_id=portion.id,
                    reviewed_by_user_id=unique_user_fn_scoped.user_id,
                    curator_decision_id=decision.id,
                )
            )
            session.flush()
    session.rollback()


@pytest.mark.parametrize("kind", ["ingredient_structure", "food_match", "portion_resolution"])
@pytest.mark.parametrize("state", ["pending", "running", "review_required", "completed", "failed"])
def test_nutrition_work_accepts_supported_kind_and_state(
    unique_user_fn_scoped: TestUser,
    kind: str,
    state: str,
) -> None:
    work = _nutrition_work(unique_user_fn_scoped, kind=kind, state=state)

    unique_user_fn_scoped.repos.session.flush()

    assert work.attempt_count == 0
    assert work.lease_expires_at is None
    assert work.provider_requested_at is None
    assert work.result is None
    assert work.last_error is None
    unique_user_fn_scoped.repos.session.rollback()


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "unsupported"), ("state", "unsupported"), ("attempt_count", -1)],
)
def test_nutrition_work_rejects_invalid_control_values(
    unique_user_fn_scoped: TestUser,
    field: str,
    value: str | int,
) -> None:
    work = _nutrition_work(unique_user_fn_scoped)
    setattr(work, field, value)

    with pytest.raises(IntegrityError):
        unique_user_fn_scoped.repos.session.flush()
    unique_user_fn_scoped.repos.session.rollback()


def test_nutrition_work_has_one_row_per_kind_and_target(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    target_id = str(uuid4())
    _nutrition_work(unique_user_fn_scoped, target_id=target_id)
    _nutrition_work(unique_user_fn_scoped, kind="portion_resolution", target_id=target_id)
    session.flush()

    _nutrition_work(unique_user_fn_scoped, target_id=target_id)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_nutrition_work_accepts_recipe_ingredient_target_id(unique_user_fn_scoped: TestUser) -> None:
    work = _nutrition_work(unique_user_fn_scoped, target_id="42")

    unique_user_fn_scoped.repos.session.flush()

    assert work.target_id == "42"
    unique_user_fn_scoped.repos.session.rollback()


def test_provider_usage_has_one_nonnegative_counter_per_utc_date(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    usage_date = date(2026, 10, 17)
    session.add(HealthLabsNutritionProviderUsage(usage_date=usage_date, request_count=0))
    session.flush()

    session.add(HealthLabsNutritionProviderUsage(usage_date=usage_date, request_count=1))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    session.add(HealthLabsNutritionProviderUsage(usage_date=date(2026, 10, 18), request_count=-1))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_source_release_identity_is_unique(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    _source(unique_user_fn_scoped, provider="usda", version="2026-04")

    session.add(
        HealthLabsNutrientSourceRelease(
            provider="usda",
            version="2026-04",
            country="OTHER",
            source_url="https://example.test/usda/replacement",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            attribution="Replacement",
            sha256="b" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_provider_checksum_cannot_be_relabelled_as_another_version(
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    source = _source(unique_user_fn_scoped, provider="ciqual", version="2025")

    session.add(
        HealthLabsNutrientSourceRelease(
            provider="ciqual",
            version="2026",
            country="FR",
            source_url="https://example.test/ciqual/2026",
            license_url="https://example.test/open-licence",
            attribution="ANSES",
            sha256=source.sha256,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_source_food_identity_is_unique_within_release(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    source = _source(unique_user_fn_scoped)
    food = _nutrient_food(unique_user_fn_scoped, source)

    session.add(
        HealthLabsNutrientFood(
            source_release_id=source.id,
            source_food_id=food.source_food_id,
            name="Duplicate",
            nutrient_values={},
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_food_has_only_one_match_and_records_review(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    ingredient_food = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(
            group_id=unique_user_fn_scoped.group_id,
            name=f"lentils-{uuid4()}",
        )
    )
    first_source = _source(unique_user_fn_scoped, provider="cofid", version="2021")
    first = _nutrient_food(unique_user_fn_scoped, first_source)
    second_source = _source(unique_user_fn_scoped, provider="usda", version="2026-04")
    second = _nutrient_food(unique_user_fn_scoped, second_source)
    reviewed_at = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
    match = HealthLabsFoodMatch(
        ingredient_food_id=ingredient_food.id,
        nutrient_food_id=first.id,
        country="GB",
        match_method="manual",
        confidence=1,
        food_group="pulse",
        plant_id="red_lentil",
        allergens={"gluten": "doesnt_contain"},
        diets={"vegan": "doesnt_contain"},
        reviewed_by_user_id=unique_user_fn_scoped.user_id,
        reviewed_at=reviewed_at,
    )
    session.add(match)
    session.flush()

    assert match.reviewed_by_user_id == unique_user_fn_scoped.user_id
    assert match.reviewed_at == reviewed_at

    session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=ingredient_food.id,
            nutrient_food_id=second.id,
            country="OTHER",
            match_method="exact",
            confidence=0.8,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_food_match_rejects_confidence_outside_unit_interval(
    unique_user_fn_scoped: TestUser,
    confidence: float,
) -> None:
    session = unique_user_fn_scoped.repos.session
    ingredient_food = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=f"food-{uuid4()}")
    )
    nutrient_food = _nutrient_food(unique_user_fn_scoped, _source(unique_user_fn_scoped))
    session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=ingredient_food.id,
            nutrient_food_id=nutrient_food.id,
            country="OTHER",
            match_method="exact",
            confidence=confidence,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_food_match_requires_review_actor_and_time_together(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    ingredient_food = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=f"food-{uuid4()}")
    )
    nutrient_food = _nutrient_food(unique_user_fn_scoped, _source(unique_user_fn_scoped))
    session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=ingredient_food.id,
            nutrient_food_id=nutrient_food.id,
            country="OTHER",
            match_method="manual",
            confidence=1,
            reviewed_by_user_id=unique_user_fn_scoped.user_id,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


@pytest.mark.parametrize(("amount", "grams"), [(0, 10), (1, 0), (-1, 10), (1, -10)])
def test_food_portion_requires_positive_amount_and_grams(
    unique_user_fn_scoped: TestUser,
    amount: float,
    grams: float,
) -> None:
    session = unique_user_fn_scoped.repos.session
    nutrient_food = _nutrient_food(unique_user_fn_scoped, _source(unique_user_fn_scoped))
    session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=nutrient_food.id,
            description="one portion",
            amount=amount,
            unit="piece",
            grams=grams,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


@pytest.mark.parametrize(("source_managed", "has_group"), [(True, True), (False, False)])
def test_food_portion_requires_source_or_editor_ownership(
    unique_user_fn_scoped: TestUser,
    source_managed: bool,
    has_group: bool,
) -> None:
    session = unique_user_fn_scoped.repos.session
    nutrient_food = _nutrient_food(unique_user_fn_scoped, _source(unique_user_fn_scoped))
    session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=nutrient_food.id,
            group_id=unique_user_fn_scoped.group_id if has_group else None,
            description="one portion",
            amount=1,
            unit="piece",
            grams=10,
            source_managed=source_managed,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_branded_product_identity_is_provider_plus_barcode(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    barcode = "0123456789012"
    first = HealthLabsBrandedProduct(
        provider="open_food_facts",
        barcode=barcode,
        product_name="Test product",
        source_version="2026-10-17",
        source_url="https://world.openfoodfacts.org/product/0123456789012",
        license_url="https://opendatacommons.org/licenses/odbl/1-0/",
        nutrient_values={"kcal": {"amount": 100, "unit": "kcal"}},
        payload={"code": barcode},
    )
    second_provider = HealthLabsBrandedProduct(
        provider="manufacturer",
        barcode=barcode,
        product_name="Test product",
        source_version="label-1",
        source_url="https://manufacturer.example/product",
        license_url="https://manufacturer.example/terms",
        nutrient_values={},
        payload={},
    )
    session.add_all([first, second_provider])
    session.flush()

    session.add(
        HealthLabsBrandedProduct(
            provider="open_food_facts",
            barcode=barcode,
            product_name="Duplicate",
            source_version="2026-10-18",
            source_url="https://world.openfoodfacts.org/product/0123456789012",
            license_url="https://opendatacommons.org/licenses/odbl/1-0/",
            nutrient_values={},
            payload={},
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
