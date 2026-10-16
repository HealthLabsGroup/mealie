from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.schema.healthlabs.nutrition_source import NutrientValue, SourceFoodRow, SourceManifest
from mealie.schema.recipe import SaveIngredientFood
from mealie.services.healthlabs.nutrition_import_service import NutritionImportService, SourceChecksumMismatch
from tests.utils import TestUser


def _manifest(*, provider: str | None = None, sha256: str | None = None) -> SourceManifest:
    provider = provider or f"test-{uuid4()}"
    return SourceManifest(
        provider=provider,
        version="2026-10",
        country="GB",
        source_url=f"https://example.test/{provider}.csv",
        license_url="https://example.test/licence",
        attribution="Test source",
        sha256=sha256 or uuid4().hex.ljust(64, "0"),
    )


def _row(source_food_id: str = "food-1", *, name: str = "Cooked lentils") -> SourceFoodRow:
    return SourceFoodRow(
        source_food_id=source_food_id,
        name=name,
        preparation_state="cooked",
        nutrients_per_100g={
            "kcal": NutrientValue(amount=116, unit="kcal"),
            "protein_g": NutrientValue(amount=9.02, unit="g"),
        },
        raw_nutrients={"Calcium": {"amount": 19.0, "unit": "mg"}},
        portions=[{"description": "cup", "amount": 1, "unit": "cup", "grams": 198}],
    )


def test_reimport_is_idempotent_and_updates_existing_food(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    service = NutritionImportService(session)
    manifest = _manifest()

    first = service.import_release(manifest, [_row()])
    first_food = session.scalars(
        select(HealthLabsNutrientFood).where(HealthLabsNutrientFood.source_release_id == first.source_release_id)
    ).one()
    second = service.import_release(manifest, [_row(name="Cooked green lentils")])
    second_food = session.scalars(
        select(HealthLabsNutrientFood).where(HealthLabsNutrientFood.source_release_id == first.source_release_id)
    ).one()

    assert first.created_release is True
    assert second.created_release is False
    assert second.source_release_id == first.source_release_id
    assert second_food.id == first_food.id
    assert second_food.name == "Cooked green lentils"
    assert (
        session.scalar(
            select(func.count())
            .select_from(HealthLabsNutrientSourceRelease)
            .where(HealthLabsNutrientSourceRelease.provider == manifest.provider)
        )
        == 1
    )
    session.commit()


def test_changed_checksum_under_existing_release_is_rejected(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    service = NutritionImportService(session)
    manifest = _manifest()
    service.import_release(manifest, [_row()])

    changed = manifest.model_copy(update={"sha256": "f" * 64})
    with pytest.raises(SourceChecksumMismatch):
        service.import_release(changed, [_row()])

    stored = session.scalars(
        select(HealthLabsNutrientSourceRelease).where(HealthLabsNutrientSourceRelease.provider == manifest.provider)
    ).one()
    assert stored.sha256 == manifest.sha256
    session.commit()


def test_malformed_stream_rolls_back_the_entire_release(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    service = NutritionImportService(session)
    manifest = _manifest()

    def broken_rows() -> Iterator[SourceFoodRow]:
        yield _row()
        raise ValueError("malformed source row")

    with pytest.raises(ValueError, match="malformed source row"):
        service.import_release(manifest, broken_rows())

    assert (
        session.scalar(
            select(func.count())
            .select_from(HealthLabsNutrientSourceRelease)
            .where(HealthLabsNutrientSourceRelease.provider == manifest.provider)
        )
        == 0
    )
    session.rollback()


def test_reimport_preserves_reviewed_food_match(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    service = NutritionImportService(session)
    manifest = _manifest()
    result = service.import_release(manifest, [_row()])
    nutrient_food = session.scalars(
        select(HealthLabsNutrientFood).where(HealthLabsNutrientFood.source_release_id == result.source_release_id)
    ).one()
    ingredient_food = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=f"lentils-{uuid4()}")
    )
    reviewed_at = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
    match = HealthLabsFoodMatch(
        ingredient_food_id=ingredient_food.id,
        nutrient_food_id=nutrient_food.id,
        country="GB",
        match_method="manual",
        confidence=1,
        reviewed_by_user_id=unique_user_fn_scoped.user_id,
        reviewed_at=reviewed_at,
    )
    session.add(match)
    session.commit()
    match_id = match.id

    service.import_release(manifest, [_row()])
    preserved = session.get(HealthLabsFoodMatch, match_id)

    assert preserved is not None
    assert preserved.nutrient_food_id == nutrient_food.id
    assert preserved.reviewed_by_user_id == unique_user_fn_scoped.user_id
    assert preserved.reviewed_at == reviewed_at
    session.commit()
