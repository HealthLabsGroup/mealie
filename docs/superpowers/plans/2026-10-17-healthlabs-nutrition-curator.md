# HealthLabs Nutrition Curator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a permission-gated native Mealie workbench for reviewing real ingredient-food nutrition matches and portions.

**Architecture:** Expose the current user's active HealthLabs capabilities through one authenticated read route, add a typed HealthLabs nutrition client to the existing `UserApi`, and drive a single responsive Vue page from a focused curator composable. Existing nutrition mutations remain the only write path.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy, Vue 3, Nuxt 4, Vuetify 4, TypeScript, pytest, Vitest.

**Spec:** `docs/superpowers/specs/2026-10-17-healthlabs-nutrition-curator-design.md`

## Global Constraints

- Runtime data comes only from Mealie's real database and pinned source releases.
- `nutrition:write` gates the navigation, page, and every mutation.
- No automatic approval, new dependency, database migration, or cross-workspace listing.
- Missing nutrient values remain unavailable; never coerce them to zero.
- Every write retains idempotency and a specific HealthLabs access reason.

## Review Focus

- A revoked or absent `nutrition:write` capability must redirect the route and still fail server writes.
- Switching users in one browser session must not reuse the previous user's capability cache.
- Empty, short, or failed source searches must preserve the selected food and explain the next action.
- A null nutrient must render as unavailable, while an explicit zero renders as `0`.
- Replacing, clearing, or adding a portion must update the selected food without duplicating queue items.

---

### Task 1: Expose active capabilities and gate the route

**Files:**
- Create: `mealie/routes/healthlabs/identity.py`
- Modify: `mealie/routes/healthlabs/__init__.py`
- Test: `tests/integration_tests/healthlabs/test_identity_api.py`
- Create: `frontend/app/composables/use-healthlabs-capabilities.ts`
- Test: `frontend/app/composables/__tests__/use-healthlabs-capabilities.test.ts`
- Create: `frontend/app/middleware/nutrition-write-only.ts`
- Create: `frontend/app/lib/api/user/healthlabs-nutrition.ts`
- Modify: `frontend/app/lib/api/client-user.ts`
- Test: `frontend/app/middleware/__tests__/nutrition-write-only.test.ts`

**Interfaces:**
- Produces: `GET /api/healthlabs/v1/capabilities -> HealthLabsCapability[]`.
- Produces: `UserApi.healthlabsNutrition.getCapabilities()`.
- Produces: `useHealthLabsCapabilities().load()` and `canWriteNutrition` keyed by the authenticated user ID.

- [x] **Step 1: Write failing backend and middleware tests.** Assert active-only capability discovery and redirect without `nutrition:write`, including a user switch.
- [x] **Step 2: Run both tests.** Expect the backend route to return 404 and the middleware import/behavior to fail because the capability client is absent.
- [x] **Step 3: Implement the route, capability client/cache, and middleware.** Reuse `RecipeAccessPolicy.active_capabilities`; do not infer capability from Mealie admin or organizer roles.
- [x] **Step 4: Generate route/type artifacts and rerun the focused tests.** Expect PASS.
- [x] **Step 5: Commit.** Commit as `feat(healthlabs): expose nutrition editor access`.

### Task 2: Add the typed nutrition curator client and state

**Files:**
- Modify: `frontend/app/lib/api/user/healthlabs-nutrition.ts`
- Create: `frontend/app/composables/use-nutrition-curator.ts`
- Test: `frontend/app/composables/__tests__/use-nutrition-curator.test.ts`

**Interfaces:**
- Consumes: existing `HealthLabsFoodNutritionPage`, `HealthLabsNutrientFoodOut`, `HealthLabsFoodMatchWrite`, and `HealthLabsFoodPortionCreate` types.
- Produces: queue/search state plus `loadFoods`, `selectFood`, `searchSources`, `approveSource`, `clearMatch`, and `addPortion` actions.

- [ ] **Step 1: Write failing composable tests.** Assert real endpoint-facing calls, null-preserving candidate data, audited/idempotent writes, in-place selected-food refresh, and error-state preservation.
- [ ] **Step 2: Run the focused test.** Expect failure because the client and composable do not exist.
- [ ] **Step 3: Implement the smallest client and composable.** Use the existing request abstraction; keep all queue derivation client-side for the current workspace response.
- [ ] **Step 4: Rerun the focused test.** Expect PASS.
- [ ] **Step 5: Commit.** Commit as `feat(healthlabs): add nutrition curator workflow`.

### Task 3: Build the native Mealie curator page

**Files:**
- Create: `frontend/app/pages/nutrition.vue`
- Test: `frontend/app/pages/nutrition.test.ts`
- Modify: `frontend/app/components/Layout/DefaultLayout.vue`
- Modify: `frontend/app/lang/messages/en-US.json`
- Modify: `docs/healthlabs/nutrition-service-v1.md`

**Interfaces:**
- Consumes: `useNutritionCurator` and `useHealthLabsCapabilities`.
- Produces: `/nutrition`, visible only to `nutrition:write` editors.

- [ ] **Step 1: Write a failing page test.** Assert the three queue states, candidate provenance/core nutrients, unavailable-vs-zero rendering, approve action, and measured-portion form labels.
- [ ] **Step 2: Run the page test.** Expect failure because the page is absent.
- [ ] **Step 3: Implement the responsive two-column page and conditional navigation link.** Use existing Vuetify/theme primitives and exact active-voice copy from the spec.
- [ ] **Step 4: Update English copy and service documentation.** Document the UI route, capability discovery, and real-data-only workflow. Other locales remain Crowdin-managed.
- [ ] **Step 5: Run focused frontend tests and lint.** Expect PASS with no accessibility or type/lint errors.
- [ ] **Step 6: Commit.** Commit as `feat(healthlabs): add nutrition curator page`.

## Final verification

- [ ] Run the complete backend suite, frontend suite, mypy, Ruff, ESLint, and production frontend build.
- [ ] Review authorization, tenant scoping, idempotency/audit headers, null rendering, and responsive layout.
