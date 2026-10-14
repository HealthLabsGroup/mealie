from __future__ import annotations

import asyncio
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import HealthLabsImportDraft
from mealie.lang.providers import Translator
from mealie.pkgs import safehttp
from mealie.repos.all_repositories import get_repositories
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.healthlabs.recipe import (
    HealthLabsDraftIngredient,
    HealthLabsFoodIn,
    HealthLabsImportDraftOut,
    HealthLabsImportRecipeDraft,
    HealthLabsIngredientConfidence,
    HealthLabsInstruction,
    HealthLabsRecipeScope,
    HealthLabsUnitIn,
)
from mealie.schema.recipe.recipe_ingredient import RegisteredParser
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import RecipeAccessPolicy
from mealie.services.parser_services import get_parser
from mealie.services.scraper.recipe_scraper import RecipeScraper
from mealie.services.scraper.scraper_strategies import RecipeScraperPackage

IMPORT_MAX_BYTES = 2 * 1024 * 1024
IMPORT_MAX_REDIRECTS = 5
IMPORT_TIMEOUT_SECONDS = 20
IMPORT_RETENTION = timedelta(hours=24)
IMPORT_MIME_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "application/ld+json",
    }
)


class ImportDraftNotFound(LookupError):
    pass


class ImportDraftInvalidMime(ValueError):
    pass


class ImportDraftUnprocessable(ValueError):
    pass


class ImportDraftTimedOut(TimeoutError):
    pass


