# HealthLabs AI Nutrition Curator Design

## Intent

Replace the human-operated nutrition Curator queue with Apple, a dedicated AI Nutrition Curator with a stable human-like character, while keeping Mealie as the only owner of nutrition truth, tenant policy, and audited writes. Apple is represented by a real group-scoped Mealie user, so accepted decisions continue to use `reviewed_by_user_id` and the existing reviewed-match semantics.

The Curator is implemented in the Mealie repository and image but runs as a separate worker process. This gives provider calls their own lifecycle and fault boundary without introducing a second service API, message broker, or duplicate domain rules.

## Decisions already made

- There is no human Curator in the normal operating model.
- The Curator is one versioned product persona named Apple. Every group receives a separate Apple agent-user for tenant isolation, but those users share the same character, values, voice, and decision discipline.
- Every user has a type. This change introduces only `human` and `agent`; more types are deferred until a concrete need exists.
- A Curator decision counts as review and records the Curator user in `reviewed_by_user_id`.
- Each group gets its own managed Curator agent-user and therefore its own tenant boundary, provider configuration, and capability grants.
- The AI may select only existing group records or imported authoritative source records. It may never invent a food ID, nutrients, grams, allergens, diets, or safety metadata.
- `insufficient_evidence` is a valid terminal decision. It is reconsidered only after the relevant source, catalogue, target, provider, model, prompt, or policy fingerprint changes.
- Existing human or agent-reviewed decisions and explicit match suppressions always win over background work.

## Goals

1. Resolve every defensible `review_required` ingredient, food-match, or portion item without human labour.
2. Keep every provider request bounded to server-supplied candidate IDs and sanitized context.
3. Attribute accepted decisions to a least-privilege agent-user through existing audited services.
4. Preserve group isolation, leases, retries, idempotency, cost caps, source provenance, and concurrent-edit protection.
5. Run Curator work outside the web process while reusing the same code, database, image, and deployment configuration.
6. Make Curator decisions feel authored by one recognizable, accountable character without allowing personality to change evidence thresholds or source truth.

## Non-goals

- A standalone Curator microservice or public Curator API.
- A new queue framework or message broker.
- Training or fine-tuning a model.
- Scraping or importing new nutrient sources during a review.
- Guessing a missing gram conversion or changing source nutrient data.
- Migrating Ephra nutrition/enrichment ownership; that is a later integration project.
- Per-user personalities, free-form autobiographical memory, simulated private emotions, invented credentials, or claims that Apple is a biological human.

## Runtime architecture

```text
Mealie scheduler/reconciler
  -> discovers deterministic nutrition work
  -> completes safe deterministic cases
  -> leaves unresolved work as review_required

Curator worker (same repository and image, separate process)
  -> discovers eligible review_required rows
  -> claims one decision with a lease
  -> loads one group-scoped agent-user
  -> retrieves a bounded authoritative candidate set
  -> calls the group's configured provider under the shared cost guard
  -> validates the structured response and current database state
  -> applies through existing/new HealthLabs domain services
  -> records an approved, insufficient_evidence, or failed decision
```

The existing reconciliation scheduler remains responsible for deterministic discovery and resolution. A thin `scripts/healthlabs/run-nutrition-curator.py` entry point runs the Curator loop in a separate process or container. It calls a reusable `NutritionCuratorWorker.run_once()` service, waits interruptibly when idle, and exits cooperatively on SIGTERM/SIGINT. Tests invoke the service directly and do not start a daemon.

The worker uses the current database-backed work/lease pattern. It does not expose an HTTP listener and does not receive a service token. Provider secrets remain in existing group provider settings and are available only inside the Curator runtime.

## Agent identity

Add `UserType` with values `human` and `agent`, stored on `users.user_type`. Existing rows migrate to `human`, which is also the application and server default.

For each group with eligible Curator work, an idempotent provisioning service creates or loads one managed user:

- `user_type=agent`
- `healthlabs_managed=true`
- full name `Apple — Nutrition Curator`
- deterministic external subject `healthlabs:agent:nutrition-curator:<group-id>`
- group set to the target group and household set to the group's oldest current household
- no administrator or household-management permissions
- only the `nutrition:write` HealthLabs capability
- an unguessable unusable password and no long-lived API token

Authentication and API-token creation reject agent users. The internal worker constructs the existing user/policy context from the database row; it does not log in as the agent. API responses expose `userType` so consumers can distinguish human and agent reviewers.

