from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mealie.db.models.healthlabs import (
    HealthLabsFoodDensity,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.db.models.recipe import IngredientFoodModel
from mealie.schema.healthlabs.nutrition_source import (
    DensityImportResult,
    NutritionImportResult,
    SourceDensityRow,
    SourceFoodRow,
    SourceManifest,
)


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
        foods_imported = 0
        portions_imported = 0

        with self.session.begin_nested():
            release, created_release = self._source_release(manifest)

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
                        HealthLabsFoodPortion.density_id.is_(None),
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

    def import_density_release(
        self,
        manifest: SourceManifest,
        rows: Iterable[SourceDensityRow],
    ) -> DensityImportResult:
        with self.session.begin_nested():
            release, created_release = self._source_release(manifest)
            existing = {
                density.source_record_id: density
                for density in self.session.scalars(
                    select(HealthLabsFoodDensity).where(HealthLabsFoodDensity.source_release_id == release.id)
                )
            }
            seen: set[str] = set()
            densities: list[HealthLabsFoodDensity] = []
            for row in rows:
                if row.source_record_id in seen:
                    raise ValueError(f"duplicate density source record id: {row.source_record_id}")
                seen.add(row.source_record_id)
                density = existing.get(row.source_record_id)
                if density is None:
                    density = HealthLabsFoodDensity(
                        source_release_id=release.id,
                        source_record_id=row.source_record_id,
                        name=row.name,
                        name_normalized=IngredientFoodModel.normalize(row.name),
                        grams_per_ml=row.grams_per_ml,
                        reference_id=row.reference_id,
                    )
                    self.session.add(density)
                else:
                    density.name = row.name
                    density.name_normalized = IngredientFoodModel.normalize(row.name)
                    density.grams_per_ml = row.grams_per_ml
                    density.reference_id = row.reference_id
                densities.append(density)

            missing = existing.keys() - seen
            if missing:
                raise ValueError("density reimport omitted existing source records")
            self.session.flush()

            name_counts = Counter(density.name_normalized for density in densities)
            unique_densities = {
                density.name_normalized: density for density in densities if name_counts[density.name_normalized] == 1
            }
            foods_by_name: dict[str, list[HealthLabsNutrientFood]] = {}
            for food in self.session.scalars(select(HealthLabsNutrientFood)):
                foods_by_name.setdefault(IngredientFoodModel.normalize(food.name), []).append(food)

            linked = 0
            for normalized_name, density in unique_densities.items():
                for food in foods_by_name.get(normalized_name, []):
                    portion = self.session.scalar(
                        select(HealthLabsFoodPortion).where(
                            HealthLabsFoodPortion.nutrient_food_id == food.id,
                            HealthLabsFoodPortion.density_id == density.id,
                        )
                    )
                    if portion is None:
                        portion = HealthLabsFoodPortion(
                            nutrient_food_id=food.id,
                            density_id=density.id,
                            description="FAO/INFOODS density",
                            amount=1,
                            unit="ml",
                            grams=density.grams_per_ml,
                            source_managed=True,
                        )
                        self.session.add(portion)
                    else:
                        portion.grams = density.grams_per_ml
                    linked += 1
            self.session.flush()

        self.session.commit()
        return DensityImportResult(
            source_release_id=release.id,
            created_release=created_release,
            densities_imported=len(densities),
            portions_linked=linked,
            ambiguous_rows=sum(count for count in name_counts.values() if count > 1),
        )

    def _source_release(self, manifest: SourceManifest) -> tuple[HealthLabsNutrientSourceRelease, bool]:
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
        if release is not None:
            return release, False

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
        return release, True
