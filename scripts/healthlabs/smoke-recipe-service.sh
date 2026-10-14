#!/usr/bin/env bash
set -euo pipefail

for command in curl jq; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'required command is missing: %s\n' "$command" >&2
    exit 1
  fi
done

: "${HEALTHLABS_BASE_URL:?set HEALTHLABS_BASE_URL to the Mealie origin}"
: "${HEALTHLABS_PROVISIONING_SECRET:?set HEALTHLABS_PROVISIONING_SECRET}"

origin=${HEALTHLABS_BASE_URL%/}
api="$origin/api/healthlabs/v1"
subject=${HEALTHLABS_SMOKE_SUBJECT:-"release-smoke-$(date -u +%Y%m%d%H%M%S)-$$"}
work_dir=$(mktemp -d /tmp/healthlabs-recipe-smoke.XXXXXX)
token=
recipe_id=

cleanup() {
  set +e
  if [[ -n "$token" && -n "$recipe_id" ]]; then
    curl --fail-with-body --silent --show-error \
      --request DELETE \
      --header "Authorization: Bearer $token" \
      --header "Idempotency-Key: $subject-delete" \
      "$api/recipes/$recipe_id" >/dev/null
  fi
  curl --fail-with-body --silent --show-error \
    --request DELETE \
    --header "X-HealthLabs-Provisioning-Secret: $HEALTHLABS_PROVISIONING_SECRET" \
    "$api/subjects/$subject" >/dev/null
  rm -rf "$work_dir"
}
trap cleanup EXIT

jq -n --arg subject "$subject" '{externalSubjectId: $subject}' >"$work_dir/provision.json"
curl --fail-with-body --silent --show-error \
  --request POST \
  --header 'Content-Type: application/json' \
  --header "X-HealthLabs-Provisioning-Secret: $HEALTHLABS_PROVISIONING_SECRET" \
  --data-binary @"$work_dir/provision.json" \
  "$api/subjects" >"$work_dir/provisioned.json"

token=$(jq -er '.token' "$work_dir/provisioned.json")
jq -e '.capabilities | contains(["recipes:read_own", "recipes:write_own", "catalogue:read"])' \
  "$work_dir/provisioned.json" >/dev/null

jq -n --arg name "HealthLabs release smoke $subject" '{
  name: $name,
  servings: 1,
  ingredients: [{quantity: 1, food: {name: "Smoke ingredient"}, originalText: "1 Smoke ingredient"}],
  instructions: [{text: "Verify the immutable recipe-service release."}],
  authoring: {smokeTest: true}
}' >"$work_dir/recipe.json"

curl --fail-with-body --silent --show-error \
  --request POST \
  --header 'Content-Type: application/json' \
  --header "Authorization: Bearer $token" \
  --header "Idempotency-Key: $subject-create" \
  --data-binary @"$work_dir/recipe.json" \
  "$api/recipes" >"$work_dir/created.json"

recipe_id=$(jq -er '.id' "$work_dir/created.json")
revision_id=$(jq -er '.revisionId' "$work_dir/created.json")
jq -e '.scope == "personal" and .revisionNumber == 1' "$work_dir/created.json" >/dev/null

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer $token" \
  "$api/recipes/$recipe_id" | jq -e --arg id "$recipe_id" '.id == $id' >/dev/null

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer $token" \
  "$api/recipes/$recipe_id/revisions/$revision_id" \
  | jq -e --arg recipe_id "$recipe_id" '.recipeId == $recipe_id and .revisionNumber == 1' >/dev/null

curl --fail-with-body --silent --show-error \
  --get \
  --header "Authorization: Bearer $token" \
  --data-urlencode "q=HealthLabs release smoke $subject" \
  "$api/recipes" | jq -e --arg id "$recipe_id" '.items | any(.id == $id)' >/dev/null

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer $token" \
  "$api/operations/$subject-create" \
  | jq -e '.state == "completed" and .responseStatus == 201' >/dev/null

printf 'HealthLabs recipe-service smoke passed: subject=%s recipe=%s\n' "$subject" "$recipe_id"
