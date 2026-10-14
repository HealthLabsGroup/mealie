#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd "$script_dir/../.." && pwd)

expected=$'upstream-version=3.28.0\npackage-version=3.28.0+healthlabs.1'
actual=$("$script_dir/validate-release-tag.sh" v3.28.0-healthlabs.1)
test "$actual" = "$expected"

for invalid_tag in \
  3.28.0-healthlabs.1 \
  v03.28.0-healthlabs.1 \
  v3.028.0-healthlabs.1 \
  v3.28.00-healthlabs.1 \
  v3.28.0-healthlabs.01 \
  v3.28-healthlabs.1 \
  v3.28.0-healthlabs.0 \
  v3.28.0-healthlabs.latest \
  v3.28.0; do
  if "$script_dir/validate-release-tag.sh" "$invalid_tag" >/dev/null 2>&1; then
    printf 'accepted invalid tag: %s\n' "$invalid_tag" >&2
    exit 1
  fi
done

"$script_dir/verify-release-policy.sh" "$repository_root"

fixture_root=$(mktemp -d)
cleanup() {
  if [[ -n ${fixture_root:-} && -d $fixture_root ]]; then
    rm -rf -- "$fixture_root"
  fi
}
trap cleanup EXIT

mkdir -p "$fixture_root/.github/workflows" "$fixture_root/docker"
cat > "$fixture_root/.github/workflows/package-visibility.yml" <<'YAML'
name: HealthLabs Package Visibility
on:
  workflow_call:
  push:
    branches:
      - mealie-next
permissions:
  contents: read
  packages: write
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh api --method PATCH \
            /orgs/HealthLabsGroup/packages/container/mealie \
            -f visibility=public
YAML
cp "$repository_root/.github/workflows/publish.yml" "$fixture_root/.github/workflows/publish.yml"
cp "$repository_root/.github/workflows/pull-requests.yml" "$fixture_root/.github/workflows/pull-requests.yml"
cp "$repository_root/.github/workflows/release.yml" "$fixture_root/.github/workflows/release.yml"
cp "$repository_root/.github/workflows/partial-trivy-container-scanning.yml" "$fixture_root/.github/workflows/partial-trivy-container-scanning.yml"
cp "$repository_root/docker/Dockerfile" "$fixture_root/docker/Dockerfile"
"$script_dir/verify-release-policy.sh" "$fixture_root"

sed -E \
  -e '/visibility=public/s/^([[:space:]]*)/\1# /' \
  "$fixture_root/.github/workflows/package-visibility.yml" \
  > "$fixture_root/.github/workflows/package-visibility.yml.commented"
mv "$fixture_root/.github/workflows/package-visibility.yml.commented" "$fixture_root/.github/workflows/package-visibility.yml"
if "$script_dir/verify-release-policy.sh" "$fixture_root" >/dev/null 2>&1; then
  printf 'accepted a package workflow that does not make GHCR public\n' >&2
  exit 1
fi

cp "$repository_root/.github/workflows/package-visibility.yml" \
  "$fixture_root/.github/workflows/package-visibility.yml"

sed -E \
  -e 's/needs: \[validate, publish, package-visibility\]/needs: [validate, publish]/' \
  "$fixture_root/.github/workflows/release.yml" \
  > "$fixture_root/.github/workflows/release.yml.unordered"
mv "$fixture_root/.github/workflows/release.yml.unordered" "$fixture_root/.github/workflows/release.yml"
if "$script_dir/verify-release-policy.sh" "$fixture_root" >/dev/null 2>&1; then
  printf 'accepted a release that can publish notes before public visibility\n' >&2
  exit 1
fi
cp "$repository_root/.github/workflows/release.yml" \
  "$fixture_root/.github/workflows/release.yml"

sed -E \
  -e '/workflow_call:/a\
  workflow_dispatch:' \
  "$fixture_root/.github/workflows/package-visibility.yml" \
  > "$fixture_root/.github/workflows/package-visibility.yml.dispatchable"
mv "$fixture_root/.github/workflows/package-visibility.yml.dispatchable" "$fixture_root/.github/workflows/package-visibility.yml"
if "$script_dir/verify-release-policy.sh" "$fixture_root" >/dev/null 2>&1; then
  printf 'accepted a branch-dispatchable package-write workflow\n' >&2
  exit 1
fi
cp "$repository_root/.github/workflows/package-visibility.yml" \
  "$fixture_root/.github/workflows/package-visibility.yml"

sed -E \
  -e '/TRIVY_PLATFORM:/s/^([[:space:]]*)/\1# /' \
  -e '/hashFiles\(format/s/^([[:space:]]*)/\1# /' \
  "$repository_root/.github/workflows/publish.yml" \
  > "$fixture_root/.github/workflows/publish.yml.commented"
mv "$fixture_root/.github/workflows/publish.yml.commented" "$fixture_root/.github/workflows/publish.yml"
if "$script_dir/verify-release-policy.sh" "$fixture_root" >/dev/null 2>&1; then
  printf 'accepted a workflow fixture with required release controls commented out\n' >&2
  exit 1
fi

cp "$repository_root/.github/workflows/publish.yml" "$fixture_root/.github/workflows/publish.yml"
sed -E \
  -e '/validate-release-tag\.sh/s/^([[:space:]]*)/\1# /' \
  "$repository_root/.github/workflows/release.yml" \
  > "$fixture_root/.github/workflows/release.yml.commented"
mv "$fixture_root/.github/workflows/release.yml.commented" "$fixture_root/.github/workflows/release.yml"
if "$script_dir/verify-release-policy.sh" "$fixture_root" >/dev/null 2>&1; then
  printf 'accepted a release workflow with tag validation commented out\n' >&2
  exit 1
fi

cp "$repository_root/.github/workflows/release.yml" "$fixture_root/.github/workflows/release.yml"
printf '\nregistry: docker.io\n' >> "$fixture_root/.github/workflows/publish.yml"
if "$script_dir/verify-release-policy.sh" "$fixture_root" >/dev/null 2>&1; then
  printf 'accepted a workflow fixture containing a forbidden publisher\n' >&2
  exit 1
fi
