# HealthLabs Nutrition Source Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let authenticated nutrition editors discover authoritative source-food IDs through the running Mealie API before using the existing audited match mutation.

**Architecture:** Add one read-only query to `NutritionService`, expose it from the existing HealthLabs nutrition router, and generate the public Python/TypeScript schema exports. Matching authorization and mutation behavior remain unchanged.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy, Pydantic, pytest, generated TypeScript schemas.

**Spec:** `docs/superpowers/specs/2026-10-17-healthlabs-nutrition-source-search.md`

## Global Constraints

- No schema migration and no new dependency.
- Source search is authenticated but does not require `nutrition:write`; all mutations keep the existing capability, reason, idempotency, and audit requirements.
- Only requested-country and `OTHER` source releases may be returned.
- Search is token-based and deterministic; it never approves fuzzy matches.
- Missing nutrient values remain `null`.

## Review Focus

- SQL wildcard or punctuation-only input must not broaden into a full-catalogue query.
- A GB query must not leak FR/DE source rows.
- Reordered tokens such as `olive oil` must match `Oil, olive`.
- Stable ordering must not depend on database insertion order.
- Existing match writes must still reject callers without `nutrition:write`.

---

### Task 1: Add authoritative source-food search

**Files:**
- Modify: `mealie/schema/healthlabs/nutrition.py`
- Generated: `mealie/schema/healthlabs/__init__.py`
- Modify: `mealie/services/healthlabs/nutrition_service.py`
- Modify: `mealie/routes/healthlabs/nutrition.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_api.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_capability_matrix.py`
- Generated: `frontend/app/lib/api/types/healthlabs.ts`
- Generated: `tests/utils/api_routes/__init__.py`
- Modify: `docs/healthlabs/nutrition-service-v1.md`

**Interfaces:**
- Consumes: `HealthLabsNutrientFood`, `HealthLabsNutrientSourceRelease`, `HealthLabsFoodPortion`, and the existing authenticated nutrition router.
- Produces: `NutritionService.search_source_foods(query: str, country: str, limit: int) -> list[HealthLabsNutrientFoodOut]` and `GET /api/healthlabs/v1/nutrition/source-foods`.

- [x] **Step 1: Write failing API tests.** Add `test_source_food_search_is_tokenized_country_scoped_and_stable` with GB, OTHER, and FR rows; assert `olive oil` returns GB then OTHER, excludes FR, includes nutrients/portions/provenance, honors limit, and returns `[]` for punctuation-only input. Extend the capability test to prove an ordinary authenticated user can search while match writes remain denied.
- [x] **Step 2: Run the focused tests.** Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_api.py tests/integration_tests/healthlabs/test_nutrition_capability_matrix.py -q`; expect the source-food route assertions to fail with HTTP 404.
- [x] **Step 3: Implement the minimal schema, service query, and route.** Normalize query tokens and source names, filter country to requested plus `OTHER`, rank requested-country rows first, then shortest name, case-insensitive name, and ID, and cap `limit` at 50.
- [x] **Step 4: Generate exports.** Run `task dev:generate`; retain only generated schema/type/route changes caused by the new response and route.
- [x] **Step 5: Document the endpoint.** Add the exact route, permission boundary, country scope, and no-auto-approval rule to `docs/healthlabs/nutrition-service-v1.md`.
- [x] **Step 6: Verify.** Run the focused API/capability tests, `tests/unit_tests/healthlabs/test_nutrition_calculation.py`, Ruff formatting/linting for touched Python files, and generated-file checks; expect PASS.
- [x] **Step 7: Commit.** Commit as `feat(healthlabs): search nutrition source foods`.

## Final verification

- [x] Run the complete backend test suite.
- [x] Review the branch for authorization regression, country leakage, unbounded query behavior, and fabricated facts.
