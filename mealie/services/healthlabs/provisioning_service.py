from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import sqlalchemy as sa
from slugify import slugify
from sqlalchemy.orm import Session

from mealie.core.security import create_access_token, hash_password
from mealie.db.models.group import AIProviderSettings, Group, GroupPreferencesModel
from mealie.db.models.healthlabs import HealthLabsCapabilityGrant
from mealie.db.models.household import Household, HouseholdPreferencesModel
from mealie.db.models.users import LongLiveToken, User
from mealie.schema.healthlabs.identity import HealthLabsCapability

CatalogueAccess = Literal["reader", "editor"]

SUBJECT_CAPABILITIES = frozenset(
    {
        HealthLabsCapability.RECIPES_READ_OWN,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.CATALOGUE_READ,
    }
)
CATALOGUE_CAPABILITIES: dict[CatalogueAccess, frozenset[HealthLabsCapability]] = {
    "reader": frozenset({HealthLabsCapability.CATALOGUE_READ}),
    "editor": frozenset(
        {
            HealthLabsCapability.CATALOGUE_READ,
            HealthLabsCapability.CATALOGUE_WRITE,
        }
    ),
}
CATALOGUE_GROUP_NAME = "HealthLabs catalogue"
CATALOGUE_HOUSEHOLD_NAME = "Catalogue"
TOKEN_LIFETIME = timedelta(days=1825)


@dataclass(frozen=True)
class ProvisionedCredential:
    external_subject_id: str
    user_id: str
    group_id: str
    household_id: str
    token: str
    capabilities: tuple[str, ...]
    created: bool


