from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mealie.db.models.healthlabs import (
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.schema.healthlabs.nutrition_source import NutritionImportResult, SourceFoodRow, SourceManifest


class SourceChecksumMismatch(ValueError):
    pass


class NutritionImportService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def import_release(
        self,
        manifest: SourceManifest,
        rows: Iterable[SourceFoodRow],
    ) -> NutritionImportResult:
        created_release = False
        foods_imported = 0
        portions_imported = 0

        with self.session.begin_nested():
            release = self.session.scalars(
                select(HealthLabsNutrientSourceRelease).where(
                    HealthLabsNutrientSourceRelease.provider == manifest.provider,
                    HealthLabsNutrientSourceRelease.version == manifest.version,
                    HealthLabsNutrientSourceRelease.country == manifest.country,
                )
            ).one_or_none()
            if release is not None and release.sha256 != manifest.sha256:
                raise SourceChecksumMismatch("source bytes changed under an existing provider/version/country")

            checksum_release = self.session.scalars(
                select(HealthLabsNutrientSourceRelease).where(
                    HealthLabsNutrientSourceRelease.provider == manifest.provider,
                    HealthLabsNutrientSourceRelease.sha256 == manifest.sha256,
                )
            ).one_or_none()
            if release is None and checksum_release is not None:
                raise SourceChecksumMismatch("source checksum is already registered under another release")

            if release is None:
                release = HealthLabsNutrientSourceRelease(
                    provider=manifest.provider,
                    version=manifest.version,
                    country=manifest.country,
                    source_url=manifest.source_url,
                    license_url=manifest.license_url,
                    attribution=manifest.attribution,
                    sha256=manifest.sha256,
                )
                self.session.add(release)
                self.session.flush()
                created_release = True

            existing_foods = {
                food.source_food_id: food
                for food in self.session.scalars(
                    select(HealthLabsNutrientFood).where(HealthLabsNutrientFood.source_release_id == release.id)
                )
            }
            seen: set[str] = set()
            for row in rows:
                if row.source_food_id in seen:
                    raise ValueError(f"duplicate source food id: {row.source_food_id}")
                seen.add(row.source_food_id)
                nutrient_values = {
                    code: value.model_dump(mode="json") for code, value in row.nutrients_per_100g.items()
                }
                nutrient_values["_raw"] = row.raw_nutrients

                food = existing_foods.get(row.source_food_id)
                if food is None:
                    food = HealthLabsNutrientFood(
                        source_release_id=release.id,
                        source_food_id=row.source_food_id,
                        name=row.name,
                        preparation_state=row.preparation_state,
                        nutrient_values=nutrient_values,
                    )
                    self.session.add(food)
                    self.session.flush()
                else:
                    food.name = row.name
                    food.preparation_state = row.preparation_state
                    food.nutrient_values = nutrient_values

                self.session.execute(
                    delete(HealthLabsFoodPortion).where(
                        HealthLabsFoodPortion.nutrient_food_id == food.id,
                        HealthLabsFoodPortion.source_managed.is_(True),
                    )
                )
                source_portions = row.portions
                self.session.add_all(
                    HealthLabsFoodPortion(
                        nutrient_food_id=food.id,
                        description=portion.description,
                        amount=portion.amount,
                        unit=portion.unit,
                        grams=portion.grams,
                        source_managed=True,
                    )
                    for portion in source_portions
                )
                foods_imported += 1
                portions_imported += len(source_portions)

            self.session.flush()

        self.session.commit()
        return NutritionImportResult(
            source_release_id=release.id,
            created_release=created_release,
            foods_imported=foods_imported,
            portions_imported=portions_imported,
        )
