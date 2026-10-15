# HealthLabs Mealie Distribution

This public repository is the HealthLabs maintained fork of
[`mealie-recipes/mealie`](https://github.com/mealie-recipes/mealie). It retains
the upstream history, license, notices, and attribution. This distribution has
been modified by HealthLabs beginning 2026-10-14.

## Branches and upstream synchronization

`mealie-next` is the protected default integration branch. HealthLabs work is
reviewed on focused branches. To synchronize upstream, create a branch from the
current HealthLabs `mealie-next`, merge `upstream/mealie-next` into it, resolve
conflicts there, run every required check, and merge it by pull request.
Upstream updates are never deployed automatically.

Classify each change before merge:

1. Send generally useful bug, security, performance, migration, and API fixes upstream.
2. Keep HealthLabs operational hardening here when it is outside upstream scope.
3. Keep Ephra product and clinical behavior in Ephra unless Mealie needs a generally useful API capability.

## Releases

Release tags use `v<upstream-version>-healthlabs.<revision>`. The matching
container is `ghcr.io/healthlabsgroup/mealie:<same-tag>`. Deployments pin the
published digest. This fork does not publish or deploy a floating `latest` tag.

Each GitHub release records the source commit, image digest, upstream version,
HealthLabs changes, and rollback instructions. Schema-changing releases also
record the tested database backup and restore procedure.

The first recipe-service API release is
[`v3.28.0-healthlabs.3`](docs/healthlabs/recipe-service-v1.md). Its image is
published for `linux/amd64` and `linux/arm64`; consumers must deploy the digest
recorded in the GitHub release rather than the mutable tag reference.

The first immutable plan-service release is
[`v3.28.0-healthlabs.5`](docs/healthlabs/plan-service-v1.md). It adds
capability-scoped publication, current-plan reads, and cooked-state updates on
the existing HealthLabs API boundary.

## AGPL-3.0 and source availability

The upstream AGPL-3.0 license, copyright notices, and attribution remain in
place. Every modified deployed build links to the exact public source commit.
The public source and workflows contain everything needed to build the image;
credentials, customer data, and private deployment configuration are excluded.

## Quality gates

Pull requests run backend tests against SQLite and PostgreSQL, frontend tests,
end-to-end tests, linting, type checking, package builds, container scanning,
CodeQL, secret scanning, and fork-policy checks. A failure blocks merging.
Release tags are accepted only from protected `mealie-next` commits after the
same release gates pass.

## Ephra integration boundary

Mealie owns recipes, imports, recipe images, recipe organization, and recipe
search. Ephra owns meal logs, normalized nutrition, planning, shopping,
clinical safeguards, evidence, habits, and care workflows. The systems use
documented HTTP/JSON APIs, separate processes, and separate databases.

The recipe-service interface is additive under `/api/healthlabs/v1`. It does
not expose Mealie group or household identifiers in recipe responses, and it
keeps consumer source and runtime state outside this repository. Consumers are
responsible for their own AGPL compliance assessment.

## Security reports

Report vulnerabilities privately through this repository's Security tab.
Do not open a public issue containing exploit details, credentials, or user data.
