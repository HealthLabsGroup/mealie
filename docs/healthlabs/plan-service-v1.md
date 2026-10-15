# HealthLabs Plan Service v1

Release `v3.28.0-healthlabs.5` adds immutable, versioned meal-plan
publications to the public HealthLabs API. Mealie remains the source of truth
for published operational plans and pinned recipe revisions.

## Deployment identity

- Source tag: `v3.28.0-healthlabs.5`
- Container: `ghcr.io/healthlabsgroup/mealie:v3.28.0-healthlabs.5`
- Platforms: `linux/amd64`, `linux/arm64`
- API prefix: `/api/healthlabs/v1`
- Upstream application version: `3.28.0`

Deploy the immutable digest recorded in the GitHub release. Run the database
migration before enabling plan traffic, and retain the paired database and
data-volume backup until publication reconciliation succeeds.

## Plan routes

```text
GET   /plans/current
PATCH /plans/{plan_id}/entries/{entry_id}                 Idempotency-Key required
GET   /subjects/{external_subject_id}/plans/current       access reason required
POST  /subjects/{external_subject_id}/plan-publications   both headers required
```

Cross-subject calls require `X-HealthLabs-Access-Reason` of at most 500
characters. Mutation idempotency keys are at most 255 characters. Replaying an
identical request returns the original result; reusing a key for different
content returns `409 Conflict`.

Publication accepts only 3-, 7-, or 14-day windows. Recipe entries must pin an
immutable recipe revision. Plain operational ideas require a title and no
recipe identifiers. Leftovers must point to an earlier recipe entry in the
same publication and use the same recipe and revision.

Only the current publication is materialized into native Mealie meal-plan
rows. A new publication supersedes the prior one and replaces only rows linked
to HealthLabs publication entries. Ordinary upstream meal plans are unchanged.

## Capabilities and privacy

| Capability | Effect |
| --- | --- |
| `plans:read_own` | Read the caller's current publication |
| `plans:mark_cooked_own` | Change cooked state on the caller's current entry |
| `plans:write_all` | Publish and read another subject's current plan with an access reason |
| `recipes:read_all` | Read personal recipes across subjects and receive owner subject metadata |

Managed subjects receive only the two own-plan capabilities as new plan
baselines. Nutrition plan editors need explicit, revocable `plans:write_all`
and `recipes:read_all` grants. Administrative status alone grants no plan or
recipe content access.

Publications and their entries are immutable. Cooked state is stored
separately with optimistic integer versions and never records a meal or changes
the pinned recipe. Cross-subject plan reads and writes create immutable audit
events. The API stores no clinical rationale, screening answers, diagnoses,
goals, exclusions, email addresses, or private application data.

Managed tokens remain denied from upstream recipe, household meal-plan, group,
token, and administrator routes.

## Smoke test

Run the smoke test only against a disposable environment created from the
exact release digest. It provisions a random managed subject, publishes a
plain operational entry with a preconfigured editor token, verifies exact
replay and both read paths, marks the entry cooked, and removes the disposable
workspace. Immutable publication and audit rows intentionally remain until the
disposable database is destroyed.

```bash
HEALTHLABS_BASE_URL=https://recipes.example.com \
HEALTHLABS_PROVISIONING_SECRET='replace-with-secret' \
HEALTHLABS_PLAN_EDITOR_TOKEN='replace-with-editor-token' \
./scripts/healthlabs/smoke-plan-service.sh
```

The editor token must have active `plans:write_all` and `recipes:read_all`
grants. The script never uses an existing subject or recipe.

## Rollback

Stop plan traffic before rolling back the application. Restore the paired
pre-migration database and data-volume backup together; do not run an older
binary against the migrated schema. Compare publication content hashes before
accepting further writes after any ambiguous rollback or timeout.
