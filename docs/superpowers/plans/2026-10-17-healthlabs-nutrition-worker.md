# HealthLabs Nutrition Reconciliation Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continuously reconcile unresolved Mealie ingredient structure, source-backed nutrition matches, and gram-conversion gaps in safe, restartable background batches.

**Architecture:** Reuse Mealie's five-minute scheduler and add one HealthLabs-specific persisted work table. A bounded asynchronous worker performs deterministic work first, conditionally calls the group's configured provider, creates only review-required source matches, and exposes status plus actionable review rows to the existing Nutrition Curator.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy/Alembic, PostgreSQL/SQLite, Mealie scheduler, Vue 3/Nuxt 4, Vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-10-17-healthlabs-nutrition-worker-design.md`

## Global Constraints

- Reuse `SchedulerRegistry.register_minutely`; do not add a service, container, queue dependency, or package.
- Schedule every five minutes; batch size `20`; lease `15` minutes; three total attempts with retry delays at `5m` and `30m`.
- Provider calls default off through `HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT=0` and use a persisted UTC-day request counter when enabled.
- Never derive nutrient values from provider output; imported source releases remain the only nutrition truth.
- Persist ingredient structure only with positive quantity and exact existing group food/unit matches.
- Persist automatic food candidates only as unreviewed `HealthLabsFoodMatch` rows.
- Resolve grams only through existing mass units or authoritative/reviewed `HealthLabsFoodPortion` rows; never infer grams with the provider.
- Preserve source text and never log secrets, prompts, raw responses, recipe text, or nutrient values.
- Commit messages must not mention automated assistants.

## Review Focus

- Scheduler overlap or restart: one target is claimed once and an expired lease is reclaimable.
- Unchanged ambiguous input: it remains review-required without repeated provider spend.
- Changed recipe text or catalogue/source release: its fingerprint resets work to pending.
- Missing/deleted targets and cross-group IDs: stale work is removed and no other group is read or changed.
- Provider timeout, malformed output, or exhausted allowance: deterministic work continues and retry state remains valid.

---

### Task 1: Persist bounded nutrition work

**Files:**
- Modify: `mealie/db/models/healthlabs.py`
- Create: `mealie/alembic/versions/2026-10-17-21.00.00_9e13f4a6b8c0_add_healthlabs_nutrition_work.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_models.py`

**Interfaces:**
- Consumes: existing `BaseMixins`, `GUID`, group IDs, and SQLAlchemy migration conventions.
- Produces: `HealthLabsNutritionWork` with `kind`, `target_id`, `group_id`, `input_sha256`, `state`, `attempt_count`, `next_attempt_at`, `lease_expires_at`, `provider_requested_at`, `result`, and `last_error`; unique `(kind, target_id)` and indexed due-work fields. Also produces `HealthLabsNutritionProviderUsage` with unique UTC `usage_date` and atomic `request_count`.

- [ ] **Step 1: Write failing model tests** for valid states/kinds, unique target identity, group cascade, attempt non-negativity, nullable lease/result/error behavior, and one provider-usage counter per UTC date.
- [ ] **Step 2: Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_models.py -q`**; expect failures because the model/table do not exist.
- [ ] **Step 3: Add the model and migration** with database checks for kinds `ingredient_structure|food_match|portion_resolution` and states `pending|running|review_required|completed|failed`.
- [ ] **Step 4: Run the focused model tests and upgrade/downgrade migration tests**; expect PASS on SQLite and PostgreSQL-backed test configuration.
- [ ] **Step 5: Commit** as `feat(healthlabs): persist nutrition reconciliation work`.

### Task 2: Reconcile deterministic work iteratively

**Files:**
- Create: `mealie/services/healthlabs/nutrition_worker.py`
- Modify: `mealie/services/healthlabs/ingredient_structure_service.py`
- Create: `tests/integration_tests/healthlabs/test_nutrition_worker.py`

