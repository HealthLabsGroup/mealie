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
bash -n "$script_dir/smoke-recipe-service.sh"
bash -n "$script_dir/smoke-plan-service.sh"
grep -Fq 'HEALTHLABS_PLAN_EDITOR_TOKEN' "$script_dir/smoke-plan-service.sh"
grep -Fq '/plan-publications' "$script_dir/smoke-plan-service.sh"
grep -Fq '/entries/' "$script_dir/smoke-plan-service.sh"

if insecure_output=$(
  HEALTHLABS_BASE_URL=http://127.0.0.1:1 \
    HEALTHLABS_PROVISIONING_SECRET=test-secret \
    HEALTHLABS_PLAN_EDITOR_TOKEN=test-token \
    "$script_dir/smoke-plan-service.sh" 2>&1
); then
  printf 'plan-service smoke accepted an insecure base URL\n' >&2
  exit 1
fi
if ! grep -Fq 'HEALTHLABS_BASE_URL must use https://' <<<"$insecure_output"; then
  printf 'plan-service smoke did not reject HTTP before making requests\n' >&2
  exit 1
fi

fixture_root=$(mktemp -d)
cleanup() {
  if [[ -n ${fixture_root:-} && -d $fixture_root ]]; then
    rm -rf -- "$fixture_root"
  fi
}
trap cleanup EXIT

mkdir -p "$fixture_root/.github/workflows" "$fixture_root/docker"
cp "$repository_root/LICENSE" "$repository_root/README.md" "$repository_root/HEALTHLABS.md" "$fixture_root/"
cp "$repository_root/.github/pull_request_template.md" "$fixture_root/.github/pull_request_template.md"
cp "$repository_root/.github/workflows/publish.yml" "$fixture_root/.github/workflows/publish.yml"
cp "$repository_root/.github/workflows/pull-requests.yml" "$fixture_root/.github/workflows/pull-requests.yml"
cp "$repository_root/.github/workflows/release.yml" "$fixture_root/.github/workflows/release.yml"
cp "$repository_root/.github/workflows/partial-trivy-container-scanning.yml" "$fixture_root/.github/workflows/partial-trivy-container-scanning.yml"
cp "$repository_root/docker/Dockerfile" "$fixture_root/docker/Dockerfile"
git -C "$fixture_root" init --quiet
git -C "$fixture_root" add .
"$script_dir/verify-governance.sh" "$fixture_root"

printf 'synthetic fixture\n' > "$fixture_root/density_DB_v2.xlsx"
git -C "$fixture_root" add -f density_DB_v2.xlsx
if "$script_dir/verify-governance.sh" "$fixture_root" >/dev/null 2>&1; then
  printf 'accepted a tracked nutrition source workbook\n' >&2
  exit 1
fi

"$script_dir/verify-release-policy.sh" "$fixture_root"

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