class HealthLabsProvisioningService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def provision_subject(self, external_subject_id: str) -> ProvisionedCredential:
        self._acquire_subject_lock(external_subject_id)
        existing = self._find_user(external_subject_id)
        if existing is not None:
            return self._credential_for(existing, created=False)

        digest = self._subject_digest(external_subject_id)
        group_name = f"HealthLabs subject {digest}"
        household_name = "Recipes"
        group, household = self._create_workspace(group_name, household_name)
        user = self._create_user(
            external_subject_id=external_subject_id,
            group=group,
            household=household,
            label=f"HealthLabs recipe user {digest}",
            capabilities=SUBJECT_CAPABILITIES,
            token_name="HealthLabs Ephra recipe service",
        )
        self.session.commit()
        return self._credential_for(user, created=True)

    def provision_catalogue(self, access: CatalogueAccess) -> ProvisionedCredential:
        external_subject_id = f"healthlabs:catalogue:{access}"
        self._acquire_subject_lock("healthlabs:catalogue")
        existing = self._find_user(external_subject_id)
        if existing is not None:
            return self._credential_for(existing, created=False)

        group = self.session.scalar(sa.select(Group).where(Group.name == CATALOGUE_GROUP_NAME))
        if group is None:
            group, household = self._create_workspace(CATALOGUE_GROUP_NAME, CATALOGUE_HOUSEHOLD_NAME)
        else:
            household = self.session.scalar(
                sa.select(Household).where(
                    Household.group_id == group.id,
                    Household.name == CATALOGUE_HOUSEHOLD_NAME,
                )
            )
            if household is None:
                raise RuntimeError("HealthLabs catalogue workspace is incomplete")

        user = self._create_user(
            external_subject_id=external_subject_id,
            group=group,
            household=household,
            label=f"HealthLabs catalogue {access}",
            capabilities=CATALOGUE_CAPABILITIES[access],
            token_name=f"HealthLabs catalogue {access}",
        )
        self.session.commit()
        return self._credential_for(user, created=True)

    def rotate_subject_token(self, external_subject_id: str) -> ProvisionedCredential:
        self._acquire_subject_lock(external_subject_id)
        user = self._find_user(external_subject_id)
        if user is None or external_subject_id.startswith("healthlabs:catalogue:"):
            raise LookupError(external_subject_id)

        self.session.execute(sa.delete(LongLiveToken).where(LongLiveToken.user_id == user.id))
        self.session.add(self._new_api_token(user, "HealthLabs Ephra recipe service"))
        self.session.commit()
        return self._credential_for(user, created=False)

    def deprovision_subject(self, external_subject_id: str) -> None:
        self._acquire_subject_lock(external_subject_id)
        user = self._find_user(external_subject_id)
        if user is None or external_subject_id.startswith("healthlabs:catalogue:"):
            raise LookupError(external_subject_id)

        group = user.group
        self.session.execute(sa.delete(HealthLabsCapabilityGrant).where(HealthLabsCapabilityGrant.user_id == user.id))
        self.session.delete(user)
        self.session.flush()
        self.session.delete(group)
        self.session.commit()

    def _acquire_subject_lock(self, external_subject_id: str) -> None:
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            self.session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:subject))"),
                {"subject": external_subject_id},
            )
        elif dialect == "sqlite":
            self.session.execute(sa.text("BEGIN IMMEDIATE"))

    def _create_workspace(self, group_name: str, household_name: str) -> tuple[Group, Household]:
        group = Group(session=self.session, name=group_name, slug=slugify(group_name))
        self.session.add(group)
        self.session.flush()
        self.session.add_all(
            [
                GroupPreferencesModel(
                    session=self.session,
                    group_id=group.id,
                    private_group=True,
                    recipe_public=False,
                ),
                AIProviderSettings(session=self.session, group_id=group.id),
            ]
        )

        household = Household(
            session=self.session,
            name=household_name,
            slug=slugify(household_name),
            group_id=group.id,
        )
        self.session.add(household)
        self.session.flush()
        self.session.add(
            HouseholdPreferencesModel(
                session=self.session,
                household_id=household.id,
                private_household=True,
                recipe_public=False,
            )
        )
        return group, household

    def _create_user(
        self,
        *,
        external_subject_id: str,
        group: Group,
        household: Household,
        label: str,
        capabilities: frozenset[HealthLabsCapability],
        token_name: str,
    ) -> User:
        digest = self._subject_digest(external_subject_id)
        username = f"subject-{digest}"
        user = User(
            session=self.session,
            username=username,
            full_name=label,
            email=f"{username}@subjects.invalid",
            password=hash_password(secrets.token_urlsafe(48)),
            admin=False,
            advanced=False,
            group=group.name,
            household=household.name,
            external_subject_id=external_subject_id,
            healthlabs_managed=True,
        )
        self.session.add(user)
        self.session.flush()
        self.session.add_all(
            [
                HealthLabsCapabilityGrant(
                    user_id=user.id,
                    capability=capability.value,
                    reason="HealthLabs managed-account provisioning",
                )
                for capability in capabilities
            ]
        )
        self.session.add(self._new_api_token(user, token_name))
        return user

    def _new_api_token(self, user: User, name: str) -> LongLiveToken:
        token, _ = create_access_token(
            {
                "long_token": True,
                "id": str(user.id),
                "name": name,
                "integration_id": "healthlabs",
                "jti": secrets.token_urlsafe(24),
            },
            TOKEN_LIFETIME,
        )
        return LongLiveToken(name=name, token=token, user_id=user.id)

    def _credential_for(self, user: User, *, created: bool) -> ProvisionedCredential:
        token = self.session.scalar(
            sa.select(LongLiveToken).where(LongLiveToken.user_id == user.id).order_by(LongLiveToken.created_at.desc())
        )
        if token is None:
            raise RuntimeError("HealthLabs managed account has no API token")
        capabilities = self.session.scalars(
            sa.select(HealthLabsCapabilityGrant.capability)
            .where(HealthLabsCapabilityGrant.user_id == user.id)
            .where(HealthLabsCapabilityGrant.revoked_at.is_(None))
            .order_by(HealthLabsCapabilityGrant.capability)
        ).all()
        return ProvisionedCredential(
            external_subject_id=user.external_subject_id or "",
            user_id=str(user.id),
            group_id=str(user.group_id),
            household_id=str(user.household_id),
            token=token.token,
            capabilities=tuple(capabilities),
            created=created,
        )

    def _find_user(self, external_subject_id: str) -> User | None:
        return self.session.scalar(sa.select(User).where(User.external_subject_id == external_subject_id))

    @staticmethod
    def _subject_digest(external_subject_id: str) -> str:
        return hashlib.sha256(external_subject_id.encode()).hexdigest()[:32]
