from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import HealthLabsNutritionProviderUsage

if TYPE_CHECKING:
    from mealie.core.settings.settings import AppSettings

_USAGE_KEYS = frozenset({"input_tokens", "cached_input_tokens", "output_tokens"})


@dataclass(frozen=True, slots=True)
class ProviderReservation:
    usage_date: date
    reserved_microusd: int
    requested_at: datetime


class NutritionProviderBudget:
    def __init__(
        self,
        session: Session,
        *,
        daily_request_limit: int,
        daily_budget_usd: Decimal,
        input_usd_per_million_tokens: Decimal,
        output_usd_per_million_tokens: Decimal,
        now: Callable[[], datetime] = get_utc_now,
    ) -> None:
        if daily_request_limit < 0:
            raise ValueError("daily_request_limit cannot be negative")
        daily_budget_usd = Decimal(str(daily_budget_usd))
        input_usd_per_million_tokens = Decimal(str(input_usd_per_million_tokens))
        output_usd_per_million_tokens = Decimal(str(output_usd_per_million_tokens))
        if min(daily_budget_usd, input_usd_per_million_tokens, output_usd_per_million_tokens) < 0:
            raise ValueError("provider cost settings cannot be negative")
        self.session = session
        self.daily_request_limit = daily_request_limit
        self.daily_budget_microusd = int((daily_budget_usd * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
        self.input_rate = input_usd_per_million_tokens
        self.output_rate = output_usd_per_million_tokens
        self.now = now

    @classmethod
    def from_settings(
        cls,
        session: Session,
        settings: AppSettings,
        *,
        now: Callable[[], datetime] = get_utc_now,
    ) -> NutritionProviderBudget:
        return cls(
            session,
            daily_request_limit=settings.HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT,
            daily_budget_usd=settings.HEALTHLABS_NUTRITION_PROVIDER_DAILY_BUDGET_USD,
            input_usd_per_million_tokens=settings.HEALTHLABS_NUTRITION_PROVIDER_INPUT_USD_PER_MILLION_TOKENS,
            output_usd_per_million_tokens=settings.HEALTHLABS_NUTRITION_PROVIDER_OUTPUT_USD_PER_MILLION_TOKENS,
            now=now,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.daily_request_limit and self.daily_budget_microusd and (self.input_rate or self.output_rate))

    def reserve(self, input_token_upper_bound: int, output_token_upper_bound: int) -> ProviderReservation | None:
        requested_at = self.now()
        usage = self.session.scalar(
            sa.select(HealthLabsNutritionProviderUsage).where(
                HealthLabsNutritionProviderUsage.usage_date == requested_at.date()
            )
        )
        if usage is None:
            values = {"usage_date": requested_at.date(), "request_count": 0}
            dialect = self.session.get_bind().dialect.name
            if dialect == "sqlite":
                self.session.execute(
                    sqlite_insert(HealthLabsNutritionProviderUsage)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[HealthLabsNutritionProviderUsage.usage_date])
                )
            elif dialect == "postgresql":
                self.session.execute(
                    postgresql_insert(HealthLabsNutritionProviderUsage)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[HealthLabsNutritionProviderUsage.usage_date])
                )
            else:
                self.session.add(HealthLabsNutritionProviderUsage(**values))
            self.session.flush()
            usage = self.session.scalar(
                sa.select(HealthLabsNutritionProviderUsage).where(
                    HealthLabsNutritionProviderUsage.usage_date == requested_at.date()
                )
            )
            if usage is None:
                raise RuntimeError("provider usage reservation was not created")

        amount = self.cost_microusd(input_token_upper_bound, output_token_upper_bound)
        reserved = self.session.execute(
            sa.update(HealthLabsNutritionProviderUsage)
            .where(
                HealthLabsNutritionProviderUsage.id == usage.id,
                HealthLabsNutritionProviderUsage.request_count < self.daily_request_limit,
                HealthLabsNutritionProviderUsage.spent_microusd
                + HealthLabsNutritionProviderUsage.reserved_microusd
                + amount
                <= self.daily_budget_microusd,
            )
            .values(
                request_count=HealthLabsNutritionProviderUsage.request_count + 1,
                reserved_microusd=HealthLabsNutritionProviderUsage.reserved_microusd + amount,
            )
        ).rowcount
        if reserved != 1:
            self.session.rollback()
            return None
        self.session.commit()
        return ProviderReservation(requested_at.date(), amount, requested_at)

    def record(self, reservation: ProviderReservation, usage: dict[str, int]) -> None:
        if set(usage) != _USAGE_KEYS or any(type(usage[key]) is not int for key in _USAGE_KEYS):
            raise ValueError("provider returned invalid token usage")
        input_tokens = usage["input_tokens"]
        cached_input_tokens = usage["cached_input_tokens"]
        output_tokens = usage["output_tokens"]
        if min(input_tokens, cached_input_tokens, output_tokens) < 0 or cached_input_tokens > input_tokens:
            raise ValueError("provider returned invalid token usage")
        actual = self.cost_microusd(input_tokens, output_tokens)
        if actual > reservation.reserved_microusd:
            raise RuntimeError("provider token usage exceeded its reserved cost")
        updated = self.session.execute(
            sa.update(HealthLabsNutritionProviderUsage)
            .where(
                HealthLabsNutritionProviderUsage.usage_date == reservation.usage_date,
                HealthLabsNutritionProviderUsage.reserved_microusd >= reservation.reserved_microusd,
            )
            .values(
                reserved_microusd=(HealthLabsNutritionProviderUsage.reserved_microusd - reservation.reserved_microusd),
                spent_microusd=HealthLabsNutritionProviderUsage.spent_microusd + actual,
                input_tokens=HealthLabsNutritionProviderUsage.input_tokens + input_tokens,
                cached_input_tokens=HealthLabsNutritionProviderUsage.cached_input_tokens + cached_input_tokens,
                output_tokens=HealthLabsNutritionProviderUsage.output_tokens + output_tokens,
            )
        ).rowcount
        if updated != 1:
            raise RuntimeError("provider usage reservation was not found")
        self.session.commit()

    def cost_microusd(self, input_tokens: int, output_tokens: int) -> int:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("provider token bounds cannot be negative")
        cost = input_tokens * self.input_rate + output_tokens * self.output_rate
        return int(cost.to_integral_value(rounding=ROUND_CEILING))
