# HealthLabs AI Nutrition Curator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve defensible `review_required` nutrition work with a group-scoped autonomous Curator user while preserving Mealie's source truth, cost controls, audit trail, and tenant boundaries.

**Architecture:** Keep deterministic reconciliation in the existing scheduler, then run one global Curator loop as a separate process from the same repository and image. Persist each bounded evaluation, call the group's existing provider once, validate the structured selection against current database state, and apply accepted choices only through HealthLabs domain services as the managed group agent-user.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, SQLAlchemy/Alembic, PostgreSQL/SQLite, OpenAI-compatible structured responses, Vue 3, Nuxt 4, TypeScript, pytest, Vitest.

**Spec:** `docs/superpowers/specs/2026-10-18-healthlabs-ai-nutrition-curator-design.md`

## Global Constraints

- Support only `human` and `agent` user types; existing users and all ordinary creation paths default to `human`.
- Provision one tokenless, non-admin Apple agent per group under `healthlabs:agent:nutrition-curator:<group-id>`, displayed as `Apple — Nutrition Curator`.
- Keep persona version `healthlabs-nutrition-curator-apple-v1`; personality may shape attention, voice, and explanation but never candidate membership, thresholds, source truth, or application policy.
- Supply at most eight candidates of each applicable kind and accept only supplied, current, same-group or authoritative-source IDs.
- Apply a selection only at confidence `>= 0.95` and winner margin `>= 0.15`; otherwise record terminal `insufficient_evidence`.
- Never invent foods, units, portions, grams, nutrients, allergens, diets, plant metadata, or safety data.
- Existing reviewed matches, explicit suppressions, concurrent edits, ownership changes, and changed fingerprints win.
- Reuse the deployment-wide UTC request/USD/token guard; make at most one provider request per decision and no paid call unless all positive limits are configured.
- Curator is disabled by default; tests use fake structured responses and cost `$0`; deploy and paid provider calls are separate operator actions.
- Add no dependency, public Curator API, service token, message broker, or Ephra ownership change.
- Give Apple no invented credentials, demographic biography, free-form memory, or claim of being a biological human.
- Commit messages must not mention automated assistants.

## Review Focus

- A forged provider result or instruction embedded in a candidate label containing an unknown, deleted, cross-group, cross-country, or preparation-incompatible ID must terminate without a nutrition write; Tasks 3–4 test these variants.
- An agent account presented to password, OIDC, JWT, reset, API-token, or admin user-management paths must be rejected without changing the managed identity; Task 2 tests each boundary.
- A provider response arriving after a target, source release, catalogue, suppression, reviewer, capability, provider, prompt, or policy change must become stale and preserve the newer state; Task 4 tests these races.
- Concurrent workers or a process restart must not spend twice or apply twice for one `(kind, target, curator fingerprint)`; Task 5 tests claims, expired leases, unique decisions, and shared-budget contention.
- Missing portions and ambiguous candidates must remain explainable abstentions, while Apple's persona remains recognizable without changing the policy-only fixture outcomes; Tasks 3–5 test empty candidates, thresholds, persona consistency, third failure, and unchanged-fingerprint reruns.

---

### Task 1: Persist user types, Curator decisions, and portion mappings

**Files:**
- Modify: `mealie/db/models/users/users.py`
- Modify: `mealie/db/models/users/__init__.py`
- Modify: `mealie/db/models/healthlabs.py`
- Modify: `mealie/schema/user/user.py`
- Create: `mealie/alembic/versions/2026-10-18-13.00.00_c146e8a30f9d_add_healthlabs_nutrition_curator.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_models.py`
- Modify: `tests/unit_tests/healthlabs/test_nutrition_migration.py`

