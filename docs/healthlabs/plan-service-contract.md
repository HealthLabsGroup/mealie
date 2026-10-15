# HealthLabs plan service starting contract

This document pins the meal-plan boundary before HealthLabs adds immutable
published plans.

## Release line

- Base release: `v3.28.0-healthlabs.4`
- Base commit: `9836b45df47754abf476d128ba1363011a3a1e1d`
- Next release: `v3.28.0-healthlabs.5`

## Existing native plan

Mealie stores the current household plan in `group_meal_plans` and exposes it
through `/api/households/mealplans`. A native entry contains:

- `date`, `entryType`, `title`, and `text`;
- an optional `recipeId` and expanded recipe;
- `recipeScale`, which defaults to `1` and must stay positive;
- Mealie group, household, and user ownership.

HealthLabs publication will reuse those rows only as the current materialized
view. Immutable publication history, pinned recipe revisions, and cooked state
will use separate HealthLabs tables.

## Existing Ephra patient facade

Ephra Web calls only these private routes:

```text
GET   /v1/plan?from=&days=
PUT   /v1/plan
PATCH /v1/plan/items/{item_id}
```

The patient response keeps `from`, `days`, `people`, `version`, `items`, and
`someday`. Each item keeps its stable Ephra ID, date/someday slot, recipe or
idea, name snapshot, label, leftover parent, servings, position, cooked
timestamp, optimistic version, and removed-recipe state.

Phase B may add publication metadata. It must not rename or remove these read
fields. The patient structural `PUT` is intentionally disabled only after
migration verifies a Mealie publication for existing plans.

## Boundary

Managed HealthLabs tokens remain denied from upstream household meal-plan
routes. Ephra communicates only with documented `/api/healthlabs/v1` JSON
routes and receives no Mealie group, household, credential, or database
details.
