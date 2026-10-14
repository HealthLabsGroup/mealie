# HealthLabs Recipe Service v1

Release `v3.28.0-healthlabs.3` adds a stable HTTP/JSON boundary for clients
that need Mealie's native recipes, ingredients, foods, units, imports, images,
and search without sharing a database or copying product-specific code into
this AGPL service.

## Deployment identity

- Source tag: `v3.28.0-healthlabs.3`
- Container: `ghcr.io/healthlabsgroup/mealie:v3.28.0-healthlabs.3`
- Platforms: `linux/amd64`, `linux/arm64`
- API prefix: `/api/healthlabs/v1`
- Upstream application version: `3.28.0`

Deploy the immutable digest recorded in the GitHub release. The tag is a
discovery name, not a deployment lock.

## Authentication and provisioning

Provisioning routes require `X-HealthLabs-Provisioning-Secret`. Set
`HEALTHLABS_PROVISIONING_SECRET` to a dedicated high-entropy secret and expose
these routes only to the trusted integration network.

```text
POST   /subjects
POST   /subjects/{external_subject_id}/tokens/rotate
DELETE /subjects/{external_subject_id}
POST   /catalogue
```

Provisioning is idempotent by external subject. It creates an isolated native
Mealie user, group, household, long-lived bearer token, and the minimum
capability grants. Token rotation invalidates prior tokens. Deprovisioning
removes the managed workspace.

Every other route uses `Authorization: Bearer <token>`. Mutation routes marked
below require a caller-generated `Idempotency-Key`; replaying the same request
returns the stored response, while reusing a key for different content returns
`409 Conflict`.

## Recipe, revision, import, and image routes

```text
GET    /recipes
POST   /recipes                                      Idempotency-Key required
GET    /recipes/{recipe_id}
PUT    /recipes/{recipe_id}                          Idempotency-Key required
DELETE /recipes/{recipe_id}                          Idempotency-Key required
GET    /recipes/{recipe_id}/revisions/{revision_id}
POST   /recipes/{recipe_id}/image                    Idempotency-Key required
GET    /recipes/{recipe_id}/image
DELETE /recipes/{recipe_id}/image
POST   /import-drafts                                Idempotency-Key required
GET    /import-drafts/{draft_id}
GET    /operations/{idempotency_key}
```

Recipes use native Mealie food, unit, ingredient, instruction, image, and
organizer storage. Every successful content mutation creates an immutable
revision. A personal recipe can be edited only by its owner. A catalogue recipe
can be edited only by a catalogue editor. Copy provenance is immutable.

Import drafts are owner-scoped, expire after 24 hours, and create no recipe,
revision, image, organizer, food, or unit row before an explicit recipe save.
URL fetches allow only public HTTP(S) HTML or JSON, enforce DNS/IP validation on
every hop, stop after five redirects, cap the response at 2 MiB, and time out
after 20 seconds. Recipe images accept verified JPEG, PNG, or WebP up to 10 MiB
and 40 million pixels; reads return authenticated WebP bytes without a file
path.

## Capabilities

| Capability | Effect |
| --- | --- |
| `recipes:read_own` | Read personal recipes owned by the subject |
| `recipes:write_own` | Create, update, and delete owned personal recipes |
| `catalogue:read` | Read catalogue recipes and native reference data |
| `catalogue:write` | Create and edit catalogue recipes |
| `recipes:read_all` | Nutritionist read-all access with `X-HealthLabs-Access-Reason` |

Administrative status alone grants no recipe-wide content access. Break-glass
access requires an explicit reason and emits an immutable audit event.

## Integration and AGPL boundary

Ephra is one client of this public API. It remains a separate application,
process, and database and stores only provider identifiers, revision metadata,
normalized nutrition, planning, and clinical data. Recipe authoring flags use
the public Mealie extras key `healthlabs.ephra`; nutrition truth never does.
Users may edit their personal recipes, but Ephra-authored catalogue recipes are
read-only to ordinary users.

The public fork intentionally permits the product name `Ephra` in this public
contract, `HEALTHLABS.md`, the pull-request source-boundary checklist,
integration documentation, and tests of the `healthlabs.ephra` extras key. It
does not permit EphyrTech source, private schemas, customer data, real user
email addresses, private hostnames, deployment credentials, or copied
proprietary code.

## Smoke test

The smoke test creates a disposable isolated subject and recipe, verifies a
revision, search, and idempotency record, then removes both resources:

```bash
HEALTHLABS_BASE_URL=https://recipes.example.com \
HEALTHLABS_PROVISIONING_SECRET='replace-with-secret' \
./scripts/healthlabs/smoke-recipe-service.sh
```

Run it against the exact digest before promotion. The script never uses an
existing user or catalogue workspace.
