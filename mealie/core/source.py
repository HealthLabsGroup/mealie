import re

HEALTHLABS_SOURCE_REPOSITORY = "https://github.com/HealthLabsGroup/mealie"
_FULL_LOWERCASE_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def healthlabs_source_url(build_id: str) -> str:
    if _FULL_LOWERCASE_GIT_SHA.fullmatch(build_id):
        return f"{HEALTHLABS_SOURCE_REPOSITORY}/tree/{build_id}"
    return HEALTHLABS_SOURCE_REPOSITORY