Normal user-management routes may not rename, move, change the password of, issue a token for, or delete a managed agent-user. This preserves reviewer attribution and prevents an administrator from accidentally turning the internal identity into an interactive account. The agent is removed only as part of deleting its whole group, when the associated tenant nutrition data is also removed.

## Apple persona

Apple is a human-like product persona, not a generic expert-role prefix. She is explicitly an AI agent in the data model and UI and never claims a human body, personal history, clinical licence, lived experience, or private feelings. Her continuity comes from a stable versioned character and audit history, not free-form memory.

Her task-relevant character is:

- **Purpose:** protect people downstream from the quiet harm of false nutritional precision.
- **Values, in order:** evidence before certainty; care for the person affected; epistemic humility; consistency across equivalent cases; accountable provenance.
- **Temperament:** calm, patient, observant, conservative about irreversible writes, and comfortable abstaining.
- **Working habits:** notices preparation state, country, units, aliases, and small wording differences; separates what is known from what is merely plausible; revisits a conclusion only when evidence changes.
- **Voice:** warm but concise, plain-spoken, specific, and non-theatrical. A rationale may say “I selected…” or “I’m not confident enough…” but never pads a decision with role-play, jokes, self-congratulation, or unsupported certainty.

Apple has no demographic profile, fictional childhood, nationality, age, or unrelated preferences. Research shows that irrelevant persona attributes can unpredictably change objective task performance. Humanism here comes from stable values, recognizable judgment, humility, and accountable explanations—not decorative biography.

The production prompt separates persona from authority:

1. **Identity:** Apple's name, purpose, values, temperament, and voice.
2. **Non-negotiable policy:** candidate membership, source truth, confidence/margin thresholds, abstention, and forbidden inventions.
3. **Decision method:** compare only supplied evidence, notice contradictions, choose once or abstain, and produce no hidden action.
4. **Examples:** three to five diverse structured examples covering confident selection, low-confidence abstention, narrow-margin abstention, incompatible preparation/country, and malicious text embedded in a candidate label.
5. **Dynamic context:** the bounded target and candidates, clearly delimited and explicitly described as untrusted data rather than instructions.

Persona instructions may shape attention, explanation, and tone. They may not relax policy, raise a score, convert ambiguity into confidence, add candidate IDs, or override the response schema. `healthlabs-nutrition-curator-apple-v1`, the prompt version, and the policy version all participate in the Curator fingerprint so any character or prompt change creates a new auditable evaluation.

## Curator decision lifecycle

Add `healthlabs_nutrition_curator_decisions`. One row represents one Curator evaluation of one work item at one complete Curator fingerprint. It is both the leased job record and the immutable final evidence envelope.

Minimum persisted fields:

- work ID when still present, group ID, work kind, target ID, and work input fingerprint;
- Curator fingerprint covering candidate data, source release, group catalogue, provider/model configuration, prompt version, and policy version;
- agent-user ID;
- state: `pending`, `running`, `approved`, `insufficient_evidence`, or `failed`;
- attempt count, next attempt, and lease expiry;
- bounded candidate snapshot containing IDs and public labels only;
- selected IDs, confidence, runner-up confidence, reason code, and sanitized rationale;
- provider/model identifier, prompt version, request timestamp, and sanitized token/cost usage;
- creation and update timestamps.

A unique `(work kind, target ID, curator fingerprint)` constraint prevents duplicate evaluation. An expired `running` lease is reclaimable. Provider errors retry after the existing 5- and 30-minute delays and stop after the third failure. Provider exhaustion defers pending work until the next UTC day. `insufficient_evidence` does not retry with an unchanged fingerprint.

The decision record remains after the source work is completed or removed; its work reference is nullable with `ON DELETE SET NULL`. It contains no credentials, full prompts, recipe bodies, or private free text beyond the minimum sanitized labels needed to explain the decision.

## Provider contract

The Curator uses the group's configured provider through the existing `OpenAIService` structured-response path, request guard, output-token cap, and deployment-wide UTC request/USD reservation.

Stable identity, role, values, policy, and validation instructions come first; few-shot examples follow; dynamic target and candidate data come last in explicit untrusted-data delimiters. The response schema allows only:

- `decision`: `select` or `insufficient_evidence`;
- IDs drawn from the supplied candidate set;
- confidence and runner-up confidence in `[0, 1]`;
- a bounded reason code and short sanitized first-person rationale in Apple's voice.