**Interfaces:**
- Produces: `UserType(StrEnum)` with `HUMAN = "human"` and `AGENT = "agent"`, and `User.user_type: UserType` defaulting to `human`.
- Produces: `HealthLabsNutritionCuratorDecision` fields `id`, nullable `work_id`, `group_id`, `kind`, `target_id`, `work_input_sha256`, `curator_sha256`, `agent_user_id`, `state`, `attempt_count`, `next_attempt_at`, `lease_expires_at`, `candidate_snapshot`, nullable `selected_ids`, nullable `confidence`, nullable `runner_up_confidence`, nullable `reason_code`, nullable `rationale`, nullable `provider_id`, `provider_name`, `provider_model`, `prompt_version`, nullable `requested_at`, and nullable `usage`; `BaseMixins` supplies timestamps.
- Produces: `HealthLabsFoodPortionMapping` fields `id`, `group_id`, `ingredient_food_id`, `ingredient_unit_id`, `food_portion_id`, `reviewed_by_user_id`, and `curator_decision_id`, uniquely mapping `(group_id, ingredient_food_id, ingredient_unit_id)` to an existing portion.
- Produces: read-only `UserOut.user_type`/`PrivateUser.user_type` serialization as `human|agent`; `UserBase` and `UserIn` remain unable to request an agent type.

- [ ] **Step 1: Write failing model tests.** Add tests named `test_user_type_defaults_to_human`, `test_curator_decision_enforces_identity_state_and_scores`, `test_curator_decision_is_unique_per_complete_fingerprint`, `test_terminal_curator_evidence_is_immutable`, and `test_portion_mapping_is_group_scoped_and_unique`; assert terminal states are `approved|insufficient_evidence|failed`, scores are finite and within `[0, 1]`, attempts are non-negative, mapping references are foreign-key protected, and one group food/unit pair cannot receive two mappings. Task 4 owns runtime cross-group validation because the existing food/unit tables do not expose composite ownership keys.
- [ ] **Step 2: Write the failing migration test.** Upgrade from `b035d7f29e8c`, assert every legacy user reads `user_type='human'`, the new column/tables/checks/indexes/FKs exist, deleting nutrition work sets `decision.work_id` to `NULL`, and downgrade refuses while agent users or Curator audit/mapping rows remain before removing the new tables/column from an empty Curator state.
- [ ] **Step 3: Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_models.py tests/unit_tests/healthlabs/test_nutrition_migration.py -q`.** Expect missing enum/model/table/column failures.
- [ ] **Step 4: Implement the models and migration.** Store `UserType` as a non-native string enum for SQLite/PostgreSQL parity; use group cascade, work `ON DELETE SET NULL`, and restrictive candidate/source references; block mutation of a terminal decision except group-level database cascade.
- [ ] **Step 5: Regenerate schema exports/types with `task dev:generate`.** Do not hand-edit `frontend/app/lib/api/types/` or generated `__init__.py` files.
- [ ] **Step 6: Rerun the focused model/migration tests.** Expect PASS on the configured SQLite suite and the existing PostgreSQL-compatible constraint assertions.
- [ ] **Step 7: Commit.** Commit as `feat(healthlabs): persist curator evidence`.

### Task 2: Provision and protect the group Curator identity

**Files:**
- Create: `mealie/services/healthlabs/nutrition_curator_identity_service.py`
- Modify: `mealie/core/security/providers/auth_provider.py`
- Modify: `mealie/core/dependencies/dependencies.py`
- Modify: `mealie/routes/users/api_tokens.py`
- Modify: `mealie/routes/users/crud.py`
- Modify: `mealie/routes/admin/admin_management_users.py`
- Modify: `mealie/services/user_services/password_reset_service.py`
- Create: `tests/integration_tests/healthlabs/test_nutrition_curator_identity.py`

**Interfaces:**
- Produces: `NutritionCuratorIdentityService(session: Session).get_or_create(group_id: UUID) -> PrivateUser`.
- Produces: deterministic subject `healthlabs:agent:nutrition-curator:<group-id>`, username `nutrition-curator-<group-id-hex>`, email `<group-id-hex>@nutrition-curator.invalid`, full name `Apple — Nutrition Curator`, oldest-household assignment ordered by `(created_at, id)`, a hash of `secrets.token_urlsafe(48)` that is never returned, and exactly one active `nutrition:write` grant.
- Produces: `assert_interactive_user(user: PrivateUser) -> None`, used by session/token/reset/user-management boundaries to reject `user_type=agent` or protected managed-agent mutation.
- Consumes: `HealthLabsCapabilityGrant`, existing password hashing, `PrivateUser`, group/household rows, and existing route exception patterns.

- [ ] **Step 1: Write failing provisioning tests.** Assert repeated and concurrent `get_or_create()` calls return one agent in the requested group, select the oldest current household deterministically, grant only `nutrition:write`, create no `LongLiveToken`, and never repair a deliberately revoked grant during an active decision.
- [ ] **Step 2: Write failing security-boundary tests.** Assert credentials/OIDC token minting, an already-signed JWT lookup, password reset, self password update, API-token creation, and admin rename/move/password-token/delete each return the surrounding path's normal `403`/non-enumerating failure and leave the agent unchanged; group deletion remains allowed to cascade it.
- [ ] **Step 3: Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_curator_identity.py tests/integration_tests/healthlabs/test_provisioning.py -q`.** Expect missing service and unprotected-route failures.
- [ ] **Step 4: Implement lazy provisioning and one shared guard.** Build the internal `PrivateUser` from the persisted row; do not log in, mint a token, add admin/household-manager rights, or expose a provisioning endpoint.
- [ ] **Step 5: Apply the guard at the common token mint/current-user boundaries and at mutation endpoints.** Preserve existing timing/non-enumeration behavior for failed credentials and password-reset requests.
- [ ] **Step 6: Rerun the identity, provisioning, auth, API-token, password, and admin-user focused tests.** Expect PASS with unchanged human-user behavior.
- [ ] **Step 7: Commit.** Commit as `feat(healthlabs): add curator identity`.

