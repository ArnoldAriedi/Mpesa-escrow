"""baseline corrected escrow schema

This is the first migration for the project. It creates the current corrected
schema, including RESTRICT foreign keys for financial history. Existing
installations created with create_all should be backed up and then stamped at
this revision after running scripts/verify_schema.py.
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial_escrow_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # For a fresh database, create the complete schema from the application's
    # metadata. This keeps the migration aligned with the current model while
    # Alembic becomes the source of truth for subsequent changes.
    from escrow_models import Base
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade():
    from escrow_models import Base
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