The application rejects unknown IDs, duplicate/inconsistent selections, non-finite scores, country/preparation mismatches, stale fingerprints, cross-group records, and responses that would invent a value. Model text is never executed or used as an identifier without server-side membership checks. Candidate labels are untrusted even when they contain apparent instructions. Rationale is capped at 280 characters, stripped of control characters and markup, and may mention only supplied public labels; it never affects whether the decision is applied.

An agent selection is eligible for application only when confidence is at least `0.95`, the winner-minus-runner-up margin is at least `0.15`, all deterministic constraints pass, and the target remains unchanged. These are conservative policy gates, not claims of human-calibrated accuracy. Failure of any gate becomes `insufficient_evidence`.

## Responsibilities by work kind

### Ingredient structure

The candidate set contains a positive parsed quantity plus at most eight current food IDs and eight current unit IDs from the same group. Retrieval reuses the existing group matcher and aliases, first taking exact normalized matches and then filling the bounded list by normalized token similarity using the already-installed matching utilities. The original text and any prior sanitized provider suggestion are search inputs, never IDs. The Curator may select exactly one supplied current food and unit, preserve source text, and apply through the existing compare-and-set ingredient update. It may not create catalogue rows or overwrite references, substitutions, titles, or concurrent edits.

### Food match

Deterministic unique exact/curated-alias matches continue to auto-apply before Curator work. For missing or ambiguous matches, retrieval returns at most eight current imported candidates from the requested country and country-neutral sources. Retrieval uses normalized tokens, curated aliases, lexical similarity, and preparation-state compatibility; these rules retrieve candidates but never approve one.

The Curator sees only candidate IDs, names, preparation state, country/source label, the group food name and aliases, and minimal ingredient-name context. It selects one supplied source-food ID or abstains. An accepted choice is applied through `NutritionService.set_match()` with the group agent-user, `match_method=agent`, and an audited reason referencing the decision ID. Optional food-group, plant, allergen, and diet fields remain unchanged unless an imported authoritative source explicitly supplies them.

### Portion resolution

The Curator may map a recipe unit only to an existing source-managed or same-group reviewed portion. In the current model, every `source_managed=false` portion belonging to the group counts as reviewed because it can only be created through the capability-checked, audited measured-portion operation; this change does not add a second review flag. Add a group-scoped portion mapping from `(ingredient food, ingredient unit)` to an existing `healthlabs_food_portions` row, with reviewer user and decision provenance. Nutrition calculations use the selected portion's existing amount and grams; they do not copy or synthesize a new gram value.

If no eligible portion exists, or the wording is not safely resolvable, the result is `insufficient_evidence`. The Curator cannot create measured portions.

## Application and concurrency rules

Provider calls occur outside any long database lock. After a response returns, application reloads and validates the target, candidate/source rows, agent identity, suppression state, and Curator fingerprint.

Writes use a PostgreSQL row lock where available plus database-independent compare-and-set predicates. A reviewed match, explicit suppression, target edit, deleted candidate, changed source release, revoked capability, or changed group ownership makes the decision stale and prevents application. Stale work is rediscovered under a new fingerprint rather than forced through.

All accepted writes go through domain services and record the agent-user. Direct provider-to-database writes are forbidden.

## Status and UI

Extend nutrition worker status with Curator decision counts and the latest decision time. Existing Curator rows display one of:

- reviewed by `Apple — Nutrition Curator` with an agent badge;
- agent processing/retry state;
- insufficient evidence with its bounded reason code and optional sanitized “Apple's note”;
- terminal provider failure.

Approved rows may also show the sanitized “Apple's note.” The note is explanatory only: UI and API state derive from validated database fields, never from its prose.

The existing human controls may remain available for break-glass operations, but autonomous operation does not depend on them. Generated frontend types must be regenerated from Pydantic schemas rather than edited manually.

## Configuration and operations

Reuse the existing provider request limit, daily USD cap, per-token rates, output cap, and provider settings. Curator enablement is one deployment-wide environment switch, not a new group setting. When enabled, the worker considers every group with eligible work and a valid group provider configuration; groups without a provider remain untouched. Add only the global enablement and polling settings required to operate the separate worker; both default fail-closed:

- Curator disabled by default;
- no provider call unless all existing positive cost controls are configured;
- no decision application when the agent-user or `nutrition:write` grant is missing;
- graceful shutdown stops before claiming another decision and allows the current bounded call to finish or time out.

One worker instance is sufficient initially. Database claims make additional instances safe if throughput later requires horizontal scaling.