### Task 3: Build bounded candidate sets and strict provider responses

**Files:**
- Create: `mealie/schema/healthlabs/nutrition_curator.py`
- Create: `mealie/services/healthlabs/nutrition_curator_candidates.py`
- Create: `mealie/services/healthlabs/nutrition_curator_prompt.py`
- Create: `mealie/services/openai/prompts/healthlabs/nutrition-curator.txt`
- Create: `tests/fixtures/healthlabs/nutrition_curator_prompt_cases.json`
- Create: `tests/unit_tests/healthlabs/test_nutrition_curator_candidates.py`
- Create: `tests/unit_tests/healthlabs/test_nutrition_curator_prompt.py`
- Create: `tests/unit_tests/healthlabs/test_nutrition_curator_schema.py`

**Interfaces:**
- Produces: immutable `NutritionCuratorCandidateSet(kind, target_id, work_input_sha256, curator_sha256, prompt_payload, snapshot)` where snapshots contain IDs and public labels only.
- Produces: `NutritionCuratorCandidateService(session: Session, *, country: str = "GB").build(work: HealthLabsNutritionWork, provider: AIProvider) -> NutritionCuratorCandidateSet | None`.
- Produces: `NutritionCuratorPrompt.system() -> str`, `policy_only_control() -> str`, and `input(candidate_set: NutritionCuratorCandidateSet) -> str`; the control removes only the `<identity>` block, so policy/examples/context handling remain byte-identical.
- Produces: `NutritionCuratorProviderDecision` with `decision`, kind-specific selected IDs, finite `confidence`, finite `runner_up_confidence`, `reason_code`, and sanitized first-person `rationale` of at most 280 characters in Apple's voice. Provider reason codes are exactly `name_and_context_match|preparation_match|unit_and_context_match|insufficient_candidates`; application may persist `low_confidence|low_margin|invalid_selection|stale_fingerprint|provider_failure` instead.
- Consumes: `_ExactDataMatcher`, model normalization, RapidFuzz already pinned in `pyproject.toml`, imported source releases, same-group foods/units/portions, provider/model configuration, persona version `healthlabs-nutrition-curator-apple-v1`, prompt version `healthlabs-nutrition-curator-v1`, and policy version `healthlabs-nutrition-curator-policy-v1`.

`build()` returns `None` only when the work is no longer eligible. An eligible target with no candidates returns an empty candidate set so `start()` can persist `insufficient_evidence` without a provider call.

