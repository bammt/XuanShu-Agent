"""Create the current schema and migrate legacy installations."""
from alembic import op
from xuanshu_platform.migrations import upgrade as upgrade_schema

revision = '0001_schema_baseline'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    upgrade_schema(op.get_bind())

def downgrade():
    raise RuntimeError('The initial XuanShu schema migration is irreversible')
