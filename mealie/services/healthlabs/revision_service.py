from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from mealie.db.models.recipe.recipe import HealthLabsRecipeRevision, RecipeModel
from mealie.schema.recipe.recipe import Recipe


class RecipeRevisionService:
    """Creates immutable content-addressed snapshots for recipe writes."""

    SNAPSHOT_FIELDS = (
        "id",
        "user_id",
        "group_id",
        "name",
        "slug",
        "description",
        "recipe_servings",
        "recipe_yield_quantity",
        "recipe_yield",
        "total_time",
        "prep_time",
        "cook_time",
        "perform_time",
        "total_time_seconds",
        "prep_time_seconds",
        "perform_time_seconds",
        "org_url",
        "healthlabs_scope",
        "copied_from_recipe_id",
        "copied_from_revision_id",
        "copied_at",
        "image",
        "recipe_ingredient",
        "recipe_instructions",
        "recipe_category",
        "tags",
        "tools",
        "nutrition",
        "settings",
        "assets",
        "notes",
    )
    UNORDERED_LIST_FIELDS = ("recipe_category", "tags", "tools")

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def canonical_json(snapshot: dict[str, Any]) -> bytes:
        return json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

    @classmethod
    def build_snapshot(cls, recipe: Recipe) -> dict[str, Any]:
        data = recipe.model_dump(mode="json", by_alias=False)
        snapshot = {field: data.get(field) for field in cls.SNAPSHOT_FIELDS}
        for field in cls.UNORDERED_LIST_FIELDS:
            snapshot[field] = sorted(
                snapshot[field] or [],
                key=lambda item: cls.canonical_json(item),
            )

        # Mealie rebuilds instruction row IDs during a byte-equivalent full PUT.
        # The IDs are persistence details, while instruction order and content are
        # the immutable recipe meaning captured by this snapshot.
        for instruction in snapshot["recipe_instructions"] or []:
            instruction.pop("id", None)

        extras = data.get("extras") or {}
        snapshot["extras"] = {key: value for key, value in extras.items() if key.startswith("healthlabs.")}
        return snapshot

    def latest(self, recipe_id: UUID) -> HealthLabsRecipeRevision | None:
        return self.session.scalars(
            select(HealthLabsRecipeRevision)
            .where(HealthLabsRecipeRevision.recipe_id == recipe_id)
            .order_by(HealthLabsRecipeRevision.revision_number.desc())
            .limit(1)
        ).one_or_none()

    def record(self, recipe: Recipe, *, actor_user_id: UUID) -> HealthLabsRecipeRevision:
        if recipe.id is None:
            raise ValueError("Cannot revision a recipe without an id")

        snapshot = self.build_snapshot(recipe)
        content_sha256 = hashlib.sha256(self.canonical_json(snapshot)).hexdigest()
        latest = self.latest(recipe.id)
        if latest is not None and latest.content_sha256 == content_sha256:
            recipe.healthlabs_revision_number = latest.revision_number
            return latest

        revision_number = 1 if latest is None else latest.revision_number + 1
        revision = HealthLabsRecipeRevision(
            recipe_id=recipe.id,
            revision_number=revision_number,
            snapshot=snapshot,
            content_sha256=content_sha256,
            actor_user_id=actor_user_id,
        )
        model = self.session.get(RecipeModel, recipe.id)
        if model is None:
            raise ValueError("Cannot revision a recipe that is not persisted")

        model.healthlabs_revision_number = revision_number
        self.session.add(revision)
        self.session.commit()
        self.session.refresh(revision)
        recipe.healthlabs_revision_number = revision_number
        return revision
