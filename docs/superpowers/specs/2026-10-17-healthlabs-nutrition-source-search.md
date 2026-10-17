# HealthLabs Nutrition Source Search

Date: 2026-10-17
Status: Approved for native implementation

## Problem

Mealie stores 13,874 checksum-pinned authoritative source foods and already
supports audited `nutrition:write` match mutations, but the running API cannot
search those source foods. Editors therefore cannot discover a
`nutrientFoodId` without querying the database or running a repository script.
On Mac mini DEV this leaves all 2,736 recipes nutritionally incomplete: 303 of
315 Mealie foods are still unknown and no recipe has every ingredient matched.

## Decision

Add an authenticated, read-only endpoint:

```text
GET /api/healthlabs/v1/nutrition/source-foods?q=&country=&limit=
```

The endpoint searches the pinned generic food catalogue by all normalized
query tokens, restricts results to the requested country plus `OTHER`, ranks
the requested country first, and returns at most 50 stable results. Each result
contains the source-food ID needed by the existing audited match endpoint,
canonical nutrients per 100 g, source-backed portions, and full provenance.

Source-food facts are public licensed data, so any authenticated Mealie user
may search them. Accepting, replacing, or clearing a match remains restricted
to the independently granted `nutrition:write` capability, requires an access
reason and idempotency key, and remains audited.

## Safety boundaries

- Search never creates or approves a match.
- Fuzzy similarity never auto-selects a result; every non-exact choice is made
  by a nutrition editor through the existing match mutation.
- Searches never cross into unapproved countries: only the requested country
  and country-neutral `OTHER` records are returned.
- Missing nutrients remain `null`; values are returned exactly from the pinned
  source release.
- No schema migration or new dependency is required.

## Acceptance criteria

- Multi-token queries such as `olive oil` find names containing both tokens in
  any order.
- Requested-country results sort before `OTHER`; unrelated-country records are
  absent.
- Results include stable source-food identity, nutrients, portions, and source
  provenance and can be passed unchanged to the existing match endpoint.
- Blank-after-normalization searches return no results and limits above 50 are
  rejected.
- Existing nutrition read/write permissions and calculations continue to pass.

## Non-goals

- An editor UI in this increment.
- Automatic or fuzzy approval.
- Nutrient target or clinical interpretation changes.
- Backfilling recipe ingredient structure.
