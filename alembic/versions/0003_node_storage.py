"""Store workflow node IDs exactly as they appear in the document."""

from alembic import op
import sqlalchemy as sa


revision = '0003_node_storage'
down_revision = '0002_model_vision'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    statements = [
        "ALTER TABLE application_tasks ADD COLUMN IF NOT EXISTS task_key VARCHAR(120)",
        "ALTER TABLE application_tasks ADD COLUMN IF NOT EXISTS node_key VARCHAR(120)",
        "UPDATE application_tasks SET node_key = COALESCE(node_key, task_key) WHERE node_key IS NULL",
        "ALTER TABLE application_tasks ALTER COLUMN node_key SET NOT NULL",
        "ALTER TABLE application_tasks DROP COLUMN IF EXISTS task_key",
        "ALTER TABLE application_task_dependencies ADD COLUMN IF NOT EXISTS task_key VARCHAR(120)",
        "ALTER TABLE application_task_dependencies ADD COLUMN IF NOT EXISTS depends_on_key VARCHAR(120)",
        "ALTER TABLE application_task_dependencies ADD COLUMN IF NOT EXISTS node_key VARCHAR(120)",
        "ALTER TABLE application_task_dependencies ADD COLUMN IF NOT EXISTS depends_on_node_key VARCHAR(120)",
        "UPDATE application_task_dependencies SET node_key = COALESCE(node_key, task_key), depends_on_node_key = COALESCE(depends_on_node_key, depends_on_key) WHERE node_key IS NULL OR depends_on_node_key IS NULL",
        "ALTER TABLE application_task_dependencies ALTER COLUMN node_key SET NOT NULL",
        "ALTER TABLE application_task_dependencies ALTER COLUMN depends_on_node_key SET NOT NULL",
        "ALTER TABLE application_task_dependencies DROP CONSTRAINT IF EXISTS application_task_dependencies_application_id_task_key_depen_key",
        "ALTER TABLE application_task_dependencies DROP COLUMN IF EXISTS task_key",
        "ALTER TABLE application_task_dependencies DROP COLUMN IF EXISTS depends_on_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_application_task_dependencies_node_edge ON application_task_dependencies (application_id, node_key, depends_on_node_key)",
    ]
    for statement in statements:
        bind.execute(sa.text(statement))


def downgrade():
    raise RuntimeError('Node storage migration is irreversible')
