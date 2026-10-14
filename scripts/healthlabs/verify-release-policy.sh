#!/usr/bin/env bash
set -euo pipefail

repository_root=${1:-$(git rev-parse --show-toplevel)}
publish_workflow=$repository_root/.github/workflows/publish.yml
pull_request_workflow=$repository_root/.github/workflows/pull-requests.yml
release_workflow=$repository_root/.github/workflows/release.yml
trivy_workflow=$repository_root/.github/workflows/partial-trivy-container-scanning.yml
dockerfile=$repository_root/docker/Dockerfile

grep -Fq 'ghcr.io/healthlabsgroup/mealie' "$publish_workflow"
grep -Fq 'digest:' "$publish_workflow"
grep -Fq 'push-by-digest=true' "$publish_workflow"
# GitHub expression must remain literal.
# shellcheck disable=SC2016
grep -Fq 'TRIVY_PLATFORM: ${{ matrix.platform }}' "$publish_workflow"
grep -Fq "hashFiles(format('trivy-{0}.sarif', matrix.suffix)) != ''" "$publish_workflow"
# GitHub expression must remain literal.
# shellcheck disable=SC2016
grep -Fq 'ghcr.io/healthlabsgroup/mealie@${{ steps.build.outputs.digest }}' "$publish_workflow"
grep -Fq 'docker buildx imagetools create' "$publish_workflow"
grep -Fq 'docker buildx imagetools inspect' "$publish_workflow"
grep -Fq 'v*-healthlabs.*' "$release_workflow"
# Workflow shell variables must remain literal.
# shellcheck disable=SC2016
grep -Fq 'test "$GITHUB_SHA" = "$branch_head"' "$release_workflow"
grep -Fq 'Protect HealthLabs release tags' "$release_workflow"
# Workflow shell variables must remain literal.
# shellcheck disable=SC2016
grep -Fq 'gh release view "$GITHUB_REF_NAME"' "$release_workflow"
grep -Fq 'validate-release-tag.sh' "$release_workflow"
grep -Fq 'ACTIONLINT_VERSION: 1.7.12' "$pull_request_workflow"
grep -Fq 'ACTIONLINT_VERSION: 1.7.12' "$release_workflow"
grep -Fq 'HEALTHLABS_FORK_BASE_SHA: ce0be528e7cfce4bf1fa846c3c136186dd719af5' "$pull_request_workflow"
grep -Fq 'HEALTHLABS_FORK_BASE_SHA: ce0be528e7cfce4bf1fa846c3c136186dd719af5' "$release_workflow"
grep -Fq 'gitleaks git --redact --verbose' "$pull_request_workflow"
grep -Fq 'gitleaks git --redact --verbose' "$release_workflow"
grep -Fq -- "--log-opts=\"\${HEALTHLABS_FORK_BASE_SHA}..HEAD\"" "$pull_request_workflow"
grep -Fq -- "--log-opts=\"\${HEALTHLABS_FORK_BASE_SHA}..HEAD\"" "$release_workflow"
grep -Fq 'scanners: "vuln"' "$trivy_workflow"
grep -Fq 'apt-get upgrade --no-install-recommends -y' "$dockerfile"
grep -Fq '/usr/local/bin/python -m pip uninstall --yes pip' "$dockerfile"
grep -Fq "\$VENV_PATH/bin/python -m pip uninstall --yes pip" "$dockerfile"

for forbidden in 'hkotel/mealie' 'registry: docker.io' 'DOCKERHUB_TOKEN' 'DOCKERHUB_USERNAME' 'depot/' ':latest' 'gitleaks/gitleaks-action'; do
  if grep -Fq -- "$forbidden" "$publish_workflow" "$pull_request_workflow" "$release_workflow"; then
    printf 'forbidden release publisher content: %s\n' "$forbidden" >&2
    exit 1
  fi
done