- [ ] **Step 1: Write failing ingredient candidate tests.** Assert positive quantity is mandatory; exact normalized names and aliases come first; RapidFuzz token similarity fills deterministic `(score desc, label, id)` order; food and unit lists are each capped at eight; another group's catalogue rows and ambiguous duplicate aliases are excluded.
- [ ] **Step 2: Write failing food/portion candidate tests.** Assert food candidates are capped at eight and restricted to requested-country/`OTHER` releases with compatible preparation state, while portion candidates include only source-managed or same-group `source_managed=false` rows for the currently matched nutrient food and never contain nutrient values or grams in the provider payload.
- [ ] **Step 3: Write failing prompt/persona tests.** Assert exact ordered sections `<identity>`, `<policy>`, `<decision_method>`, `<examples>`; Apple’s purpose/values/temperament/truthful agent identity; five examples for confident selection, low confidence, narrow margin, incompatible country/preparation, and candidate-label prompt injection; and dynamic JSON only inside a final `<untrusted_context>` block.
- [ ] **Step 4: Write failing fingerprint/schema tests.** Assert persona/catalogue/source/provider-model/prompt/policy/candidate changes alter `curator_sha256`; ordering does not; unknown fields, non-finite/out-of-range scores, markup/control characters, non-supplied labels, oversized rationale, inconsistent select/abstain fields, and duplicate selections fail validation or sanitization.
- [ ] **Step 5: Add golden fixture regression tests.** For ingredient, food, and portion cases, feed recorded policy-only and Apple responses through the same validator and assert identical selected IDs/abstentions, zero invalid writes, correct low-confidence/margin abstention, recognizable concise first-person voice, and rejection of fictional authority or emotional-pressure instructions. Keep the completion callable injectable so tests remain offline; a live provider run is a separate pre-enable operation.
- [ ] **Step 6: Run `task py:test -- tests/unit_tests/healthlabs/test_nutrition_curator_candidates.py tests/unit_tests/healthlabs/test_nutrition_curator_prompt.py tests/unit_tests/healthlabs/test_nutrition_curator_schema.py -q`.** Expect import failures.
- [ ] **Step 7: Implement the three retrieval paths, prompt builder, production prompt, and response schemas.** Put stable Apple identity first, immutable policy next, examples after policy, and dynamic sanitized target/candidate JSON last; do not include credentials, recipe bodies, private notes, nutrients, or portion grams.
- [ ] **Step 8: Rerun the candidate/prompt/schema tests.** Expect PASS and stable serialized snapshots/fingerprints.
- [ ] **Step 9: Commit.** Commit as `feat(healthlabs): bound curator candidates`.

### Task 4: Validate and apply Curator decisions through domain services

**Files:**
- Create: `mealie/services/healthlabs/nutrition_curator_service.py`
- Modify: `mealie/services/healthlabs/ingredient_structure_service.py`
- Modify: `mealie/services/healthlabs/nutrition_service.py`
- Modify: `mealie/services/healthlabs/nutrition_fingerprint_service.py`
- Create: `tests/integration_tests/healthlabs/test_nutrition_curator_service.py`
- Modify: `tests/unit_tests/healthlabs/test_nutrition_calculation.py`

**Interfaces:**
- Produces: `NutritionCuratorService(session: Session, *, country: str = "GB", now: datetime | None = None)`.
- Produces: `StartedCuratorDecision(decision_id, provider, prompt_name, prompt_payload, response_schema)` and `start(work_id: UUID) -> StartedCuratorDecision | None`; `None` means stale/ineligible work or a no-candidate abstention already finalized without a provider call.
- Produces: `apply(decision_id: UUID, response: NutritionCuratorProviderDecision, usage: dict[str, int]) -> str`, `defer_until(decision_id: UUID, when: datetime, reason_code: str) -> str`, and `fail(decision_id: UUID, exc: Exception) -> str`, each returning the persisted state.
- Produces: `IngredientStructureService.apply_selected(ingredient, *, quantity: float, food_id: UUID, unit_id: UUID, expected_input: str) -> bool` with same-group and compare-and-set validation.
- Extends: `NutritionService.set_match(..., match_method: Literal["manual", "agent"] = "manual")`; `agent` semantics refuse reviewed/suppressed rows and preserve optional food-group/plant/allergen/diet values, while existing `manual` semantics keep clearing suppressions and accepting the route payload. Agent calls pass `access_reason="Nutrition curator decision <id>"`.
- Extends: `NutritionService._resolve_grams()` to prefer a current `HealthLabsFoodPortionMapping` for the exact group food/unit and use the referenced portion's existing `amount`/`grams`.

