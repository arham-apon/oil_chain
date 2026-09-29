"""remember the first-seen quantity / planned tick of each supply arrival (shortfall + delay detection)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE supply_arrivals ADD COLUMN first_quantity DOUBLE PRECISION")
    op.execute("ALTER TABLE supply_arrivals ADD COLUMN first_planned_tick INT")
    op.execute("UPDATE supply_arrivals SET first_quantity = quantity, first_planned_tick = planned_tick")


def downgrade() -> None:
    op.execute("ALTER TABLE supply_arrivals DROP COLUMN IF EXISTS first_planned_tick")
    op.execute("ALTER TABLE supply_arrivals DROP COLUMN IF EXISTS first_quantity")
