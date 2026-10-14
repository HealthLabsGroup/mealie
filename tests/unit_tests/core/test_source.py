import pytest

from mealie.core.source import HEALTHLABS_SOURCE_REPOSITORY, healthlabs_source_url


@pytest.mark.parametrize("build_id", ["unknown", "", "abc1234", "A" * 40, "g" * 40])
def test_healthlabs_source_url_falls_back_for_non_commit_build_ids(build_id: str):
    assert healthlabs_source_url(build_id) == HEALTHLABS_SOURCE_REPOSITORY


def test_healthlabs_source_url_points_to_exact_lowercase_commit():
    commit = "0123456789abcdef0123456789abcdef01234567"
    assert healthlabs_source_url(commit) == (
        "https://github.com/HealthLabsGroup/mealie/tree/0123456789abcdef0123456789abcdef01234567"
    )