- [ ] **Step 1: Write failing policy-gate tests.** Assert `select` at `0.949` confidence or `0.149` margin records `insufficient_evidence`; boundary values `0.95` and `0.15` are eligible; explicit abstention, empty candidates, invalid reason, unknown/duplicate/deleted IDs, and mismatched work kind never mutate nutrition data. Feed two otherwise identical valid responses with different Apple rationales and assert the same state/write; reject an invalid rationale before any write.
- [ ] **Step 2: Write failing stale/concurrency tests.** After `start()`, mutate each of target input, candidate, source release, catalogue, provider/model, prompt/policy fingerprint, group ownership, capability grant, reviewed match, and suppression; assert `apply()` marks the old evaluation stale/insufficient without overwriting the new state. Run two starts concurrently and assert the unique fingerprint produces one decision.
- [ ] **Step 3: Write failing ingredient and food application tests.** Assert ingredient application preserves original text and unrelated fields, uses exact supplied same-group IDs and compare-and-set, while food application calls audited authorization, records `match_method=agent`, reviewer agent ID/time, confidence, and leaves optional food-group/plant/allergen/diet metadata untouched.
- [ ] **Step 4: Write failing portion/calculation tests.** Assert accepted mappings reference an existing eligible portion, record agent and decision provenance, override only the exact food/unit pair, and calculate from existing `amount`/`grams`; cross-group portions, deleted portions, or no candidate abstain and never insert/update `HealthLabsFoodPortion`.
- [ ] **Step 5: Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_curator_service.py tests/unit_tests/healthlabs/test_nutrition_calculation.py -q`.** Expect missing service/interface failures.
- [ ] **Step 6: Implement start/revalidation/application with short transactions.** Use `NutritionCuratorPrompt` for the versioned system/input payload, a PostgreSQL row lock where available, and compare-and-set predicates that also work on SQLite; the caller commits the running decision before any provider call and reloads every referenced row afterward. Persist only the validated, sanitized rationale as display metadata; never consult it when deriving state or applying a selection.
- [ ] **Step 7: Rerun the focused service/calculation/nutrition-audit tests.** Expect PASS and no mutation on every rejected path.
- [ ] **Step 8: Commit.** Commit as `feat(healthlabs): apply curator decisions`.

### Task 5: Run the Curator as a bounded separate worker process

**Files:**
- Create: `mealie/services/healthlabs/nutrition_provider_budget.py`
- Modify: `mealie/services/healthlabs/nutrition_worker.py`
- Create: `mealie/services/healthlabs/nutrition_curator_worker.py`
- Create: `scripts/healthlabs/run-nutrition-curator.py`
- Modify: `mealie/core/settings/settings.py`
- Modify: `tests/unit_tests/test_config.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_worker.py`
- Create: `tests/integration_tests/healthlabs/test_nutrition_curator_worker.py`
- Create: `tests/unit_tests/healthlabs/test_run_nutrition_curator.py`

**Interfaces:**
- Produces: shared `NutritionProviderBudget.reserve(input_token_upper_bound: int, output_token_upper_bound: int) -> ProviderReservation | None` and `record(reservation: ProviderReservation, usage: dict[str, int]) -> None`, preserving the current atomic UTC usage row.
- Produces: `NutritionCuratorWorker(session: Session, *, country: str = "GB", lease_minutes: int = 15, now: datetime | None = None, should_stop: Callable[[], bool] = lambda: False).run_once() -> NutritionCuratorReport`, where the report contains `claimed`, `approved`, `insufficient_evidence`, `failed`, and `provider_exhausted`.
- Produces: settings `HEALTHLABS_NUTRITION_CURATOR_ENABLED: bool = False` and `HEALTHLABS_NUTRITION_CURATOR_POLL_SECONDS: Annotated[int, Field(ge=1)] = 30`.
- Produces: script `main() -> int` that uses `session_context()`, runs one decision at a time, waits interruptibly when idle, and exits before another claim after SIGTERM/SIGINT.
- Consumes: `OpenAIService.get_response()`, `NutritionCuratorCandidateService`, `NutritionCuratorService`, group `AIProviderSettings.default_provider`, and all existing positive request/USD/rate/output limits.

- [ ] **Step 1: Characterize the existing budget guard with its current worker tests, then write shared-budget failures.** Assert reconciliation and Curator reservations contend atomically for one request/USD ceiling, actual token usage releases only its own reservation, invalid usage fails closed, and exhaustion defers without consuming a decision attempt.
- [ ] **Step 2: Write failing worker lifecycle tests.** Assert disabled mode and groups without valid providers make no claim/call; due `review_required` work across groups is ordered deterministically; one run claims/evaluates/finalizes at most one decision; provider call receives the Apple system prompt plus only bounded untrusted context; unchanged approved/insufficient/failed fingerprints are not called again.
- [ ] **Step 3: Write failing retry/lease tests.** Assert provider failures retry after 5 and 30 minutes, fail terminally on attempt three, expired `running` leases are reclaimable, active leases are not, restart recovery is idempotent, and multiple workers do not call/apply the same decision twice.
- [ ] **Step 4: Write failing script/config tests.** Assert defaults are disabled/30 seconds, invalid poll values fail settings validation, SIGTERM/SIGINT interrupt idle waiting, shutdown during a bounded call lets that call finalize then exits, and logs contain IDs/state only—not keys, prompts, raw responses, recipe text, nutrient values, or rationale.
- [ ] **Step 5: Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_worker.py tests/integration_tests/healthlabs/test_nutrition_curator_worker.py tests/unit_tests/healthlabs/test_run_nutrition_curator.py tests/unit_tests/test_config.py -q`.** Expect missing budget/worker/script/setting failures.
- [ ] **Step 6: Extract the current budget code without behavior change, then implement `run_once()` and the thin loop.** Commit the running lease/candidate snapshot before calling `OpenAIService`; use `request_guard` and `usage_callback`; never hold a target row lock during network I/O.
- [ ] **Step 7: Rerun the focused reconciliation/Curator/config/script suites.** Expect PASS with zero real provider calls.
- [ ] **Step 8: Commit.** Commit as `feat(healthlabs): run nutrition curator worker`.

