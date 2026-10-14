#!/usr/bin/env bash
set -euo pipefail

tag=${1:-}
if [[ ! $tag =~ ^v([0-9]+\.[0-9]+\.[0-9]+)-healthlabs\.([1-9][0-9]*)$ ]]; then
  printf 'invalid HealthLabs release tag: %s\n' "$tag" >&2
  exit 1
fi

printf 'upstream-version=%s\n' "${BASH_REMATCH[1]}"
printf 'package-version=%s+healthlabs.%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
