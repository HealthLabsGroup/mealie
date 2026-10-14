#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd "$script_dir/../.." && pwd)

expected=$'upstream-version=3.28.0\npackage-version=3.28.0+healthlabs.1'
actual=$("$script_dir/validate-release-tag.sh" v3.28.0-healthlabs.1)
test "$actual" = "$expected"

for invalid_tag in 3.28.0-healthlabs.1 v3.28-healthlabs.1 v3.28.0-healthlabs.0 v3.28.0-healthlabs.latest v3.28.0; do
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

mkdir -p "$fixture_root/.github/workflows"
cp "$repository_root/.github/workflows/publish.yml" "$fixture_root/.github/workflows/publish.yml"
cp "$repository_root/.github/workflows/release.yml" "$fixture_root/.github/workflows/release.yml"
"$script_dir/verify-release-policy.sh" "$fixture_root"

printf '\nregistry: docker.io\n' >> "$fixture_root/.github/workflows/publish.yml"
if "$script_dir/verify-release-policy.sh" "$fixture_root" >/dev/null 2>&1; then
  printf 'accepted a workflow fixture containing a forbidden publisher\n' >&2
  exit 1
fi