### Task 6: Expose agent attribution and sanitized Curator status

**Files:**
- Modify: `mealie/schema/healthlabs/nutrition.py`
- Modify: `mealie/services/healthlabs/nutrition_work_service.py`
- Modify: `mealie/routes/healthlabs/nutrition.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_api.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_capability_matrix.py`
- Regenerate: `frontend/app/lib/api/types/healthlabs.ts`
- Regenerate: relevant generated user TypeScript types containing `userType`
- Modify: `frontend/app/composables/use-nutrition-curator.ts`
- Modify: `frontend/app/composables/__tests__/use-nutrition-curator.test.ts`
- Modify: `frontend/app/pages/nutrition.vue`
- Modify: `frontend/app/pages/nutrition.test.ts`
- Modify: `frontend/app/lang/messages/en-US.json`

**Interfaces:**
- Extends: `HealthLabsNutritionWorkerStatus` with `curator_counts: dict[str, int]` and `curator_last_updated_at: datetime | None`.
- Extends: `HealthLabsNutritionWorkOut` with sanitized latest decision state, reason code, attempt count, optional `apple_note`, agent display name/type, and no prompt/raw response/private target text.
- Extends: `HealthLabsFoodMatchOut` with `reviewed_by` carrying reviewer `id`, display name, and `user_type`, optional `apple_note` from the latest approved decision for that target, and retained `reviewed_by_user_id` compatibility.
- Consumes: the existing `/worker-status` and `/work` routes; no new public Curator mutation endpoint.

