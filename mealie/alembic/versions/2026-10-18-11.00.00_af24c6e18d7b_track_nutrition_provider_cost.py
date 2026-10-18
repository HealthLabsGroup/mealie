"""track nutrition provider cost

Revision ID: af24c6e18d7b
Revises: 9e13f4a6b8c0
Create Date: 2026-10-18 11:00:00.000000
"""

import sqlalchemy as sa

from alembic import op


revision = "af24c6e18d7b"
down_revision: str | None = "9e13f4a6b8c0"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("healthlabs_nutrition_provider_usage") as batch_op:
        for name in (
            "reserved_microusd",
            "spent_microusd",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
        ):
            batch_op.add_column(sa.Column(name, sa.Integer(), server_default="0", nullable=False))
        batch_op.create_check_constraint(
            "healthlabs_nutrition_provider_usage_reserved_nonnegative",
            "reserved_microusd >= 0",
        )
        batch_op.create_check_constraint(
            "healthlabs_nutrition_provider_usage_spent_nonnegative",
            "spent_microusd >= 0",
        )
        batch_op.create_check_constraint(
            "healthlabs_nutrition_provider_usage_tokens_nonnegative",
            "input_tokens >= 0 AND cached_input_tokens >= 0 AND output_tokens >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("healthlabs_nutrition_provider_usage") as batch_op:
        batch_op.drop_constraint("healthlabs_nutrition_provider_usage_tokens_nonnegative", type_="check")
        batch_op.drop_constraint("healthlabs_nutrition_provider_usage_spent_nonnegative", type_="check")
        batch_op.drop_constraint("healthlabs_nutrition_provider_usage_reserved_nonnegative", type_="check")
        for name in (
            "output_tokens",
            "cached_input_tokens",
            "input_tokens",
            "spent_microusd",
            "reserved_microusd",
        ):
            batch_op.drop_column(name)
