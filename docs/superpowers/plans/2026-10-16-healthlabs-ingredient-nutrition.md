# HealthLabs Ingredient Nutrition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make HealthLabs Mealie the only editable source of truth for ingredient food facts and calculated recipe nutrition, then cut Ephra over without changing its public Nutrition API.

**Architecture:** Mealie imports pinned authoritative food-source releases into global provenance tables, maps group-scoped Mealie foods to those records, resolves source-backed portions to grams, and returns calculated nutrition through the existing HealthLabs recipe API. Ephra consumes that contract and retains only health context, saved-meal facts, published-plan snapshots, and a one-release read-only rollback path.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL/SQLite, pytest, HTTPX, Ephra FastAPI provider adapter

**Spec:** `docs/healthlabs/nutrition-service-v1.md`

## Global Constraints

- Missing or ambiguous values stay `null`; never coerce them to zero.
- No generated or estimated nutrient value may be persisted without an identified source record.
- Mealie is the only editable owner of food matches, food facts, conversions, and recipe calculations.
- Open Food Facts data stays logically separate from the generic catalogue because it is ODbL-licensed.
- Only `nutrition:write` may change matches, portions, or curated safety metadata; every write is audited.
- Existing recipe ownership, catalogue immutability, plan-editor grants, tenant isolation, and AGPL service separation remain unchanged.
- Do not drop the Ephra legacy tables in the same release that switches reads.

## Review Focus

- An ingredient matched to a food but missing a unit conversion returns `grams=null` and incomplete recipe totals.
- A reviewed match survives a repeated source import and is not silently replaced by a name match.
- A source file whose bytes change under an existing version is rejected by checksum.
- A nutrition editor cannot read or mutate a food outside the recipe visibility granted to that principal.
- Recipe servings of zero expose totals only; they never divide by zero or report per-serving values.

---

### Task 1: Persist source provenance, food facts, matches, and portions in Mealie

**Repository:** `HealthLabsGroup/mealie`

**Files:**
- Modify: `mealie/db/models/healthlabs.py`
- Modify: `mealie/db/models/_all_models.py`
- Modify: `mealie/schema/healthlabs/identity.py`
- Create: `mealie/alembic/versions/2026-10-16-10.00.00_<revision>_add_healthlabs_nutrition.py`
- Modify: `mealie/services/backups_v2/alchemy_exporter.py`
- Test: `tests/integration_tests/healthlabs/test_nutrition_models.py`
- Test: `tests/unit_tests/healthlabs/test_nutrition_migration.py`

**Interfaces:**
- Produces: SQLAlchemy models `HealthLabsNutrientSourceRelease`, `HealthLabsNutrientFood`, `HealthLabsFoodMatch`, `HealthLabsFoodPortion`, and `HealthLabsBrandedProduct`.
- Produces: `HealthLabsCapability.NUTRITION_WRITE = "nutrition:write"`.

