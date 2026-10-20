# HealthLabs nutrition completion backlog

Date: 2026-10-20
Status: Active (Tasks 1–3 complete)

The core nutrition service is implemented and running on DEV. These tasks finish
the existing reconciliation backlog without weakening evidence, tenant, audit,
or source-provenance boundaries. Execute them in order: 1–2, then 3, then 4–5.

## Task 1 — Recover provider failures without losing audit history

- [x] Treat rationale as optional display metadata: sanitize it, but never let
  missing first-person voice reject an otherwise valid bounded decision.
- [x] Continue hiding a rationale from public responses unless it is safe,
  concise, and in Apple's first-person voice.
- [x] Preserve each terminal failed decision unchanged.
- [x] Give decisions from an older response contract exactly one new audited
  attempt under the current response contract.
- [x] Confirm stale budget-deferred decisions do not block the current
  fingerprint; retain them as historical evidence.
- [x] Add unit and integration coverage for metadata tolerance, one recovery
  cycle, immutable history, and no endless retry loop.
- [x] Deploy and prove the live terminal-failure count moves without changing
  reviewed nutrition records directly.

Completed in `v3.28.0-healthlabs.31`. The live recovery run preserved all 14
old terminal portion failures and created 14 separate `response-v2` decisions:
10 `insufficient_candidates` and 4 `invalid_selection`. No new rationale
validation failure was logged.

Acceptance: old failures can be reconsidered once after a contract fix, every
attempt has a distinct immutable decision, and future failures under the current
contract still stop after the normal retry policy.

## Task 2 — Verify safe reuse of identical approved decisions

- [x] Verify reuse requires the same workspace, work input, candidate snapshot,
  catalogue, source releases, provider/model, persona, prompt, and policy.
- [x] Verify only approved decisions seed reuse; pending, failed, conflicting,
  legacy, and insufficient-evidence decisions never do.
- [x] Verify every reused target receives its own immutable decision linked to
  the source decision and records zero provider usage.
- [x] Measure live eligible approved duplicate signatures before changing reuse
  logic; zero reuse is correct when there is no eligible source/target pair.
- [x] Keep conflict handling fail-closed as `reuse_conflict`.

Completed by focused reuse tests and a live eligibility audit. Live DEV had
39 approved decisions, zero eligible approved duplicate signatures, and zero
reuse audit rows, so no provider call was incorrectly replaced by reuse.

Acceptance: focused tests pass and the live audit distinguishes “reuse works but
has no eligible duplicate” from “an eligible duplicate caused a provider call.”

## Task 3 — Drain the queue with bounded asynchronous concurrency

- [x] Replace the single sequential provider loop with an async supervisor and
  configurable bounded concurrency; target the maximum provider-safe throughput.
- [x] Give every concurrent job its own database session; never share a
  SQLAlchemy session across tasks.
- [x] Claim work atomically, preserve 15-minute leases, and prevent duplicate
  claims across processes with database locking/CAS semantics.
- [x] Keep request and cost reservations atomic across all concurrent jobs;
  enforce provider rate-limit backoff and bounded retries without a retry storm.
- [x] Preserve per-target current-fingerprint checks, approved-decision reuse,
  tenant isolation, and immutable decision evidence.
- [x] Stop accepting new work on SIGTERM, allow bounded in-flight calls to
  finalize, then exit cleanly.
- [x] Add concurrency tests for duplicate claims, lease recovery, stale writes,
  shared budget, shutdown, provider throttling, and mixed work kinds.
- [x] Load-test with real DEV latency, tune concurrency to available capacity,
  and measure whether the remaining queue can finish in minutes; report the
  provider-capacity limit if the measured throughput cannot meet that target.

Completed in `v3.28.0-healthlabs.34`. The runner uses one database session per
job and one shared HTTP pool, caches revision-keyed catalogue snapshots, ramps
32 → 64 → 128 → 256 → 512, and holds the current level after the first 429
without reducing it. Sustained transport failures fall back only to the last
stable level. Live DEV reached the provider throttle boundary at concurrency
512 with zero transport errors, restarts, or OOM events; memory settled near
253 MiB. The final pre-cap window admitted 760 requests in 25 seconds (about
1,824/minute), then stopped exactly at the configured 25,000 daily requests.
The local CPU/connection bottleneck is removed; provider throttling and the
daily request cap now determine completion time, so the remaining queue cannot
be promised to finish in minutes under the current allowance.

Acceptance: one deployment processes many independent provider calls
concurrently, no target is applied twice, and measured throughput—not an
assumption—determines the completion ETA.

## Task 4 — Expand authoritative source and portion coverage

- [x] Export aggregate, non-private gap statistics by country, normalized food,
  preparation state, unit, and reason code.
- [x] Prioritize missing portion evidence, then food matches, then ingredient
  aliases based on affected recipe count.
- [ ] Research national/open datasets and verify licence, attribution, version,
  provenance, nutrient definitions, edible portions, and redistribution terms.
- [ ] Keep raw third-party datasets out of GitHub; commit only importer code,
  manifests, checksums, licence metadata, and retrieval instructions.
- [ ] Import repeatably into the existing source/portion boundary without
  replacing reviewed records or mixing Open Food Facts into generic sources.
- [ ] Re-run deterministic matching before paid curation and quantify how many
  `insufficient_evidence` records become resolvable.

The first slice shipped in `v3.28.0-healthlabs.35` by extending the existing
coverage report; no second exporter or data format was added. A read-only DEV
snapshot produced 6,017 aggregate gap groups covering 22,611 targets: 1,826
portion resolutions, 188 food matches, and 20,597 ingredient structures. The
report was summarized in memory and not saved or committed; raw source rows,
recipe text, target IDs, workspace/user IDs, prompts, and provider responses
remain excluded.

Acceptance: every new fact is source-backed and reproducible, and coverage
improves without invented nutrients, grams, or cross-workspace data.

## Task 5 — Make provider reservation accounting exact and recoverable

- [ ] Replace anonymous daily aggregate reservations with uniquely identifiable,
  idempotent per-request reservations or an equivalent auditable ledger.
- [ ] Settle actual token usage exactly once after a response.
- [ ] Release a reservation when no provider request was sent; preserve a safe
  charge when transmission outcome is genuinely unknown.
- [ ] Reconcile expired reservations after crashes without double release or
  exceeding the daily request/cost limits.
- [ ] Cover concurrent reserve/settle/release, crash recovery, invalid usage,
  UTC rollover, SQLite, and PostgreSQL.
- [ ] Expose only aggregate sanitized usage; never expose prompts, credentials,
  private recipe text, or provider response bodies.

Acceptance: `reserved + spent` remains conservative and explainable under
success, validation failure, rejection, concurrency, restart, and UTC rollover.
