from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from io import BytesIO
from uuid import UUID

import sqlalchemy as sa
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from mealie.db.models.healthlabs import HealthLabsAccessAudit
from mealie.db.models.recipe import RecipeModel
from mealie.db.models.recipe.recipe import HealthLabsRecipeRevision
from mealie.db.models.users import User
from mealie.lang.providers import Translator
from mealie.repos.all_repositories import get_repositories
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.healthlabs.recipe import (
    HealthLabsFoodOut,
    HealthLabsIngredientOut,
    HealthLabsInstruction,
    HealthLabsRecipeOut,
    HealthLabsRecipePage,
    HealthLabsRecipeRevisionOut,
    HealthLabsRecipeScope,
    HealthLabsRecipeWrite,
    HealthLabsUnitOut,
)
from mealie.schema.recipe.recipe import HealthLabsRecipeScope as NativeRecipeScope
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import RecipeAccessDenied, RecipeAccessPolicy
from mealie.services.healthlabs.nutrition_service import NutritionService
from mealie.services.recipe.recipe_service import RecipeService

AUTHORING_EXTRAS_KEY = "healthlabs.ephra"
PAGE_SIZE = 50
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
IMAGE_MIME_FORMATS = {
    "image/jpeg": ("JPEG", "jpg"),
    "image/png": ("PNG", "png"),
    "image/webp": ("WEBP", "webp"),
}


class HealthLabsRecipeNotFound(LookupError):
    pass


class HealthLabsInvalidImage(ValueError):
    pass


