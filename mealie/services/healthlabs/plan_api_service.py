from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import (
    HealthLabsPlanEntryStatus,
    HealthLabsPlanPublication,
    HealthLabsPlanPublicationEntry,
)
from mealie.db.models.household.mealplan import GroupMealPlan
from mealie.db.models.recipe.recipe import HealthLabsRecipeRevision, RecipeModel
from mealie.db.models.users import User
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.healthlabs.plan import (
    HealthLabsPlanEntryStatusPatch,
    HealthLabsPlanPublicationEntryOut,
    HealthLabsPlanPublicationOut,
    HealthLabsPlanPublicationWrite,
    plan_content_hash,
)
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import RecipeAccessPolicy
from mealie.services.healthlabs.idempotency_service import IdempotencyConflict, IdempotencyService


class HealthLabsPlanNotFound(LookupError):
    pass


class HealthLabsPlanConflict(RuntimeError):
    pass


class PlanApiService:
    def __init__(self, session: Session, user: PrivateUser) -> None:
        self.session = session
        self.user = user
        self.policy = RecipeAccessPolicy(session, user)

    def publish(
        self,
        external_subject_id: str,
        data: HealthLabsPlanPublicationWrite,
        *,
        idempotency_key: str,
        access_reason: str | None,
    ) -> HealthLabsPlanPublicationOut:
        if not external_subject_id or not idempotency_key or len(idempotency_key) > 255:
            raise ValueError("Subject and a bounded idempotency key are required")
        reason = self.policy.require_cross_subject(
            HealthLabsCapability.PLANS_WRITE_ALL,
            action="plans.publish",
            reason=access_reason,
            subject_external_id=external_subject_id,
            resource_type="plan",
            record=False,
        )
        payload = data.model_dump(mode="json", by_alias=True)
        resource = f"/subjects/{external_subject_id}/plan-publications"
        request_sha256 = IdempotencyService.request_hash("POST", resource, payload)

        previous = self.session.scalar(
            sa.select(HealthLabsPlanPublication).where(
                HealthLabsPlanPublication.external_subject_id == external_subject_id,
                HealthLabsPlanPublication.idempotency_key == idempotency_key,
            )
        )
        if previous is not None:
            if previous.request_sha256 != request_sha256:
                raise IdempotencyConflict("Idempotency key was already used for a different request")
            return self._to_out(previous)

        def operation() -> tuple[int, dict[str, Any]]:
            publication = self._publish(
                external_subject_id,
                data,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                access_reason=reason,
            )
            return 201, self._to_out(publication).model_dump(mode="json", by_alias=True)

        result = IdempotencyService(self.session, self.user.id).execute_atomic(
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            operation=operation,
        )
        return HealthLabsPlanPublicationOut.model_validate(result.body)

    def get_subject_current(
        self,
        external_subject_id: str,
        *,
        access_reason: str | None,
    ) -> HealthLabsPlanPublicationOut | None:
        self.policy.require_cross_subject(
            HealthLabsCapability.PLANS_WRITE_ALL,
            action="plans.read_cross_subject",
            reason=access_reason,
            subject_external_id=external_subject_id,
            resource_type="plan",
        )
        self._target(external_subject_id)
        publication = self._current(external_subject_id)
        result = self._to_out(publication) if publication is not None else None
        self.session.commit()
        return result

    def get_own_current(self) -> HealthLabsPlanPublicationOut | None:
        self.policy.require(HealthLabsCapability.PLANS_READ_OWN)
        if not self.user.external_subject_id:
            raise HealthLabsPlanNotFound("Plan subject not found")
        publication = self._current(self.user.external_subject_id)
        return self._to_out(publication) if publication is not None else None

    def patch_own_entry(
        self,
        plan_id: UUID,
        entry_id: UUID,
        data: HealthLabsPlanEntryStatusPatch,
        *,
        idempotency_key: str,
    ) -> HealthLabsPlanPublicationEntryOut:
        self.policy.require(HealthLabsCapability.PLANS_MARK_COOKED_OWN)
        if not self.user.external_subject_id or not idempotency_key or len(idempotency_key) > 255:
            raise ValueError("Plan subject and a bounded idempotency key are required")
        resource = f"/plans/{plan_id}/entries/{entry_id}"
        payload = data.model_dump(mode="json", by_alias=True)
        request_sha256 = IdempotencyService.request_hash("PATCH", resource, payload)

        def operation() -> tuple[int, dict[str, Any]]:
            row = self.session.execute(
                sa.select(HealthLabsPlanPublicationEntry, HealthLabsPlanEntryStatus)
                .join(
                    HealthLabsPlanPublication,
                    HealthLabsPlanPublication.id == HealthLabsPlanPublicationEntry.publication_id,
                )
                .join(
                    HealthLabsPlanEntryStatus,
                    HealthLabsPlanEntryStatus.publication_entry_id == HealthLabsPlanPublicationEntry.id,
                )
                .where(
                    HealthLabsPlanPublication.id == plan_id,
                    HealthLabsPlanPublication.external_subject_id == self.user.external_subject_id,
                    HealthLabsPlanPublication.superseded_at.is_(None),
                    HealthLabsPlanPublicationEntry.id == entry_id,
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise HealthLabsPlanNotFound("Plan entry not found")
            entry, entry_status = row
            if entry_status.version != data.version:
                raise HealthLabsPlanConflict("Plan entry version is stale")
            entry_status.cooked_at = get_utc_now() if data.cooked else None
            entry_status.updated_by_user_id = self.user.id
            entry_status.version += 1
            self.session.flush()
            revision = (
                self.session.get(HealthLabsRecipeRevision, entry.recipe_revision_id)
                if entry.recipe_revision_id is not None
                else None
            )
            body = self._entry_to_out(entry, revision, entry_status).model_dump(mode="json", by_alias=True)
            return 200, body

        result = IdempotencyService(self.session, self.user.id).execute_atomic(
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            operation=operation,
        )
        return HealthLabsPlanPublicationEntryOut.model_validate(result.body)

    def _publish(
        self,
        external_subject_id: str,
        data: HealthLabsPlanPublicationWrite,
        *,
        idempotency_key: str,
        request_sha256: str,
        access_reason: str,
    ) -> HealthLabsPlanPublication:
        self.policy.require_cross_subject(
            HealthLabsCapability.PLANS_WRITE_ALL,
            action="plans.publish",
            reason=access_reason,
            subject_external_id=external_subject_id,
            resource_type="plan",
        )
        self._lock_subject(external_subject_id)
        target = self._target(external_subject_id)
        current = self._current(external_subject_id, lock=True)
        current_id = current.id if current is not None else None
        if data.expected_publication_id != current_id:
            raise HealthLabsPlanConflict("Expected publication is stale")

        revisions = self._validated_revisions(data)
        publication_number = (
            self.session.scalar(
                sa.select(sa.func.max(HealthLabsPlanPublication.publication_number)).where(
                    HealthLabsPlanPublication.external_subject_id == external_subject_id
                )
            )
            or 0
        ) + 1
        if current is not None:
            current.superseded_at = get_utc_now()
            self.session.flush()

        publication = HealthLabsPlanPublication(
            external_subject_id=external_subject_id,
            publication_number=publication_number,
            content_sha256=plan_content_hash(data),
            from_date=data.from_date,
            days=data.days,
            published_by_user_id=self.user.id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        self.session.add(publication)
        self.session.flush()

        entries: list[HealthLabsPlanPublicationEntry] = []
        for item in sorted(data.entries, key=lambda entry: entry.leftover_of_entry_id is not None):
            revision = revisions.get(item.recipe_revision_id)
            title = item.title or str((revision.snapshot if revision else {}).get("name") or "").strip()
            if not title:
                raise ValueError("Recipe revision has no title")
            entry = HealthLabsPlanPublicationEntry(
                id=item.id,
                publication_id=publication.id,
                date=item.date,
                entry_type=item.entry_type.value,
                position=item.position,
                servings=item.servings,
                recipe_id=item.recipe_id,
                recipe_revision_id=item.recipe_revision_id,
                title_snapshot=title,
                leftover_of_entry_id=item.leftover_of_entry_id,
            )
            self.session.add(entry)
            self.session.flush()
            self.session.add(HealthLabsPlanEntryStatus(publication_entry_id=entry.id, version=1))
            entries.append(entry)

        self._materialize_current(target, entries)
        self.session.flush()
        return publication

    def _validated_revisions(
        self,
        data: HealthLabsPlanPublicationWrite,
    ) -> dict[UUID | None, HealthLabsRecipeRevision]:
        capabilities = self.policy.active_capabilities()
        revisions: dict[UUID | None, HealthLabsRecipeRevision] = {}
        for item in data.entries:
            if item.recipe_id is None or item.recipe_revision_id is None:
                continue
            recipe = self.session.get(RecipeModel, item.recipe_id)
            revision = self.session.get(HealthLabsRecipeRevision, item.recipe_revision_id)
            visible = recipe is not None and (
                HealthLabsCapability.RECIPES_READ_ALL in capabilities
                or (recipe.user_id == self.user.id and HealthLabsCapability.RECIPES_READ_OWN in capabilities)
                or (recipe.healthlabs_scope == "catalogue" and HealthLabsCapability.CATALOGUE_READ in capabilities)
            )
            if not visible or revision is None or revision.recipe_id != item.recipe_id:
                raise HealthLabsPlanNotFound("Recipe or pinned revision not found")
            revisions[item.recipe_revision_id] = revision
        return revisions

    def _materialize_current(
        self,
        target: User,
        entries: Sequence[HealthLabsPlanPublicationEntry],
    ) -> None:
        self.session.execute(
            sa.delete(GroupMealPlan).where(
                GroupMealPlan.user_id == target.id,
                GroupMealPlan.healthlabs_publication_entry_id.is_not(None),
            )
        )
        self.session.add_all(
            [
                GroupMealPlan(
                    session=self.session,
                    date=entry.date,
                    entry_type=entry.entry_type,
                    title=entry.title_snapshot,
                    text="",
                    group_id=target.group_id,
                    user_id=target.id,
                    recipe_id=entry.recipe_id,
                    recipe_scale=entry.servings or 1.0,
                    healthlabs_publication_entry_id=entry.id,
                    healthlabs_recipe_revision_id=entry.recipe_revision_id,
                )
                for entry in entries
            ]
        )

    def _target(self, external_subject_id: str) -> User:
        target = self.session.scalar(
            sa.select(User).where(
                User.external_subject_id == external_subject_id,
                User.healthlabs_managed.is_(True),
            )
        )
        if target is None:
            raise HealthLabsPlanNotFound("Plan subject not found")
        return target

    def _current(self, external_subject_id: str, *, lock: bool = False) -> HealthLabsPlanPublication | None:
        query = sa.select(HealthLabsPlanPublication).where(
            HealthLabsPlanPublication.external_subject_id == external_subject_id,
            HealthLabsPlanPublication.superseded_at.is_(None),
        )
        if lock:
            query = query.with_for_update()
        return self.session.scalar(query)

    def _lock_subject(self, external_subject_id: str) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:subject))"),
                {"subject": external_subject_id},
            )

    def _to_out(self, publication: HealthLabsPlanPublication) -> HealthLabsPlanPublicationOut:
        rows = self.session.execute(
            sa.select(
                HealthLabsPlanPublicationEntry,
                HealthLabsRecipeRevision,
                HealthLabsPlanEntryStatus,
            )
            .outerjoin(
                HealthLabsRecipeRevision,
                HealthLabsRecipeRevision.id == HealthLabsPlanPublicationEntry.recipe_revision_id,
            )
            .outerjoin(
                HealthLabsPlanEntryStatus,
                HealthLabsPlanEntryStatus.publication_entry_id == HealthLabsPlanPublicationEntry.id,
            )
            .where(HealthLabsPlanPublicationEntry.publication_id == publication.id)
            .order_by(
                HealthLabsPlanPublicationEntry.date,
                HealthLabsPlanPublicationEntry.entry_type,
                HealthLabsPlanPublicationEntry.position,
                HealthLabsPlanPublicationEntry.id,
            )
        ).all()
        entries = [self._entry_to_out(entry, revision, status) for entry, revision, status in rows]
        return HealthLabsPlanPublicationOut(
            id=publication.id,
            publication_number=publication.publication_number,
            content_sha256=publication.content_sha256,
            from_date=publication.from_date,
            days=publication.days,
            published_at=publication.published_at,
            idempotency_key=publication.idempotency_key,
            entries=entries,
        )

    @staticmethod
    def _entry_to_out(
        entry: HealthLabsPlanPublicationEntry,
        revision: HealthLabsRecipeRevision | None,
        status: HealthLabsPlanEntryStatus | None,
    ) -> HealthLabsPlanPublicationEntryOut:
        return HealthLabsPlanPublicationEntryOut(
            id=entry.id,
            date=entry.date,
            entry_type=entry.entry_type,
            position=entry.position,
            servings=entry.servings,
            recipe_id=entry.recipe_id,
            recipe_revision_id=entry.recipe_revision_id,
            title=entry.title_snapshot,
            leftover_of_entry_id=entry.leftover_of_entry_id,
            recipe_snapshot=revision.snapshot if revision is not None else None,
            cooked=status.cooked_at is not None if status is not None else False,
            cooked_at=status.cooked_at if status is not None else None,
            version=status.version if status is not None else 1,
        )
