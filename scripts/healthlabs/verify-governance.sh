#!/usr/bin/env bash
set -euo pipefail

repository_root=${1:-$(git rev-parse --show-toplevel)}

require_text() {
  local file=$1
  local text=$2
  if ! grep -Fq -- "$text" "$repository_root/$file"; then
    printf 'missing required text in %s: %s\n' "$file" "$text" >&2
    return 1
  fi
}

require_text LICENSE "GNU AFFERO GENERAL PUBLIC LICENSE"
require_text README.md "HealthLabs maintained fork"
require_text README.md "modified by HealthLabs beginning 2026-10-14"
require_text README.md "https://github.com/mealie-recipes/mealie"
require_text HEALTHLABS.md "upstream/mealie-next"
require_text HEALTHLABS.md "modified by HealthLabs beginning 2026-10-14"
require_text HEALTHLABS.md "v<upstream-version>-healthlabs.<revision>"
require_text HEALTHLABS.md "ghcr.io/healthlabsgroup/mealie"
require_text HEALTHLABS.md "Ephra integration boundary"
require_text .github/pull_request_template.md "Change classification"
require_text .github/pull_request_template.md "AGPL/source disclosure impact"

# shortcut: FAO is the only accepted workbook input, add a guard before accepting another raw source format.
tracked_workbooks=$(git -C "$repository_root" ls-files -- '*.xlsx' '*.xls' '*.ods')
if [[ -n $tracked_workbooks ]]; then
  printf 'tracked nutrition source workbooks are forbidden:\n%s\n' "$tracked_workbooks" >&2
  exit 1
fi
