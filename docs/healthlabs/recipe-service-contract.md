# HealthLabs recipe service starting contract

This document pins the starting boundary for the HealthLabs recipe service work.
It records the upstream Mealie base and the existing consumer-facing recipe
facade before storage moves to Mealie.

## Release line

- Base release: `v3.28.0-healthlabs.2`
- Base commit: `e8306de5368f571ea52fd9a271e9f9e8dbf2c7bd`
- Next release: `v3.28.0-healthlabs.3`

## Clean baseline

The feature worktree started clean on branch
`healthlabs/recipe-service-v1`. The existing upstream recipe suite is the
baseline gate:

```console
task py:test -- tests/integration_tests/user_recipe_tests -q
```

The command passes at the base commit. The matching private API baseline uses
its existing recipe API and service suites; the browser client baseline uses
its existing Nutrition service suite.

## Existing facade routes to preserve

The private consumer-facing facade keeps these paths and response behavior
while its provider changes:

```text
GET    /v1/recipes
GET    /v1/recipes/for-today
POST   /v1/recipes
GET    /v1/recipes/{recipe_id}
PUT    /v1/recipes/{recipe_id}
DELETE /v1/recipes/{recipe_id}
POST   /v1/recipes/{recipe_id}/copy
PATCH  /v1/recipes/{recipe_id}/seen
GET    /v1/recipes/{recipe_id}/versions/{from}..{to}/diff
POST   /v1/recipes/{recipe_id}/photo
GET    /v1/recipes/{recipe_id}/photo
DELETE /v1/recipes/{recipe_id}/photo
POST   /v1/recipe-imports
GET    /v1/recipe-imports/{import_id}
```

Recipe JSON continues to expose stable consumer IDs and the established
`source`, `ingredients`, `version`, `copied_from`, `figures_pending`, `photo`,
and `situations` fields. Provider URLs, credentials, group IDs, household IDs,
and provider-native IDs are not part of this contract.

## Service boundary

The HealthLabs service extension is exposed only through documented HTTP/JSON
routes. Consumer services do not import Mealie models, schemas, generated
clients, migrations, or database tables. Mealie remains independently
deployable with its own database, credentials, backups, image, and source
release.
