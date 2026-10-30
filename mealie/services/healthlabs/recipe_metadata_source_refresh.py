"""Bounded best-effort notification; periodic scanning repairs the crash gap."""

from collections.abc import Callable
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core import root_logger
from mealie.db.models.recipe import RecipeIngredientModel, RecipeModel
from mealie.repos.repository_factory import AllRepositories
from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler
from mealie.services.openai.openai import OpenAIRequestRejected

RecipeAuthorizationGuard = Callable[[Session, UUID], bool]


class RecipeAuthorizationChanged(OpenAIRequestRejected):
    pass


logger = root_logger.get_logger(__name__)


def refresh_source_recipes(
    session: Session,
    *,
    group_id: UUID,
    kind: str,
    target_id: str,
    recipe_ids: set[UUID] | frozenset[UUID] | None = None,
    limit: int = 20,
    recipe_guard: RecipeAuthorizationGuard | None = None,
) -> int:
    """Notify at most one page. Caller retains responsibility for source commit.

    Source writes are flushed before the notification savepoint. An enqueue
    failure rolls back that savepoint only, preserving authoritative changes.
    High-fanout tails are deliberately covered by the durable periodic scan.
    """
    if not 1 <= limit <= 100:
        raise ValueError("refresh page must be 1..100")
    query = (
        sa.select(RecipeModel.id)
        .join(RecipeIngredientModel, RecipeIngredientModel.recipe_id == RecipeModel.id)
        .where(RecipeModel.group_id == group_id)
    )
    if kind == "food_match":
        query = query.where(RecipeIngredientModel.food_id == UUID(target_id))
    else:
        ingredient = session.get(RecipeIngredientModel, int(target_id))
        if ingredient is None:
            return 0
        if kind == "portion_resolution":
            query = query.where(
                RecipeIngredientModel.food_id == ingredient.food_id, RecipeIngredientModel.unit_id == ingredient.unit_id
            )
        else:
            query = query.where(RecipeIngredientModel.id == ingredient.id)
    if recipe_ids is not None:
        query = query.where(RecipeModel.id.in_(recipe_ids))
    session.flush()
    count = 0
    try:
        with session.begin_nested():
            for recipe_id in session.scalars(query.distinct().order_by(RecipeModel.id).limit(limit)):
                if recipe_guard is not None and not recipe_guard(session, recipe_id):
                    continue
                recipe = AllRepositories(session, group_id=group_id, household_id=None).recipes.get_one(recipe_id, "id")
                if recipe is not None:
                    RecipeMetadataScheduler(session).schedule(recipe)
                    count += 1
    except Exception:
        logger.warning("Recipe metadata source notification deferred to periodic reconciliation")
        return 0
    return count


def source_work_recipe_scope(recipe_ids: set[UUID] | frozenset[UUID] | None) -> sa.ColumnElement[bool]:
    """Live target membership, including moves while a provider call is pending."""
    from mealie.db.models.healthlabs import HealthLabsNutritionWork as Work

    if recipe_ids is None:
        return sa.true()
    return (
        sa.select(RecipeIngredientModel.id)
        .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
        .where(
            RecipeModel.id.in_(recipe_ids),
            RecipeModel.group_id == Work.group_id,
            sa.or_(
                sa.and_(
                    Work.kind.in_(["ingredient_structure", "portion_resolution"]),
                    sa.cast(RecipeIngredientModel.id, sa.String) == Work.target_id,
                ),
                sa.and_(
                    Work.kind == "food_match",
                    sa.func.replace(sa.cast(RecipeIngredientModel.food_id, sa.String), "-", "")
                    == sa.func.replace(Work.target_id, "-", ""),
                ),
            ),
        )
        .exists()
    )


def source_target_authorized(
    session: Session,
    *,
    group_id: UUID,
    kind: str,
    target_id: str,
    recipe_ids: set[UUID] | frozenset[UUID] | None,
    recipe_guard: RecipeAuthorizationGuard | None,
) -> bool:
    if recipe_guard is None:
        return True
    query = (
        sa.select(RecipeModel.id)
        .join(RecipeIngredientModel, RecipeIngredientModel.recipe_id == RecipeModel.id)
        .where(RecipeModel.group_id == group_id)
    )
    if kind == "food_match":
        query = query.where(RecipeIngredientModel.food_id == UUID(target_id))
    else:
        query = query.where(RecipeIngredientModel.id == int(target_id))
    if recipe_ids is not None:
        query = query.where(RecipeModel.id.in_(recipe_ids))
    return any(recipe_guard(session, recipe_id) for recipe_id in session.scalars(query.distinct()))
