import secrets

from fastapi import Header, HTTPException, Request, status

from mealie.core.config import get_app_settings
from mealie.schema.user import PrivateUser

HEALTHLABS_API_PREFIX = "/api/healthlabs/v1"


async def require_healthlabs_provisioning_secret(
    supplied_secret: str | None = Header(default=None, alias="X-HealthLabs-Provisioning-Secret"),
) -> None:
    configured_secret = get_app_settings().HEALTHLABS_PROVISIONING_SECRET
    if (
        configured_secret is None
        or supplied_secret is None
        or not secrets.compare_digest(supplied_secret, configured_secret)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid provisioning credentials")


def enforce_healthlabs_managed_route_boundary(request: Request, user: PrivateUser) -> None:
    if user.healthlabs_managed and not request.url.path.startswith(f"{HEALTHLABS_API_PREFIX}/"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managed credentials are restricted to the HealthLabs API",
        )
