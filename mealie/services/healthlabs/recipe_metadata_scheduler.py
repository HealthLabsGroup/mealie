"""Enqueue-only refresh scheduling. Callers commit together with scanner progress."""

from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mealie.db.models._model_base import SqlAlchemyBase
from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate, HealthLabsRecipeMetadataScan
from mealie.db.models.recipe import RecipeModel
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.recipe import Recipe
from mealie.services.healthlabs.recipe_metadata_service import POLICY_VERSION, RecipeMetadataService, fingerprint


def insert_once(session: Session, model: type[SqlAlchemyBase], values: dict[str, Any], keys: list[str]) -> None:
    dialect = session.get_bind().dialect.name
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    session.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=keys))


@dataclass(frozen=True)
class MetadataScanReport:
    scanned: int = 0
    wrapped: bool = False
    recipe_ids: tuple[UUID, ...] = ()


class RecipeMetadataScheduler:
    def __init__(self, session: Session, *, country: str = "GB", now: datetime | None = None) -> None:
        self.session = session
        self.country = country
        self.now = now or get_utc_now()

    def schedule(self, recipe: Recipe) -> UUID | None:
        service = RecipeMetadataService(self.session, country=self.country)
        inputs = service.inputs(recipe)
        jobs = HealthLabsRecipeMetadataEstimate
        self.session.execute(
            sa.update(jobs)
            .where(
                jobs.group_id == recipe.group_id,
                jobs.recipe_id == recipe.id,
                sa.or_(jobs.input_fingerprint != inputs.input_fingerprint, sa.literal(not inputs.ingredients)),
                jobs.state.in_(["pending", "running", "failed"]),
            )
            .values(
                state="superseded",
                lease_token=None,
                lease_expires_at=None,
                updated_at=self.now,
                **({"last_error_code": "empty_ingredients"} if not inputs.ingredients else {}),
            )
        )
        if not inputs.ingredients or not inputs.missing_fields():
            return None
        identity = {
            "group_id": recipe.group_id,
            "recipe_id": recipe.id,
            "input_fingerprint": inputs.input_fingerprint,
            "policy_version": POLICY_VERSION,
        }
        current = self.session.scalar(
            sa.select(jobs).where(*(getattr(jobs, key) == value for key, value in identity.items()))
        )
        if current is not None and current.state == "ready":
            return current.id
        reuse = service.reusable_payload(inputs)
        insert_once(
            self.session,
            jobs,
            dict(
                **identity,
                id=uuid4(),
                authoring_fingerprint=inputs.authoring_fingerprint,
                state="pending",
                next_attempt_at=self.now,
                created_at=self.now,
                updated_at=self.now,
            ),
            list(identity),
        )
        work_id = self.session.scalar(
            sa.select(jobs.id).where(*(getattr(jobs, key) == value for key, value in identity.items()))
        )
        self.session.execute(
            sa.update(jobs)
            .where(jobs.id == work_id, jobs.state == "superseded")
            .values(
                state="pending",
                attempt_count=0,
                next_attempt_at=self.now,
                last_error_code=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=self.now,
            )
        )
        if reuse is not None:
            self.session.execute(
                sa.update(jobs)
                .where(jobs.id == work_id, jobs.state != "running", jobs.state != "ready")
                .values(
                    state="ready",
                    payload=reuse.model_dump(mode="json"),
                    provider_name=reuse.model_review.provider,
                    provider_model=reuse.model_review.model,
                    last_error_code=None,
                    updated_at=self.now,
                )
            )
        return work_id

    def scan_page(
        self, *, group_ids: set[UUID] | frozenset[UUID] | None = None, page_size: int = 20, max_seconds: float = 15
    ) -> MetadataScanReport:
        if not 1 <= page_size <= 100 or max_seconds <= 0:
            raise ValueError("scan bounds must be positive; at most 100 recipes per page")
        scope = fingerprint(
            {
                "groups": sorted(str(g) for g in group_ids) if group_ids is not None else "default-provider-groups",
                "country": self.country,
            }
        )
        scan = HealthLabsRecipeMetadataScan
        insert_once(self.session, scan, {"id": uuid4(), "scope_key": scope, "updated_at": self.now}, ["scope_key"])
        cursor = self.session.scalar(sa.select(scan).where(scan.scope_key == scope))
        assert cursor is not None
        # A compare-and-swap obtains the short DB write lock for the whole page.
        # Cursor movement and every enqueue commit atomically; no provider I/O here.
        generation, after = cursor.generation, cursor.cursor_recipe_id
        if (
            self.session.execute(
                sa.update(scan)
                .where(scan.id == cursor.id, scan.generation == generation)
                .values(generation=generation + 1)
            ).rowcount
            != 1
        ):
            self.session.rollback()
            return MetadataScanReport()
        query = sa.select(RecipeModel.id, RecipeModel.group_id)
        if group_ids is None:
            query = query.join(AIProviderSettings, AIProviderSettings.group_id == RecipeModel.group_id).join(
                AIProvider,
                sa.and_(
                    AIProvider.id == AIProviderSettings.default_provider_id,
                    AIProvider.settings_id == AIProviderSettings.id,
                ),
            )
        else:
            query = query.where(RecipeModel.group_id.in_(group_ids))
        if after is not None:
            query = query.where(RecipeModel.id > after)
        page = list(self.session.execute(query.order_by(RecipeModel.id).limit(page_size)))
        if not page:
            cursor.cursor_recipe_id = None
            cursor.cycles += 1
            cursor.updated_at = self.now
            self.session.commit()
            return MetadataScanReport(wrapped=True)
        deadline = monotonic() + max_seconds
        ids = []
        for recipe_id, group_id in page:
            recipe = AllRepositories(self.session, group_id=group_id, household_id=None).recipes.get_one(
                recipe_id, "id"
            )
            if recipe is not None:
                self.schedule(recipe)
            ids.append(recipe_id)
            cursor.cursor_recipe_id = recipe_id
            if monotonic() >= deadline:
                break
        cursor.updated_at = self.now
        self.session.commit()
        return MetadataScanReport(scanned=len(ids), recipe_ids=tuple(ids))
