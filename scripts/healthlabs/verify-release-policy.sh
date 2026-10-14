#!/usr/bin/env bash
set -euo pipefail

repository_root=${1:-$(git rev-parse --show-toplevel)}
publish_workflow=$repository_root/.github/workflows/publish.yml
release_workflow=$repository_root/.github/workflows/release.yml

grep -Fq 'ghcr.io/healthlabsgroup/mealie' "$publish_workflow"
grep -Fq 'digest:' "$publish_workflow"
grep -Fq 'v*-healthlabs.*' "$release_workflow"
grep -Fq 'merge-base --is-ancestor' "$release_workflow"
grep -Fq 'validate-release-tag.sh' "$release_workflow"

for forbidden in 'hkotel/mealie' 'registry: docker.io' 'DOCKERHUB_TOKEN' 'DOCKERHUB_USERNAME' 'depot/' ':latest'; do
  if grep -Fq -- "$forbidden" "$publish_workflow" "$release_workflow"; then
    printf 'forbidden release publisher content: %s\n' "$forbidden" >&2
    exit 1
  fi
done