class ImportDraftService:
    def __init__(self, session: Session, user: PrivateUser, translator: Translator) -> None:
        self.session = session
        self.user = user
        self.translator = translator
        self.policy = RecipeAccessPolicy(session, user)
        self.repos = get_repositories(session, group_id=user.group_id, household_id=user.household_id)

    @staticmethod
    async def fetch_document(url: str) -> safehttp.FetchResult:
        result = await safehttp.resilient_fetch(
            url,
            timeout=min(IMPORT_TIMEOUT_SECONDS, safehttp.SCRAPER_TIMEOUT),
            allow_flaresolverr=False,
            max_bytes=IMPORT_MAX_BYTES,
            max_redirects=IMPORT_MAX_REDIRECTS,
        )
        if result is None:
            raise ImportDraftUnprocessable("Recipe source could not be fetched")
        content_type = result.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in IMPORT_MIME_TYPES:
            raise ImportDraftInvalidMime("Recipe source must be HTML or JSON")
        return result

    async def create(self, url: str) -> HealthLabsImportDraftOut:
        self.policy.require(HealthLabsCapability.RECIPES_WRITE_OWN)
        self._validate_url(url)
        try:
            async with asyncio.timeout(IMPORT_TIMEOUT_SECONDS):
                fetched = await self.fetch_document(url)
                content_type = fetched.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                document = fetched.text
                if content_type in {"application/json", "application/ld+json"}:
                    document = RecipeScraperPackage.ld_json_to_html(document)
                recipe, _ = await RecipeScraper(
                    self.repos,
                    self.translator,
                    scrapers=[RecipeScraperPackage],
                ).scrape(fetched.url, document)
                if recipe is None or not recipe.name:
                    raise ImportDraftUnprocessable("No structured recipe data was found")
                draft_recipe = await self._to_draft(recipe, fetched.url)
        except TimeoutError as exc:
            raise ImportDraftTimedOut("Recipe import timed out") from exc

        now = get_utc_now()
        draft = HealthLabsImportDraftOut(
            id=uuid4(),
            state="ready",
            recipe=draft_recipe,
            created_at=now,
            expires_at=now + IMPORT_RETENTION,
        )
        self.session.add(
            HealthLabsImportDraft(
                id=draft.id,
                actor_user_id=self.user.id,
                state=draft.state,
                payload=draft.model_dump(mode="json", by_alias=True),
                expires_at=draft.expires_at,
            )
        )
        self.session.commit()
        return draft

    def get(self, draft_id: UUID) -> HealthLabsImportDraftOut:
        self.policy.require(HealthLabsCapability.RECIPES_READ_OWN)
        row = self.session.scalar(
            sa.select(HealthLabsImportDraft).where(
                HealthLabsImportDraft.id == draft_id,
                HealthLabsImportDraft.actor_user_id == self.user.id,
            )
        )
        if row is None:
            raise ImportDraftNotFound(draft_id)
        if row.expires_at <= get_utc_now():
            self.session.delete(row)
            self.session.commit()
            raise ImportDraftNotFound(draft_id)
        return HealthLabsImportDraftOut.model_validate(row.payload)

    async def _to_draft(self, recipe, source_url: str) -> HealthLabsImportRecipeDraft:
        source_ingredients = list(recipe.recipe_ingredient or [])
        originals = [item.original_text or item.note or "" for item in source_ingredients]
        parsed = await get_parser(
            RegisteredParser.nlp,
            self.user.group_id,
            self.session,
            self.translator,
        ).parse(originals)

        ingredients: list[HealthLabsDraftIngredient] = []
        for source, parsed_item, original in zip(source_ingredients, parsed, originals, strict=True):
            ingredient = parsed_item.ingredient
            food = ingredient.food
            unit = ingredient.unit
            ingredients.append(
                HealthLabsDraftIngredient(
                    reference_id=ingredient.reference_id,
                    quantity=ingredient.quantity,
                    food=(
                        HealthLabsFoodIn(
                            id=getattr(food, "id", None),
                            name=food.name,
                            plural_name=getattr(food, "plural_name", None),
                            description=getattr(food, "description", "") or "",
                        )
                        if food is not None and food.name
                        else None
                    ),
                    unit=(
                        HealthLabsUnitIn(
                            id=getattr(unit, "id", None),
                            name=unit.name,
                            plural_name=getattr(unit, "plural_name", None),
                            abbreviation=getattr(unit, "abbreviation", "") or "",
                            plural_abbreviation=getattr(unit, "plural_abbreviation", "") or "",
                        )
                        if unit is not None and unit.name
                        else None
                    ),
                    note=ingredient.note,
                    original_text=original,
                    title=source.title,
                    confidence=HealthLabsIngredientConfidence(
                        average=parsed_item.confidence.average,
                        quantity=parsed_item.confidence.quantity,
                        unit=parsed_item.confidence.unit,
                        food=parsed_item.confidence.food,
                        comment=parsed_item.confidence.comment,
                    ),
                )
            )

        instructions = [
            HealthLabsInstruction(title=step.title, summary=step.summary, text=step.text)
            for step in (recipe.recipe_instructions or [])
        ]
        return HealthLabsImportRecipeDraft(
            name=recipe.name,
            description=recipe.description,
            scope=HealthLabsRecipeScope.PERSONAL,
            servings=recipe.recipe_servings,
            yield_quantity=recipe.recipe_yield_quantity,
            yield_name=recipe.recipe_yield,
            total_time_seconds=recipe.total_time_seconds,
            prep_time_seconds=recipe.prep_time_seconds,
            perform_time_seconds=recipe.perform_time_seconds,
            source_url=source_url,
            ingredients=ingredients,
            instructions=instructions,
        )

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ImportDraftUnprocessable("Recipe source must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ImportDraftUnprocessable("Recipe source URL must not contain credentials")


def map_import_exception(exc: Exception) -> tuple[int, str] | None:
    if isinstance(exc, ImportDraftNotFound):
        return 404, "Import draft not found"
    if isinstance(exc, ImportDraftTimedOut | safehttp.ForceTimeoutException):
        return 408, "Recipe import timed out"
    if isinstance(exc, safehttp.ResponseTooLargeError):
        return 413, "Recipe source is too large"
    if isinstance(exc, ImportDraftInvalidMime):
        return 415, str(exc)
    if isinstance(exc, httpx.TooManyRedirects | ImportDraftUnprocessable | safehttp.InvalidDomainError):
        return 422, str(exc)
    return None
