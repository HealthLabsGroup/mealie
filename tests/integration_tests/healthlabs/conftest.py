from collections.abc import Generator

from fastapi import Depends, Request
from pytest import fixture

from mealie.app import app
from mealie.core.dependencies.dependencies import get_auth_token, get_current_user
from mealie.db.db_setup import generate_session


def _healthlabs_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": "/api/healthlabs/v1/service-policy-test",
            "raw_path": b"/api/healthlabs/v1/service-policy-test",
            "query_string": b"",
            "headers": [],
        }
    )


async def _get_managed_user_for_service_policy_test(
    token: str = Depends(get_auth_token),
    session=Depends(generate_session),
):
    return await get_current_user(_healthlabs_request(), token, session)


@fixture
def allow_managed_upstream_for_service_policy_tests() -> Generator[None]:
    """Expose upstream controllers only while testing their lower-level managed-user policy."""
    app.dependency_overrides[get_current_user] = _get_managed_user_for_service_policy_test
    yield
    app.dependency_overrides.pop(get_current_user, None)
