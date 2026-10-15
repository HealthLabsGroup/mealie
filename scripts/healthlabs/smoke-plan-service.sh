#!/usr/bin/env bash
set -euo pipefail

for command in curl jq uuidgen; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'required command is missing: %s\n' "$command" >&2
    exit 1
  fi
done

: "${HEALTHLABS_BASE_URL:?set HEALTHLABS_BASE_URL to the Mealie origin}"
: "${HEALTHLABS_PROVISIONING_SECRET:?set HEALTHLABS_PROVISIONING_SECRET}"
: "${HEALTHLABS_PLAN_EDITOR_TOKEN:?set HEALTHLABS_PLAN_EDITOR_TOKEN}"

origin=${HEALTHLABS_BASE_URL%/}
api="$origin/api/healthlabs/v1"
subject="release-plan-smoke-$(date -u +%Y%m%d%H%M%S)-$$"
entry_id=$(uuidgen | tr '[:upper:]' '[:lower:]')
plan_date=$(date -u +%F)
work_dir=$(mktemp -d /tmp/healthlabs-plan-smoke.XXXXXX)
subject_token=

cleanup() {
  set +e
  curl --silent --show-error \
    --request DELETE \
    --header "X-HealthLabs-Provisioning-Secret: $HEALTHLABS_PROVISIONING_SECRET" \
    "$api/subjects/$subject" >/dev/null
  rm -rf -- "$work_dir"
}
trap cleanup EXIT

jq -n --arg subject "$subject" '{externalSubjectId: $subject}' >"$work_dir/provision.json"
curl --fail-with-body --silent --show-error \
  --request POST \
  --header 'Content-Type: application/json' \
  --header "X-HealthLabs-Provisioning-Secret: $HEALTHLABS_PROVISIONING_SECRET" \
  --data-binary @"$work_dir/provision.json" \
  "$api/subjects" >"$work_dir/provisioned.json"

subject_token=$(jq -er '.token' "$work_dir/provisioned.json")
jq -e '.capabilities | contains(["plans:read_own", "plans:mark_cooked_own"])' \
  "$work_dir/provisioned.json" >/dev/null

jq -n \
  --arg from "$plan_date" \
  --arg entry_id "$entry_id" \
  '{
    from: $from,
    days: 3,
    entries: [{
      id: $entry_id,
      date: $from,
      entryType: "dinner",
      position: 0,
      servings: 1,
      title: "Release smoke meal"
    }]
  }' >"$work_dir/publication.json"

publish_plan() {
  local output=$1
  curl --fail-with-body --silent --show-error \
    --request POST \
    --header 'Content-Type: application/json' \
    --header "Authorization: Bearer $HEALTHLABS_PLAN_EDITOR_TOKEN" \
    --header "Idempotency-Key: $subject-publish" \
    --header 'X-HealthLabs-Access-Reason: Release plan-service smoke test' \
    --data-binary @"$work_dir/publication.json" \
    "$api/subjects/$subject/plan-publications" >"$output"
}

publish_plan "$work_dir/published.json"
publish_plan "$work_dir/replayed.json"
plan_id=$(jq -er '.id' "$work_dir/published.json")
jq -e --arg entry_id "$entry_id" \
  '.publicationNumber == 1 and .entries[0].id == $entry_id and .entries[0].version == 1' \
  "$work_dir/published.json" >/dev/null
test "$(jq -S . "$work_dir/published.json")" = "$(jq -S . "$work_dir/replayed.json")"

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer $HEALTHLABS_PLAN_EDITOR_TOKEN" \
  --header 'X-HealthLabs-Access-Reason: Release plan-service smoke test' \
  "$api/subjects/$subject/plans/current" >"$work_dir/editor-current.json"
curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer $subject_token" \
  "$api/plans/current" >"$work_dir/subject-current.json"
jq -e --arg plan_id "$plan_id" '.id == $plan_id' "$work_dir/editor-current.json" >/dev/null
test "$(jq -S . "$work_dir/editor-current.json")" = "$(jq -S . "$work_dir/subject-current.json")"

jq -n '{version: 1, cooked: true}' >"$work_dir/cooked.json"
curl --fail-with-body --silent --show-error \
  --request PATCH \
  --header 'Content-Type: application/json' \
  --header "Authorization: Bearer $subject_token" \
  --header "Idempotency-Key: $subject-cooked" \
  --data-binary @"$work_dir/cooked.json" \
  "$api/plans/$plan_id/entries/$entry_id" \
  | jq -e '.cooked == true and .version == 2 and .cookedAt != null' >/dev/null

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer $subject_token" \
  "$api/operations/$subject-cooked" \
  | jq -e '.state == "completed" and .responseStatus == 200' >/dev/null

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer $subject_token" \
  "$api/plans/current" \
  | jq -e '.entries[0].cooked == true and .entries[0].version == 2' >/dev/null

printf 'HealthLabs plan-service smoke passed: subject=%s publication=%s\n' "$subject" "$plan_id"