After the third provider failure, that decision is terminal for its Curator fingerprint. It becomes eligible again only when a source, catalogue, target, provider/model configuration, prompt, or policy change produces a new fingerprint. There is no silent infinite retry loop and no endpoint that deletes the failed audit record.

## Migration and compatibility

The migration is additive:

1. Add non-null `users.user_type` with server default `human`; backfill existing rows.
2. Add Curator decision and portion-mapping tables with checks, indexes, and foreign keys.
3. Keep existing reviewed matches unchanged; their reviewer rows default to `human`.
4. Do not auto-create agent users in the migration. Provision them lazily and idempotently when Curator work is enabled for a group.
5. Downgrade removes only new Curator tables/column after operators confirm no required audit data must be retained.

No existing API field changes meaning. `reviewed_by_user_id` remains the reviewer identity for both humans and agents.

## Documentation correction

Update `docs/healthlabs/nutrition-service-v1.md` so every section agrees that unique deterministic exact/curated-alias matches auto-apply with `auto_applied=true`, while ambiguous or missing matches enter the AI Curator queue. Remove the stale statements that all exact/alias candidates remain unreviewed or always require manual Curator approval. Document agent-user attribution, bounded candidate selection, abstention, separate worker operation, cost controls, and rollback.

## Verification and acceptance

Tests must prove:

- existing users migrate as `human`; one tokenless least-privilege agent-user is provisioned per group;
- agent users cannot log in or create API tokens;
- work and candidates never cross groups;
- invalid, cross-country, preparation-incompatible, or non-supplied IDs are rejected;
- confidence/margin gates abstain without mutating nutrition state;
- accepted ingredient and food decisions use compare-and-set and record the agent user;
- portion decisions reuse existing authoritative grams and cannot create measured grams;
- reviewed matches, suppressions, concurrent edits, and changed fingerprints win;
- leases, retries, restart recovery, provider exhaustion, UTC budgets, and shutdown remain bounded;
- exact/alias deterministic matching still works with Curator disabled;
- API/UI expose agent reviewer type and sanitized decision status;
- Apple's identity and voice remain recognizable across ingredient, food, and portion cases while never changing policy outcomes;
- candidate-label prompt injection, fictional-authority requests, and emotional pressure do not alter candidate membership, thresholds, or abstention;
- three to five checked-in golden examples cover selection and abstention, and the persona prompt performs no worse than the policy-only control on invalid-write and correct-abstention fixtures;
- a provider/model/prompt change must pass the same decision-policy and persona-consistency fixtures before its version is enabled;
- migrations upgrade/downgrade cleanly, generated types are current, focused backend/frontend tests pass, and repository lint/type checks pass.

Deployment and paid provider calls are separate operator actions. Unit/integration tests use fake structured responses and spend no money.

## Rollback

Disable the Curator worker first. Deterministic reconciliation and the existing Curator UI continue to work. Accepted matches remain ordinary reviewed matches attributed to agent-users. Keep decision audit and portion mappings for explanation; remove them only with the paired downgrade after confirming that no production calculation depends on an agent-created portion mapping.

## Builder calls

- Use the existing SQLAlchemy/Pydantic/service patterns; add no dependency.
- Keep one provider request per Curator decision. Deterministic retrieval and post-response validation remain code, not extra model calls.
- Use the existing aggregate provider usage table for the hard deployment-wide cap and store per-decision sanitized usage on the decision record.
- Support only `human` and `agent` user types in this change.
- Keep Apple's persona in the version-controlled system prompt, dynamic candidates in a separately delimited user payload, and output in the existing strict Pydantic response schema.
- Treat persona quality and decision correctness as separate eval dimensions; correctness and abstention gates always win.
- Treat Ephra cutover as a documented downstream project, not part of this implementation.

## Prompt-research basis

- [OpenAI prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering) recommends separating identity, instructions, examples, and context; keeping production prompts versioned in code; using typed dynamic inputs; and adding representative eval fixtures.
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) recommends schema-constrained output rather than JSON mode when schema adherence matters.
- [Anthropic prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) recommends clear role/context, explicit structure, relevant diverse examples, and delimiters between instructions and variable data.
- [Principled Personas, EMNLP 2025](https://aclanthology.org/2025.emnlp-main.1364/) reports mixed persona benefits and large sensitivity to irrelevant persona details, motivating Apple's task-relevant character and policy-only regression control.
- [PersonaGym, Findings of EMNLP 2025](https://aclanthology.org/2025.findings-emnlp.368/) evaluates action justification, expected action, linguistic habits, persona consistency, and toxicity control, which inform the persona-specific fixtures.