- [ ] **Step 1: Write failing API tests.** Assert `nutrition:write` and group scoping still gate status/work, counts include all five decision states, latest time is correct, reviewer type is `agent`, absent/deleted reviewer is nullable, and `apple_note` exposes only the validated 280-character rationale with markup/control characters removed. Serialized output must exclude candidate snapshots, selected IDs, usage, the raw rationale field, prompts, recipe text, and provider credentials.
- [ ] **Step 2: Write failing frontend tests.** Assert agent-reviewed rows render `Reviewed by Apple — Nutrition Curator` plus an agent badge and optional `Apple’s note`; active/retry decisions render processing; terminal abstentions show the bounded reason code plus optional note; terminal failures show recovery-on-fingerprint-change copy; empty Curator state leaves existing food controls intact.
- [ ] **Step 3: Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_api.py tests/integration_tests/healthlabs/test_nutrition_capability_matrix.py -q` and `task ui:test -- frontend/app/composables/__tests__/use-nutrition-curator.test.ts frontend/app/pages/nutrition.test.ts`.** Expect schema/status/UI failures.
- [ ] **Step 4: Implement aggregate/query serialization and minimal UI changes.** Keep human controls as break-glass; do not add buttons that retry/delete immutable decisions or make autonomous operation depend on the page.
- [ ] **Step 5: Run `task dev:generate`, then rerun the focused backend/frontend tests.** Expect PASS and generated camelCase `userType`, `curatorCounts`, `appleNote`, and decision fields; modify only `en-US` source translations.
- [ ] **Step 6: Commit.** Commit as `feat(healthlabs): show curator status`.

### Task 7: Correct operations documentation and verify the feature boundary

**Files:**
- Modify: `docs/healthlabs/nutrition-service-v1.md`
- Modify: `docs/healthlabs/nutrition-sources.md`
- Modify: `docs/superpowers/plans/2026-10-18-healthlabs-ai-nutrition-curator.md`

**Interfaces:**
- Produces: operator command `uv run python scripts/healthlabs/run-nutrition-curator.py` for the second process using the same image/database/configuration.
- Produces: documented enable/disable, polling, provider prerequisites, retry/abstention, monitoring, rollback, Apple identity/persona version, agent attribution, and deterministic exact/alias behavior.
- Preserves: unique exact/curated-alias matches auto-apply with `auto_applied=true`; ambiguous/missing matches enter the Curator; Ephra cutover remains a separate downstream project.

- [ ] **Step 1: Correct the service contract.** Remove every statement that exact/alias candidates always remain unreviewed; document autonomous bounded selection, `insufficient_evidence`, immutable evidence, agent-user review, separate worker command, and break-glass UI.
- [ ] **Step 2: Document safe operation.** State disabled-by-default configuration, all required positive cost settings, one-process initial deployment, SIGTERM behavior, third-failure recovery only after fingerprint change, status fields, disable-first rollback, and why accepted matches remain ordinary reviewed matches. Require a live golden-set run comparing Apple with the policy-only control before enabling any new prompt/model pair; this operator check is outside automated tests and this branch.
- [ ] **Step 3: Run focused backend verification.** Run the Task 1–6 pytest files plus existing nutrition audit, capability, calculation, reconciliation-worker, auth, API-token, and migration tests; expect PASS without network access.
- [ ] **Step 4: Run focused generated/frontend verification.** Run `task dev:generate`, the Curator composable/page tests, and the frontend type check for affected files; expect no generated diff after the second generation and PASS.
- [ ] **Step 5: Run repository-local static checks.** Run Ruff format/lint and mypy on changed Python modules, frontend lint on changed Vue/TypeScript files, `git diff --check`, and a tracked-file secret scan; expect clean output. Do not deploy or enable paid provider calls.
- [ ] **Step 6: Commit.** Commit as `docs(healthlabs): document nutrition curator`.

## Final acceptance

- [ ] Curator disabled: deterministic reconciliation and existing break-glass UI behave exactly as before.
- [ ] Curator enabled with fake provider: one managed agent per group resolves eligible ingredient, food, and portion work or records an explainable abstention.
- [ ] Apple remains recognizably calm, concise, evidence-first, and truthful about being an agent, while the Apple and policy-only golden fixtures produce identical selections/abstentions through the same validator.
- [ ] Every accepted write is current, same-tenant, source-backed, audited to the agent, and idempotent under concurrency/restart.
- [ ] No accepted path creates source truth or gram data; no rejected/stale path mutates nutrition state.
- [ ] No deploy, paid provider request, or Ephra ownership migration is included in this branch.
