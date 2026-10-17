from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from mealie.db.models.healthlabs import (
    HealthLabsBrandedProduct,
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.schema.recipe import SaveIngredientFood
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
