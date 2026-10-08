"""Record image-understanding capability on model connections."""
from alembic import op
import sqlalchemy as sa

revision = '0002_model_vision'
down_revision = '0001_schema_baseline'
branch_labels = None
depends_on = None


def upgrade():
    # Baseline creates current metadata for new installations.
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('model_profiles')}
    if 'supports_vision' not in columns:
        op.add_column('model_profiles', sa.Column('supports_vision', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('model_profiles', 'supports_vision')