- [ ] **Step 1: Write failing model tests.** Assert source identity/checksum uniqueness; source-food identity uniqueness; one active match per `ingredient_food_id`; positive portion grams; `confidence` between 0 and 1; reviewed actor/timestamps; and branded products isolated by provider plus barcode.
- [ ] **Step 2: Run the focused tests.** Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_models.py -q`; expect missing model/table failures.
- [ ] **Step 3: Add the five models and migration.** Store source nutrient values in JSON keyed by canonical nutrient code and unit; keep safety facts nullable so unknown never means safe.
- [ ] **Step 4: Register backup/export support and the capability.** Add all new tables to the same database backup path as the existing HealthLabs records.
- [ ] **Step 5: Verify both databases and migration shape.** Run the model tests on SQLite and PostgreSQL plus `task py:test -- tests/unit_tests/healthlabs/test_nutrition_migration.py -q`; expect PASS.
- [ ] **Step 6: Commit.** Commit as `feat(healthlabs): add ingredient nutrition data model`.

### Task 2: Import pinned authoritative source releases

**Repository:** `HealthLabsGroup/mealie`

**Files:**
- Create: `mealie/schema/healthlabs/nutrition_source.py`
- Create: `mealie/services/healthlabs/nutrition_import_service.py`
- Create: `mealie/services/healthlabs/nutrition_sources.py`
- Create: `scripts/healthlabs/import-nutrition-source.py`
- Create: `docs/healthlabs/nutrition-sources.md`
- Test: `tests/unit_tests/healthlabs/test_nutrition_sources.py`
- Test: `tests/integration_tests/healthlabs/test_nutrition_import.py`

**Interfaces:**
- Produces: `SourceManifest(provider, version, country, source_url, license_url, attribution, sha256)`.
- Produces: `SourceFoodRow(source_food_id, name, preparation_state, nutrients_per_100g, portions)`.
- Produces: `NutritionImportService.import_release(manifest: SourceManifest, rows: Iterable[SourceFoodRow]) -> NutritionImportResult`.

- [ ] **Step 1: Write parser contract tests.** Cover USDA FoodData Central, CoFID, CIQUAL, and BLS fixtures; assert canonical `kcal`, `protein_g`, `fibre_g`, `carbs_g`, `fat_g`, and `water_ml` mappings while preserving other source nutrients in the raw value map.
- [ ] **Step 2: Write import safety tests.** Assert idempotent re-import, rejection of a mismatched checksum, atomic rollback on a malformed row, and preservation of reviewed matches.
- [ ] **Step 3: Implement provider parsers and the transactional importer.** The command accepts explicit local source files plus a checked manifest; it does not download mutable URLs during import.
- [ ] **Step 4: Document versions and licences.** Pin current USDA, CoFID, CIQUAL, and BLS release metadata; record Poland as blocked on licence and Ukraine as having no verified national import.
- [ ] **Step 5: Verify.** Run `task py:test -- tests/unit_tests/healthlabs/test_nutrition_sources.py tests/integration_tests/healthlabs/test_nutrition_import.py -q`; expect PASS.
- [ ] **Step 6: Commit.** Commit as `feat(healthlabs): import authoritative food data`.

### Task 3: Match Mealie foods and calculate ingredient and recipe nutrition

**Repository:** `HealthLabsGroup/mealie`

**Files:**
- Create: `mealie/schema/healthlabs/nutrition.py`
- Create: `mealie/services/healthlabs/nutrition_service.py`
- Modify: `mealie/schema/healthlabs/recipe.py`
- Modify: `mealie/services/healthlabs/recipe_api_service.py`
- Test: `tests/unit_tests/healthlabs/test_nutrition_calculation.py`
- Test: `tests/integration_tests/healthlabs/test_recipe_nutrition.py`

**Interfaces:**
- Produces: `NutritionService.match_food(food_id: UUID, country: str) -> HealthLabsFoodMatchOut | None`.
- Produces: `NutritionService.food_facts(food_id: UUID, unit_id: UUID | None, quantity: float | None) -> HealthLabsIngredientNutrition`.
- Produces: `NutritionService.calculate_recipe(recipe: Recipe, country: str) -> HealthLabsRecipeNutrition`.
- Extends: `HealthLabsFoodOut.nutrition`, `HealthLabsIngredientOut.nutrition`, and `HealthLabsRecipeOut.calculated_nutrition` with nullable, backward-compatible fields.

- [ ] **Step 1: Write failing matching tests.** Exact normalized names and curated aliases may create candidates; ambiguous names, preparation mismatches, and unlicensed-country gaps remain unreviewed or unknown.
- [ ] **Step 2: Write failing calculation tests.** Cover grams, kilograms, ounces, source-backed millilitres/pieces, missing conversions, explicit zero, partial nutrients, zero servings, and stable decimal rounding.
- [ ] **Step 3: Implement the minimal matcher.** It uses normalized names and aliases only; reviewed matches win and are never overwritten automatically.
- [ ] **Step 4: Implement deterministic calculations.** Return `complete`, `missing_ingredient_references`, and `source_versions`; do not write to upstream `recipe_nutrition`.
- [ ] **Step 5: Expose calculations on HealthLabs recipe reads.** Current and revision endpoints use the same calculation service; existing recipe fields remain unchanged.
- [ ] **Step 6: Verify.** Run `task py:test -- tests/unit_tests/healthlabs/test_nutrition_calculation.py tests/integration_tests/healthlabs/test_recipe_nutrition.py tests/integration_tests/healthlabs/test_recipe_api.py -q`; expect PASS.
- [ ] **Step 7: Commit.** Commit as `feat(healthlabs): calculate recipe nutrition from foods`.

### Task 4: Add audited nutrition-editor and food-search APIs

**Repository:** `HealthLabsGroup/mealie`

**Files:**
- Create: `mealie/routes/healthlabs/nutrition.py`
- Modify: `mealie/routes/healthlabs/__init__.py`
- Modify: `mealie/schema/healthlabs/__init__.py`
- Modify: `mealie/services/healthlabs/access_policy.py`
- Modify: `mealie/services/healthlabs/nutrition_service.py`
- Test: `tests/integration_tests/healthlabs/test_nutrition_api.py`
- Test: `tests/integration_tests/healthlabs/test_nutrition_capability_matrix.py`
- Test: `tests/integration_tests/healthlabs/test_nutrition_audit.py`

**Interfaces:**
- Produces: the six `/api/healthlabs/v1/nutrition/*` routes defined by the spec.
- Consumes: `HealthLabsCapability.NUTRITION_WRITE` and existing `HealthLabsAccessAudit`.

- [ ] **Step 1: Write the capability matrix tests.** Ordinary users read visible food facts only; catalogue and plan editors gain no implicit write access; nutrition editors may change matches across permitted workspaces with an access reason; platform break-glass remains audited.
- [ ] **Step 2: Write mutation tests.** Require idempotency keys and access reasons; cover accept, replace, clear match, add portion, stale source ID, cross-group food ID, and repeated requests.
- [ ] **Step 3: Implement routes through `NutritionService`.** Reuse `IdempotencyService` and the existing immutable audit model; do not add a second authorization framework.
- [ ] **Step 4: Generate exported schemas and TypeScript types.** Run `task dev:generate`; never edit generated frontend types manually.
- [ ] **Step 5: Verify.** Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_api.py tests/integration_tests/healthlabs/test_nutrition_capability_matrix.py tests/integration_tests/healthlabs/test_nutrition_audit.py -q`; expect PASS.
- [ ] **Step 6: Commit.** Commit as `feat(healthlabs): add audited nutrition editing api`.

### Task 5: Backfill the real Mealie catalogue and classify every food

**Repository:** `HealthLabsGroup/mealie`

**Files:**
- Create: `scripts/healthlabs/backfill-food-matches.py`
- Create: `scripts/healthlabs/report-nutrition-coverage.py`
- Modify: `docs/healthlabs/nutrition-sources.md`
- Test: `tests/integration_tests/healthlabs/test_nutrition_backfill.py`

**Interfaces:**
- Produces: `matched`, `review_required`, and `unknown` counts covering every distinct `ingredient_foods.id`.
- Produces: a JSON report with recipe/ingredient coverage, source versions, and no nutrient values or personal recipe text.

- [ ] **Step 1: Write failing idempotency and coverage tests.** Re-running the backfill changes no reviewed match, and every food appears in exactly one terminal classification.
- [ ] **Step 2: Implement batched matching.** Use exact normalized names and aliases; never fuzzy-accept ambiguous candidates.
- [ ] **Step 3: Implement the coverage report.** Include counts by source, country, status, missing conversion, and missing core nutrient; exclude user identifiers and recipe text.
- [ ] **Step 4: Run against a restored DEV copy.** Import the four approved source releases, run the backfill, and save only aggregate counts in the release notes.
- [ ] **Step 5: Verify the target DEV workspace.** Assert all 27,828 migrated ingredient rows are accounted for: rows with structured foods resolve to matched, review-required, or unknown, while unparsed rows remain explicitly unparsed with null facts; assert no mock-source record exists.
- [ ] **Step 6: Commit.** Commit as `feat(healthlabs): backfill ingredient nutrition matches`.

### Task 6: Cut Ephra recipe and meal normalization reads over to Mealie

**Repository:** `EphyrTech/ephra-be`

**Files:**
- Modify: `app/services/recipes/contracts.py`
- Modify: `app/services/recipes/mealie_client.py`
- Modify: `app/services/recipes/mealie_provider.py`
- Modify: `app/services/recipes/mapper.py`
- Modify: `app/services/recipes/facade.py`
- Modify: `app/services/plans/validation_service.py`
- Replace: `app/services/food_normalization_service.py`
- Modify callers: `app/api/foods.py`, `app/services/meal_service.py`, `app/services/nutrition_meal_service.py`, `app/services/nutrition_week_service.py`, `app/services/recipe_import_service.py`, `app/services/recipe_service.py`
- Add setting: `app/core/config.py`
- Test: `tests/recipes/test_provider_contract.py`, `tests/recipes/test_recipe_mapper.py`, `tests/plans/test_plan_validation.py`, `tests/nutrition/test_food_api.py`, `tests/nutrition/test_food_normalization.py`, `tests/nutrition/test_meal_api.py`, `tests/nutrition/test_week_service.py`

**Interfaces:**
- Consumes: Mealie food search, ingredient nutrition, and calculated recipe nutrition responses.
- Preserves: current Ephra `/v1/foods`, `/v1/recipes`, meal logging, week view, and plan publication DTOs.
- Produces: `NUTRITION_FACTS_PROVIDER=mealie|legacy`, defaulting to `legacy` until DEV verification.

- [ ] **Step 1: Extend provider contract tests.** Parse nullable facts, provenance, incompleteness, and unknown statuses; reject malformed numeric values and unexpected safety statuses.
- [ ] **Step 2: Replace recipe enrichment reads.** Map Mealie ingredient facts directly and remove new writes from `RecipeEnrichmentService`; keep the table read-only for rollback.
- [ ] **Step 3: Replace all `FoodNormalizationService` callers.** Preserve existing Ephra DTOs and event-time values stored in `MealItem`; photo candidates remain unknown until matched by Mealie.
- [ ] **Step 4: Replace plan validation enrichment queries.** Batch-read pinned Mealie recipe nutrition and safety facts; refuse publication on provider outage, conflicts, or unacknowledged unknown status.
- [ ] **Step 5: Add shadow comparison.** Under `legacy`, fetch Mealie facts asynchronously for metrics only; log IDs, statuses, and numeric differences without food text, patient data, or provider payloads.
- [ ] **Step 6: Verify.** Run `uv run pytest tests/recipes tests/plans/test_plan_validation.py tests/nutrition/test_food_api.py tests/nutrition/test_food_normalization.py tests/nutrition/test_meal_api.py tests/nutrition/test_week_service.py -q`; expect PASS.
- [ ] **Step 7: Commit.** Commit as `feat(nutrition): read food facts from mealie`.

### Task 7: Deploy, switch, and retire duplicate Ephra ownership safely

**Repositories:** `HealthLabsGroup/mealie`, `EphyrTech/ephra-be`, deployment configuration

**Files:**
- Modify: `HealthLabsGroup/mealie/docs/healthlabs/nutrition-service-v1.md`
- Modify: `EphyrTech/ephra-be/docs/architecture/healthlabs-recipe-service.md`
- Create later: `EphyrTech/ephra-be/alembic/versions/<date>-drop_legacy_nutrition_catalogue.py`
- Modify later: `EphyrTech/ephra-be/app/models/nutrition.py`, `app/models/recipe_integration.py`, `app/db/models.py`
- Delete later: `EphyrTech/ephra-be/app/services/recipes/enrichment_service.py`, `app/services/recipes/enrichment_backfill.py`
- Update tests: `EphyrTech/ephra-be/tests/test_fresh_database_migrations.py`, `tests/recipes/test_integration_models.py`, and obsolete enrichment/catalogue fixture tests

**Interfaces:**
- Consumes: verified DEV coverage report and paired database/image backup manifest.
- Produces: Mealie-only editable nutrition ownership with a one-release rollback window.

- [ ] **Step 1: Release and pin Mealie.** Run `task py:check`, `task ui:check`, HealthLabs smoke tests, backup restore, and container scan; publish an immutable HealthLabs tag and digest.
- [ ] **Step 2: Deploy additively on Mac mini DEV.** Import pinned source releases, run the food backfill, and keep Ephra on `NUTRITION_FACTS_PROVIDER=legacy`.
- [ ] **Step 3: Approve the shadow comparison.** Require zero authorization leaks, no fabricated values, complete terminal classification, and explained differences before switching.
- [ ] **Step 4: Switch DEV to `NUTRITION_FACTS_PROVIDER=mealie`.** Verify food search, meal logging, recipe list/detail, plan safety validation, shopping, weekly figures, and saved snapshots with the `blesiv` browser profile.
- [ ] **Step 5: Preserve rollback for one release.** Stop all legacy catalogue/enrichment writes but retain their tables and the feature-flag fallback.
- [ ] **Step 6: Remove duplicate tables in a later release.** Take a paired backup, remove legacy code/models/tables and feature flag, run fresh-database and upgrade migrations, then test restore.
- [ ] **Step 7: Commit each repository separately.** Use `docs(architecture): record mealie nutrition ownership`, then `refactor(nutrition): remove legacy food catalogue` only after the rollback window.

## Final verification

- [ ] Run complete Mealie backend/frontend checks and HealthLabs smoke scripts.
- [ ] Run complete Ephra backend checks plus recipe, nutrition, plan, and export tests.
- [ ] Confirm the Mealie release source link is visible and points to the deployed commit.
- [ ] Confirm Ephra contains no editable nutrient source, matching rule, or food-fact writer.
- [ ] Confirm existing meal logs and published plan snapshots retain their historical figures after a source update.