class RecipeApiService:
    def __init__(self, session: Session, user: PrivateUser, translator: Translator) -> None:
        self.session = session
        self.user = user
        self.policy = RecipeAccessPolicy(session, user)
        self.capabilities = self.policy.active_capabilities()
        self.repos = get_repositories(session, group_id=user.group_id, household_id=user.household_id)
        household = self.repos.households.get_one(user.household_id)
        if household is None:
            raise RuntimeError("Managed recipe household is missing")
        self.native = RecipeService(self.repos, user, household, translator)
        self.unfiltered_recipes = get_repositories(session, group_id=None, household_id=None).recipes
        self.nutrition = NutritionService(session)
        self._audit_pending = False

    def list_recipes(
        self,
        *,
        q: str | None = None,
        cursor: str | None = None,
        access_reason: str | None = None,
        break_glass_reason: str | None = None,
    ) -> HealthLabsRecipePage:
        conditions = self._visibility_conditions(break_glass_reason)
        query = sa.select(RecipeModel).where(sa.or_(*conditions)).options(*Recipe.loader_options())
        if q and (normalized := RecipeModel.normalize(q)):
            query = query.where(
                sa.or_(
                    RecipeModel.name_normalized.contains(normalized),
                    RecipeModel.description_normalized.contains(normalized),
                )
            )
        if cursor:
            try:
                query = query.where(RecipeModel.id > UUID(cursor))
            except ValueError as exc:
                raise ValueError("Invalid recipe cursor") from exc

        models = self.session.execute(query.order_by(RecipeModel.id).limit(PAGE_SIZE + 1)).unique().scalars().all()
        visible = models[:PAGE_SIZE]
        recipes: list[HealthLabsRecipeOut] = []
        for model in visible:
            recipe = Recipe.model_validate(model)
            self._authorize_read(recipe, access_reason, break_glass_reason)
            recipes.append(self.to_out(recipe))
        self._commit_audits()
        next_cursor = str(visible[-1].id) if len(models) > PAGE_SIZE else None
        return HealthLabsRecipePage(items=recipes, next_cursor=next_cursor)

    def get_recipe(
        self,
        recipe_id: UUID,
        *,
        access_reason: str | None = None,
        break_glass_reason: str | None = None,
    ) -> HealthLabsRecipeOut:
        recipe = self._get_any_recipe(recipe_id)
        self._authorize_read(recipe, access_reason, break_glass_reason)
        result = self.to_out(recipe)
        self._commit_audits()
        return result

    def get_revision(
        self,
        recipe_id: UUID,
        revision_id: UUID,
        *,
        access_reason: str | None = None,
        break_glass_reason: str | None = None,
    ) -> HealthLabsRecipeRevisionOut:
        current = self._get_any_recipe(recipe_id)
        self._authorize_read(current, access_reason, break_glass_reason)
        revision = self.session.scalar(
            sa.select(HealthLabsRecipeRevision).where(
                HealthLabsRecipeRevision.id == revision_id,
                HealthLabsRecipeRevision.recipe_id == recipe_id,
            )
        )
        if revision is None:
            raise HealthLabsRecipeNotFound(recipe_id)
        snapshot = Recipe.model_validate(revision.snapshot)
        result = HealthLabsRecipeRevisionOut(
            id=revision.id,
            recipe_id=revision.recipe_id,
            revision_number=revision.revision_number,
            content_sha256=revision.content_sha256,
            created_at=revision.created_at,
            recipe=self.to_out(snapshot, revision=revision),
        )
        self._commit_audits()
        return result

    def create_recipe(self, data: HealthLabsRecipeWrite) -> HealthLabsRecipeOut:
        scope = data.scope or HealthLabsRecipeScope.PERSONAL
        if scope == HealthLabsRecipeScope.CATALOGUE:
            self.policy.require(HealthLabsCapability.CATALOGUE_WRITE)
        else:
            self.policy.require(HealthLabsCapability.RECIPES_WRITE_OWN)
        native = self._native_write(data, scope=scope)
        cleaned = self.native.clean_recipe_dict(native.model_dump(mode="json", by_alias=False))
        created = self.native.create_one(Recipe.model_validate(cleaned))
        return self.to_out(created)

    def update_recipe(self, recipe_id: UUID, data: HealthLabsRecipeWrite) -> HealthLabsRecipeOut:
        current = self._get_mutation_target(recipe_id)
        current_scope = HealthLabsRecipeScope(current.healthlabs_scope.value)
        if data.scope is not None and data.scope != current_scope:
            raise RecipeAccessDenied("Recipe scope is immutable")
        native = self._native_write(data, scope=current_scope)
        native.settings = current.settings
        if "source_url" not in data.model_fields_set:
            native.org_url = current.org_url
        cleaned = self.native.clean_recipe_dict(native.model_dump(mode="json", by_alias=False))
        updated = self.native.update_one(recipe_id, Recipe.model_validate(cleaned))
        return self.to_out(updated)

    def delete_recipe(self, recipe_id: UUID) -> HealthLabsRecipeOut:
        current = self._get_mutation_target(recipe_id)
        result = self.to_out(current)
        self.native.delete_one(recipe_id)
        return result

    def update_recipe_image(self, recipe_id: UUID, data: bytes, content_type: str) -> HealthLabsRecipeOut:
        current = self._get_mutation_target(recipe_id)
        extension = self._validate_image(data, content_type)
        self.native.update_recipe_image(current.slug, data, extension)
        return self.to_out(self._get_any_recipe(recipe_id))

    def read_recipe_image(
        self,
        recipe_id: UUID,
        *,
        access_reason: str | None = None,
        break_glass_reason: str | None = None,
    ) -> bytes:
        recipe = self.get_recipe(
            recipe_id,
            access_reason=access_reason,
            break_glass_reason=break_glass_reason,
        )
        if not recipe.image:
            raise HealthLabsRecipeNotFound(recipe_id)
        image_path = Recipe.directory_from_id(recipe_id).joinpath("images", "original.webp")
        if not image_path.is_file():
            raise HealthLabsRecipeNotFound(recipe_id)
        return image_path.read_bytes()

    def delete_recipe_image(self, recipe_id: UUID) -> HealthLabsRecipeOut:
        current = self._get_mutation_target(recipe_id)
        self.native.delete_recipe_image(current.slug)
        return self.to_out(self._get_any_recipe(recipe_id))

    def to_out(
        self,
        recipe: Recipe,
        *,
        revision: HealthLabsRecipeRevision | None = None,
    ) -> HealthLabsRecipeOut:
        if recipe.id is None or recipe.name is None:
            raise ValueError("Persisted recipe is missing an id or name")
        if revision is None:
            revision = self.session.scalar(
                sa.select(HealthLabsRecipeRevision)
                .where(HealthLabsRecipeRevision.recipe_id == recipe.id)
                .order_by(HealthLabsRecipeRevision.revision_number.desc())
                .limit(1)
            )
        calculated_nutrition = self.nutrition.calculate_recipe(recipe, "OTHER")
        ingredients = [self._ingredient_out(item) for item in recipe.recipe_ingredient]
        instructions = [
            HealthLabsInstruction(
                title=step.title,
                summary=step.summary,
                text=step.text,
                ingredient_references=[
                    ref.reference_id for ref in step.ingredient_references if ref.reference_id is not None
                ],
            )
            for step in (recipe.recipe_instructions or [])
        ]
        owner_external_subject_id = None
        if (
            HealthLabsCapability.RECIPES_READ_ALL in self.capabilities
            and recipe.healthlabs_scope == NativeRecipeScope.PERSONAL
            and recipe.user_id is not None
        ):
            owner = self.session.get(User, recipe.user_id)
            owner_external_subject_id = owner.external_subject_id if owner is not None else None
        return HealthLabsRecipeOut(
            id=recipe.id,
            slug=recipe.slug,
            name=recipe.name,
            description=recipe.description,
            scope=HealthLabsRecipeScope(recipe.healthlabs_scope.value),
            revision_id=revision.id if revision else None,
            revision_number=revision.revision_number if revision else recipe.healthlabs_revision_number,
            copied_from_recipe_id=recipe.copied_from_recipe_id,
            copied_from_revision_id=recipe.copied_from_revision_id,
            copied_at=recipe.copied_at,
            owner_external_subject_id=owner_external_subject_id,
            servings=recipe.recipe_servings,
            yield_quantity=recipe.recipe_yield_quantity,
            yield_name=recipe.recipe_yield,
            total_time_seconds=recipe.total_time_seconds,
            prep_time_seconds=recipe.prep_time_seconds,
            perform_time_seconds=recipe.perform_time_seconds,
            source_url=recipe.org_url,
            image=recipe.image,
            ingredients=ingredients,
            instructions=instructions,
            authoring=self._decode_authoring(recipe.extras or {}),
            calculated_nutrition=calculated_nutrition,
        )

    def _native_write(self, data: HealthLabsRecipeWrite, *, scope: HealthLabsRecipeScope) -> Recipe:
        ingredients = [
            {
                "reference_id": str(item.reference_id),
                "quantity": item.quantity,
                "food": item.food.model_dump(mode="json", exclude_none=True) if item.food else None,
                "unit": item.unit.model_dump(mode="json", exclude_none=True) if item.unit else None,
                "note": item.note,
                "original_text": item.original_text,
                "title": item.title,
            }
            for item in data.ingredients
        ]
        instructions = [
            {
                "title": step.title,
                "summary": step.summary,
                "text": step.text,
                "ingredient_references": [
                    {"reference_id": str(reference_id)} for reference_id in step.ingredient_references
                ],
            }
            for step in data.instructions
        ]
        return Recipe(
            name=data.name,
            description=data.description,
            healthlabs_scope=NativeRecipeScope(scope.value),
            recipe_servings=data.servings,
            recipe_yield_quantity=data.yield_quantity,
            recipe_yield=data.yield_name,
            total_time_seconds=data.total_time_seconds,
            prep_time_seconds=data.prep_time_seconds,
            perform_time_seconds=data.perform_time_seconds,
            org_url=data.source_url,
            recipe_ingredient=ingredients,
            recipe_instructions=instructions,
            extras={
                AUTHORING_EXTRAS_KEY: json.dumps(
                    data.authoring,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            },
        )

    @staticmethod
    def _validate_image(data: bytes, content_type: str) -> str:
        expected = IMAGE_MIME_FORMATS.get(content_type.lower())
        if expected is None:
            raise HealthLabsInvalidImage("Unsupported image content type")
        expected_format, extension = expected
        try:
            with Image.open(BytesIO(data)) as image:
                actual_format = image.format
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise HealthLabsInvalidImage("Recipe image dimensions are too large")
                image.verify()
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
            raise HealthLabsInvalidImage("Invalid image data") from exc
        if actual_format != expected_format:
            raise HealthLabsInvalidImage("Image content does not match its content type")
        return extension

    def _ingredient_out(self, item) -> HealthLabsIngredientOut:
        nutrition = None
        if item.food is not None and item.food.id is not None:
            nutrition = self.nutrition.food_facts(
                item.food.id,
                item.unit.id if item.unit is not None else None,
                item.quantity,
            )
        food = None
        if item.food is not None and item.food.id is not None and item.food.name is not None:
            food = HealthLabsFoodOut(
                id=item.food.id,
                name=item.food.name,
                plural_name=item.food.plural_name,
                description=item.food.description or "",
                nutrition=nutrition.food_match if nutrition is not None else None,
            )
        unit = None
        if item.unit is not None and item.unit.id is not None:
            unit = HealthLabsUnitOut(
                id=item.unit.id,
                name=item.unit.name,
                plural_name=item.unit.plural_name,
                abbreviation=item.unit.abbreviation,
                plural_abbreviation=item.unit.plural_abbreviation or "",
            )
        return HealthLabsIngredientOut(
            reference_id=item.reference_id,
            quantity=item.quantity,
            food=food,
            unit=unit,
            note=item.note,
            original_text=item.original_text,
            title=item.title,
            nutrition=nutrition,
        )

    @staticmethod
    def _decode_authoring(extras: dict) -> dict[str, bool]:
        raw = extras.get(AUTHORING_EXTRAS_KEY)
        if not isinstance(raw, str):
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(flag, bool) for key, flag in value.items()
        ):
            return {}
        return value

    def _get_any_recipe(self, recipe_id: UUID) -> Recipe:
        recipe = self.unfiltered_recipes.get_one(recipe_id, "id")
        if recipe is None:
            raise HealthLabsRecipeNotFound(recipe_id)
        return recipe

    def _get_mutation_target(self, recipe_id: UUID) -> Recipe:
        recipe = self._get_any_recipe(recipe_id)
        if recipe.healthlabs_scope == NativeRecipeScope.CATALOGUE:
            self.policy.require(HealthLabsCapability.CATALOGUE_WRITE)
        else:
            if recipe.user_id != self.user.id:
                raise HealthLabsRecipeNotFound(recipe_id)
            self.policy.require(HealthLabsCapability.RECIPES_WRITE_OWN)
        return recipe

    def _visibility_conditions(self, break_glass_reason: str | None) -> Sequence[sa.ColumnElement[bool]]:
        conditions: list[sa.ColumnElement[bool]] = []
        if HealthLabsCapability.CATALOGUE_READ in self.capabilities:
            conditions.append(RecipeModel.healthlabs_scope == HealthLabsRecipeScope.CATALOGUE.value)
        if HealthLabsCapability.RECIPES_READ_OWN in self.capabilities:
            conditions.append(
                sa.and_(
                    RecipeModel.healthlabs_scope == HealthLabsRecipeScope.PERSONAL.value,
                    RecipeModel.user_id == self.user.id,
                )
            )
        if HealthLabsCapability.RECIPES_READ_ALL in self.capabilities:
            conditions.append(RecipeModel.healthlabs_scope == HealthLabsRecipeScope.PERSONAL.value)
        if self.user.admin:
            if not (break_glass_reason or "").strip():
                raise RecipeAccessDenied("Break-glass reason is required")
            conditions.append(sa.true())
        if not conditions:
            raise RecipeAccessDenied("No recipe read capability")
        return conditions

    def _authorize_read(
        self,
        recipe: Recipe,
        access_reason: str | None,
        break_glass_reason: str | None,
    ) -> None:
        if recipe.healthlabs_scope == NativeRecipeScope.CATALOGUE:
            if HealthLabsCapability.CATALOGUE_READ in self.capabilities:
                return
            if self.user.admin:
                self._record_cross_read(recipe, break_glass_reason, "recipes.read_break_glass")
                return
            raise HealthLabsRecipeNotFound(recipe.id)
        if recipe.user_id == self.user.id and HealthLabsCapability.RECIPES_READ_OWN in self.capabilities:
            return
        if HealthLabsCapability.RECIPES_READ_ALL in self.capabilities:
            self._record_cross_read(recipe, access_reason, "recipes.read_cross_subject")
            return
        if self.user.admin:
            self._record_cross_read(recipe, break_glass_reason, "recipes.read_break_glass")
            return
        raise HealthLabsRecipeNotFound(recipe.id)

    def _record_cross_read(self, recipe: Recipe, reason: str | None, action: str) -> None:
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise RecipeAccessDenied("Cross-subject access reason is required")
        owner = self.session.get(User, recipe.user_id)
        owner_subject = owner.external_subject_id if owner is not None else None
        pseudonym_source = owner_subject or str(recipe.user_id)
        pseudonym = "sha256:" + hashlib.sha256(pseudonym_source.encode()).hexdigest()
        self.session.add(
            HealthLabsAccessAudit(
                actor_user_id=self.user.id,
                subject_external_id=pseudonym,
                action=action,
                reason=clean_reason,
                resource_type="recipe",
                resource_id=str(recipe.id),
            )
        )
        self._audit_pending = True

    def _commit_audits(self) -> None:
        if self._audit_pending:
            self.session.commit()
            self._audit_pending = False
