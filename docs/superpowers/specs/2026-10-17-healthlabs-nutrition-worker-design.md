# HealthLabs Nutrition Reconciliation Worker Design

## Intent

Mealie must continuously resolve nutrition work created by imported, edited, or newly added recipes without requiring an operator to run scripts. The worker handles safe, repeatable normalization itself and leaves only ambiguous source decisions for a nutrition editor in the existing Curator.

## Goals

- Discover unresolved recipe ingredients and ingredient foods every five minutes using Mealie's existing scheduler.
- Process small, bounded batches until no work remains, then become idle until data changes.
- Run the existing deterministic ingredient structurer before using the configured group provider.
- Use the configured provider only for ingredient structure; nutrient values and gram conversions always come from imported authoritative source releases or nutrition-editor-reviewed portions.
- Automatically persist an ingredient structure only when quantity is positive and food and unit resolve uniquely to existing group-scoped catalogue records.
- Create source-backed food matches as `review_required`; only a nutrition editor may approve them.
- Persist progress, retry transient failures three times, recover expired leases after restart, and stop retrying unchanged ambiguous or terminal work.
- Discover conversion gaps after a reviewed food match, and leave only gaps without a unique authoritative conversion for a nutrition editor.
- Show pending, running, review-required, completed, and failed counts plus actionable review items in the Nutrition Curator.

## Non-goals

- Generating calories, macros, allergens, diets, gram conversions, portions, or clinical conclusions with a language model.
- Silently approving ambiguous or model-selected nutrient-source matches.
- Adding Redis, Celery, a second worker container, or a general-purpose job framework.
- Replacing the existing scripts; they remain available for diagnostics and recovery.

## Architecture

The existing five-minute `SchedulerRegistry` callback starts one synchronous scheduler task, which runs an asynchronous `NutritionReconciliationWorker` in the scheduler thread. A HealthLabs-specific database table stores one bounded work record per target, and a second one-row-per-UTC-day table atomically reserves provider requests. The worker discovers currently unresolved targets, fingerprints their relevant input, claims due rows with an expiring lease, and commits each target independently.

Work has three kinds:

1. `ingredient_structure`: first run `IngredientStructureService`; if that cannot resolve the text and the daily provider allowance permits it, parse with the group's default provider. Apply the result only through the same exact catalogue resolver. Otherwise retain the suggestion as `review_required` without changing the recipe.
2. `food_match`: run the existing country-aware authoritative-source matcher. A unique exact or alias candidate is persisted as an unreviewed `HealthLabsFoodMatch`, so it immediately appears in the Curator queue. No candidate becomes reviewed automatically.
3. `portion_resolution`: after a reviewed match, re-check an ingredient whose quantity/unit cannot convert to grams. Existing exact source-managed portions resolve automatically through `NutritionService`; any remaining gap becomes `review_required` and links the nutrition editor to the existing audited portion editor. The worker never invents grams.

The worker defaults to country `GB`, batch size `20`, a five-minute schedule, a 15-minute lease, and three total attempts with retry delays of 5 minutes and 30 minutes. Work is processed deterministically by `next_attempt_at`, kind, then target ID. A changed input fingerprint resets terminal work to pending. Missing targets are removed. Work is group-scoped.

Ingredient fingerprints contain source text plus the group's food/unit catalogue revision. Food-match fingerprints contain normalized food name/aliases, country, and the current source-release checksum. Portion fingerprints contain ingredient quantity/unit, reviewed match, country, and source-release checksum. Changing provider configuration wakes provider-waiting ingredient work without re-spending unchanged review-required work.

Budget exhaustion defers provider-waiting work until the next UTC day without consuming an attempt. Missing provider configuration waits until configuration changes. No unique deterministic/provider candidate becomes `review_required` until its fingerprint changes. Transient provider failures consume an attempt and follow the two retry delays; the third failure becomes `failed`.

## Provider Budget Boundary

Provider calls are disabled by default. `HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT` defaults to `0`; a positive value enables at most that many persisted provider attempts per UTC day across restarts. The Mac mini deployment value must be chosen from an explicitly approved monetary budget before enabling automatic paid processing. Deterministic structuring and source matching continue when the provider allowance is zero or exhausted.

## Safety and Privacy

- Preserve `original_text`, recipe ownership, substitutions, references, and author-visible text.
- Never log provider keys, prompts, raw responses, recipe text, or nutrient values.
- Store only the structured suggestion needed for review plus sanitized error type/status.
- A work-row lease prevents duplicate processing and expires after a crash.
- Every target is committed independently so one failure cannot roll back or block the remaining batch.
- The Docker image and repository contain no provider secret.

## Curator Visibility

`GET /api/healthlabs/v1/nutrition/worker-status` requires the existing `nutrition:write` capability and returns state counts, whether provider processing is enabled or budget-exhausted, and the most recent worker update time. `GET /api/healthlabs/v1/nutrition/work` returns only the current group's actionable review rows. `POST /api/healthlabs/v1/nutrition/work/{id}/approve` can apply only a stored, still-current exact ingredient suggestion; stale fingerprints are rejected. Both actions are audited under `nutrition:write`. Portion gaps use the existing audited add-portion action. The `/nutrition` page shows status and review items above the existing food queue. It never exposes provider prompts or raw responses.

## Acceptance Criteria

1. New unresolved data is discovered automatically and processed in later scheduler ticks without an operator command.
2. Repeated ticks are idempotent and do not recreate matches or re-spend provider allowance for unchanged review-required work.
3. A restart reclaims expired running work and continues.
4. Exact deterministic work continues when the provider is unavailable or its allowance is exhausted.
5. A provider result cannot change a recipe unless quantity, food, and unit pass exact validation.
6. Source matches remain unreviewed until a nutrition editor approves them.
7. Curator status and tests prove pending, retry, review, completed, failed, budget-exhausted, and input-changed behavior.
8. Conversion gaps are discovered automatically; only authoritative exact conversions resolve without review, and adding an editor-reviewed portion completes the gap on a later tick.
9. An editor can act on each review-required ingredient/portion row, while stale or cross-group approval is rejected and audited.