**Interfaces:**
- Consumes: `IngredientStructureService.structure`, `NutritionService.match_food`, `HealthLabsFoodMatch`, the work model from Task 1, and group-scoped repositories.
- Produces: `NutritionReconciliationWorker(session: Session, *, country: str = "GB", batch_size: int = 20, provider_daily_request_limit: int = 0, now: datetime | None = None)` and `async run() -> NutritionWorkerReport`; `IngredientStructureService.apply_exact(ingredient: RecipeIngredient, parsed: RecipeIngredient) -> bool`.

- [ ] **Step 1: Write failing discovery tests** proving bounded discovery, stable target fingerprints, group isolation, stale-target cleanup, and input-changed requeue.
- [ ] **Step 2: Write failing deterministic-processing tests** proving exact ingredient updates, preserved source text, unreviewed exact/alias food-match creation, authoritative portion resolution, unresolved conversion review rows, independent target commits, deterministic ordering, idempotent repeat runs, and continued progress after one target fails.
- [ ] **Step 3: Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_worker.py -q`**; expect import/behavior failures.
- [ ] **Step 4: Implement the minimum worker discovery/claim/finalize loop** and extract only the exact parsed-result application needed to reuse `IngredientStructureService` validation.
- [ ] **Step 5: Run the worker, backfill, and calculation suites**; expect PASS and unchanged legacy script behavior.
- [ ] **Step 6: Commit** as `feat(healthlabs): reconcile nutrition data in batches`.

### Task 3: Parse unresolved ingredients through the configured provider

**Files:**
- Modify: `mealie/services/healthlabs/nutrition_worker.py`
- Modify: `mealie/core/settings/settings.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_worker.py`
- Modify: `tests/unit_tests/test_config.py`

**Interfaces:**
- Consumes: `OpenAIParser.parse_one`, group provider settings, `IngredientStructureService.apply_exact`, and persisted work attempts.
- Produces: provider fallback inside `NutritionReconciliationWorker.run()` and `AppSettings.HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT: int = 0`.

- [ ] **Step 1: Write failing tests** for default-disabled calls, configured-provider parsing, exact-result application, ambiguous-result review storage, missing provider waiting, malformed output, 5m/30m transient retry timing, third-attempt failure, UTC-day allowance persistence, next-day deferral without attempt consumption, provider-config wake-up, and unchanged-input no-respend.
- [ ] **Step 2: Run the focused worker/config tests**; expect provider-stage failures.
- [ ] **Step 3: Implement one provider call per claimed ingredient** until the persisted daily allowance is exhausted; store only a sanitized structured suggestion or error type/status.
- [ ] **Step 4: Run worker, parser, OpenAI-service, and config tests**; expect PASS.
- [ ] **Step 5: Commit** as `feat(healthlabs): parse unresolved nutrition work`.

### Task 4: Run the worker from Mealie's scheduler

**Files:**
- Create: `mealie/services/scheduler/tasks/reconcile_healthlabs_nutrition.py`
- Modify: `mealie/services/scheduler/tasks/__init__.py`
- Modify: `mealie/app.py`
- Create: `tests/unit_tests/services_tests/scheduler/tasks/test_reconcile_healthlabs_nutrition.py`

**Interfaces:**
- Consumes: `NutritionReconciliationWorker.run()`, `session_context`, application settings, and `SchedulerRegistry.register_minutely`.
- Produces: `reconcile_healthlabs_nutrition() -> None`, a synchronous scheduler callback that calls `asyncio.run()` from the scheduler thread.

- [ ] **Step 1: Write failing scheduler tests** proving registration, one session/run per tick, settings propagation, exception containment, and no overlapping in-process invocation.
- [ ] **Step 2: Run the focused scheduler tests**; expect missing-task/registration failures.
- [ ] **Step 3: Implement and register the task** without changing scheduler cadence or adding dependencies.
- [ ] **Step 4: Run all scheduler tests**; expect PASS.
- [ ] **Step 5: Commit** as `feat(healthlabs): schedule nutrition reconciliation`.

### Task 5: Expose status and actionable review work in Nutrition Curator

**Files:**
- Modify: `mealie/schema/healthlabs/nutrition.py`
- Modify: `mealie/routes/healthlabs/nutrition.py`
- Modify: `mealie/services/healthlabs/nutrition_worker.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_api.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_capability_matrix.py`
- Modify: `frontend/app/lib/api/user/healthlabs-nutrition.ts`
- Modify: `frontend/app/composables/use-nutrition-curator.ts`
- Modify: `frontend/app/pages/nutrition.vue`
- Modify: `frontend/app/pages/nutrition.test.ts`
- Modify: `frontend/app/composables/__tests__/use-nutrition-curator.test.ts`
- Regenerate: `frontend/app/lib/api/types/healthlabs.ts`
- Modify: `frontend/app/lang/messages/en-US.json`

**Interfaces:**
- Consumes: work-state aggregation and review rows scoped to the current user's group, the existing `nutrition:write` capability, access audit, idempotency, exact ingredient validation, and the existing audited portion action.
- Produces: `GET /api/healthlabs/v1/nutrition/worker-status` returning state counts, provider-enabled/budget-exhausted flags, and `last_updated_at`; `GET /api/healthlabs/v1/nutrition/work?state=review_required&kind=...`; idempotent `POST /api/healthlabs/v1/nutrition/work/{id}/approve` for a stored current ingredient suggestion; `HealthLabsNutritionApi` methods and Curator status/review panels.

- [ ] **Step 1: Write failing API/capability tests** for group scoping, `nutrition:write`, empty state, all counts, provider flags, sanitized review output, idempotent ingredient approval, audit creation, stale-fingerprint rejection, and cross-group rejection.
- [ ] **Step 2: Write failing frontend tests** for loading, status rendering, exhausted/disabled copy, ingredient approval, portion-gap handoff to the existing food portion editor, refresh with the food queue, and API errors that do not hide the queue.
- [ ] **Step 3: Run the focused backend/frontend tests**; expect missing endpoint/UI failures.
- [ ] **Step 4: Implement the status/review schemas, routes, audited exact approval service, API client, composable state, compact accessible panels, and only `en-US` strings.**
- [ ] **Step 5: Run `task dev:generate`**, then focused API, capability, composable, and page tests; expect PASS.
- [ ] **Step 6: Commit** as `feat(healthlabs): show nutrition worker status`.

### Task 6: Document and verify the complete workflow

**Files:**
- Modify: `docs/healthlabs/nutrition-service-v1.md`
- Modify: `docs/healthlabs/nutrition-sources.md`
- Modify: `docs/superpowers/plans/2026-10-17-healthlabs-nutrition-worker.md`

**Interfaces:**
- Consumes: completed worker, scheduler, API, UI, migration, and setting.
- Produces: operator documentation for cadence, states, provider allowance, retry/recovery, Curator review, monitoring, and rollback.

- [ ] **Step 1: Document runtime behavior and the exact configuration boundary** including provider calls defaulting off and the requirement to approve a deployment allowance.
- [ ] **Step 2: Run backend gates:** focused worker/API/scheduler/model/parser suites, `task py:format`, `task py:lint`, `task py:mypy`, and migration upgrade/downgrade verification; expect PASS.
- [ ] **Step 3: Run frontend gates:** `task ui:test` and `task ui:check`; expect PASS.
- [ ] **Step 4: Run repository checks:** `git diff --check`, generated-file check, secret scan, and confirm no provider key in tracked files; expect clean output.
- [ ] **Step 5: Exercise a local runtime tick** with deterministic fixtures, provider allowance `0`, restart simulation, and a mocked provider-enabled tick; expect correct state transitions and no duplicate writes.
- [ ] **Step 6: Commit** as `docs(healthlabs): document nutrition reconciliation`.

## Deployment Gate

Before enabling provider calls on Mac mini DEV, calculate the request allowance against the selected Qwen model and obtain explicit approval for the daily monetary ceiling. Deploy first with allowance `0`, verify deterministic reconciliation and Curator status, then set the approved positive request limit and observe the first paid batch.
