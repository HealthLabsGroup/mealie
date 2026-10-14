#!/usr/bin/env bash
set -euo pipefail

repository_root=${1:-$(git rev-parse --show-toplevel)}
publish_workflow=$repository_root/.github/workflows/publish.yml
pull_request_workflow=$repository_root/.github/workflows/pull-requests.yml
release_workflow=$repository_root/.github/workflows/release.yml
trivy_workflow=$repository_root/.github/workflows/partial-trivy-container-scanning.yml
dockerfile=$repository_root/docker/Dockerfile
active_workflows_dir=$(mktemp -d)
active_publish_workflow=$active_workflows_dir/publish.yml
active_pull_request_workflow=$active_workflows_dir/pull-requests.yml
active_release_workflow=$active_workflows_dir/release.yml
active_trivy_workflow=$active_workflows_dir/partial-trivy-container-scanning.yml
cleanup() {
  rm -rf -- "$active_workflows_dir"
}
trap cleanup EXIT

strip_yaml_comments() {
  sed -E \
    -e '/^[[:space:]]*#/d' \
    -e 's/[[:space:]]+#.*$//' \
    "$1" > "$2"
}

strip_yaml_comments "$publish_workflow" "$active_publish_workflow"
strip_yaml_comments "$pull_request_workflow" "$active_pull_request_workflow"
strip_yaml_comments "$release_workflow" "$active_release_workflow"
strip_yaml_comments "$trivy_workflow" "$active_trivy_workflow"

grep -Fq 'ghcr.io/healthlabsgroup/mealie' "$active_publish_workflow"
grep -Fq 'digest:' "$active_publish_workflow"
grep -Fq 'push-by-digest=true' "$active_publish_workflow"
# GitHub expression must remain literal.
# shellcheck disable=SC2016
grep -Fq 'TRIVY_PLATFORM: ${{ matrix.platform }}' "$active_publish_workflow"
grep -Fq "hashFiles(format('trivy-{0}.sarif', matrix.suffix)) != ''" "$active_publish_workflow"
# GitHub expression must remain literal.
# shellcheck disable=SC2016
grep -Fq 'ghcr.io/healthlabsgroup/mealie@${{ steps.build.outputs.digest }}' "$active_publish_workflow"
grep -Fq 'docker buildx imagetools create' "$active_publish_workflow"
grep -Fq 'docker buildx imagetools inspect' "$active_publish_workflow"
grep -Fq 'sigstore/cosign-installer@v4.1.2' "$active_publish_workflow"
# Workflow shell variables must remain literal.
# shellcheck disable=SC2016
grep -Fq 'cosign sign --yes "$REGISTRY_IMAGE@$IMAGE_DIGEST"' "$active_publish_workflow"
grep -Fq 'actions/attest-build-provenance@v4.2.2' "$active_publish_workflow"
grep -Fq 'push-to-registry: true' "$active_publish_workflow"
grep -Fq 'attestations: write' "$active_publish_workflow" "$active_release_workflow"
grep -Fq 'id-token: write' "$active_publish_workflow" "$active_release_workflow"
grep -Fq 'v*-healthlabs.*' "$active_release_workflow"
# Workflow shell variables must remain literal.
# shellcheck disable=SC2016
grep -Fq 'test "$GITHUB_SHA" = "$branch_head"' "$active_release_workflow"
grep -Fq 'Protect HealthLabs release tags' "$active_release_workflow"
# Workflow shell variables must remain literal.
# shellcheck disable=SC2016
grep -Fq 'gh release view "$GITHUB_REF_NAME"' "$active_release_workflow"
grep -Fq 'validate-release-tag.sh' "$active_release_workflow"
grep -Fq 'ACTIONLINT_VERSION: 1.7.12' "$active_pull_request_workflow"
grep -Fq 'ACTIONLINT_VERSION: 1.7.12' "$active_release_workflow"
grep -Fq 'HEALTHLABS_FORK_BASE_SHA: ce0be528e7cfce4bf1fa846c3c136186dd719af5' "$active_pull_request_workflow"
grep -Fq 'HEALTHLABS_FORK_BASE_SHA: ce0be528e7cfce4bf1fa846c3c136186dd719af5' "$active_release_workflow"
grep -Fq 'gitleaks git --redact --verbose' "$active_pull_request_workflow"
grep -Fq 'gitleaks git --redact --verbose' "$active_release_workflow"
grep -Fq -- "--log-opts=\"\${HEALTHLABS_FORK_BASE_SHA}..HEAD\"" "$active_pull_request_workflow"
grep -Fq -- "--log-opts=\"\${HEALTHLABS_FORK_BASE_SHA}..HEAD\"" "$active_release_workflow"
grep -Fq 'scanners: "vuln"' "$active_trivy_workflow"
grep -Fq 'apt-get upgrade --no-install-recommends -y' "$dockerfile"
grep -Fq '/usr/local/bin/python -m pip uninstall --yes pip' "$dockerfile"
grep -Fq "\$VENV_PATH/bin/python -m pip uninstall --yes pip" "$dockerfile"

for forbidden in 'hkotel/mealie' 'registry: docker.io' 'DOCKERHUB_TOKEN' 'DOCKERHUB_USERNAME' 'depot/' ':latest' 'gitleaks/gitleaks-action'; do
  if grep -Fq -- "$forbidden" "$active_publish_workflow" "$active_pull_request_workflow" "$active_release_workflow"; then
    printf 'forbidden release publisher content: %s\n' "$forbidden" >&2
    exit 1
  fi
done
