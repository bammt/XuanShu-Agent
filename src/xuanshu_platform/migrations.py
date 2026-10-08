"""Database schema upgrade helpers used by Alembic and bootstrap."""
import asyncio
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from .db import Base


def upgrade(connection):
    """Create the current schema and apply safe additive compatibility changes."""
    Base.metadata.create_all(connection)
    statements = [
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS process VARCHAR(30) NOT NULL DEFAULT 'sequential'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS memory BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS planning BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS published_config JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS draft_revision INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS application_id INTEGER REFERENCES applications(id)",
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS worker_id VARCHAR(160)",
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP",
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(240)",
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(80)",
        "ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS temperature DOUBLE PRECISION",
        "ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS model_type VARCHAR(30) NOT NULL DEFAULT 'chat'",
        "ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS max_tokens INTEGER",
        "ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER NOT NULL DEFAULT 180",
        "ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 5",
        "ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS thinking_mode VARCHAR(20) NOT NULL DEFAULT 'auto'",
        "ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS thinking_effort VARCHAR(20)",
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE design_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE design_sessions ADD COLUMN IF NOT EXISTS active_job JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE design_sessions ADD COLUMN IF NOT EXISTS history_summary TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE design_sessions ADD COLUMN IF NOT EXISTS history_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE application_conversations ADD COLUMN IF NOT EXISTS state JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE application_conversations ADD COLUMN IF NOT EXISTS history_summary TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE application_conversations ADD COLUMN IF NOT EXISTS history_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE external_conversations ADD COLUMN IF NOT EXISTS history_summary TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE external_conversations ADD COLUMN IF NOT EXISTS history_tokens INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_api_keys_application_id ON api_keys (application_id)",
        "CREATE INDEX IF NOT EXISTS ix_runs_idempotency_key ON runs (idempotency_key)",
        "CREATE INDEX IF NOT EXISTS ix_runs_conversation_id ON runs (conversation_id)",
        "CREATE INDEX IF NOT EXISTS ix_runs_worker_id ON runs (worker_id)",
        "CREATE INDEX IF NOT EXISTS ix_runs_heartbeat_at ON runs (heartbeat_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_application_idempotency ON runs (application_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
        """
        CREATE OR REPLACE FUNCTION xuanshu_jsonb_or(value TEXT, fallback JSONB)
        RETURNS JSONB LANGUAGE plpgsql IMMUTABLE AS $$
        BEGIN
          RETURN value::jsonb;
        EXCEPTION WHEN OTHERS THEN
          RETURN fallback;
        END $$;
        """,
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='skills' AND column_name='content' AND data_type <> 'jsonb') THEN
            ALTER TABLE skills ALTER COLUMN content DROP DEFAULT, ALTER COLUMN content TYPE JSONB USING xuanshu_jsonb_or(content, jsonb_build_object('instructions', content)), ALTER COLUMN content SET DEFAULT '{}'::jsonb;
          END IF;
          IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='plugins' AND column_name='configuration' AND data_type <> 'jsonb') THEN
            ALTER TABLE plugins ALTER COLUMN configuration DROP DEFAULT, ALTER COLUMN configuration TYPE JSONB USING xuanshu_jsonb_or(configuration, '{}'::jsonb), ALTER COLUMN configuration SET DEFAULT '{}'::jsonb;
          END IF;
          IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='runs' AND column_name='events' AND data_type <> 'jsonb') THEN
            ALTER TABLE runs ALTER COLUMN events DROP DEFAULT, ALTER COLUMN events TYPE JSONB USING xuanshu_jsonb_or(events, '[]'::jsonb), ALTER COLUMN events SET DEFAULT '[]'::jsonb;
          END IF;
          IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='runs' AND column_name='approval_payload' AND data_type <> 'jsonb') THEN
            ALTER TABLE runs ALTER COLUMN approval_payload DROP DEFAULT, ALTER COLUMN approval_payload TYPE JSONB USING xuanshu_jsonb_or(approval_payload, '{}'::jsonb), ALTER COLUMN approval_payload SET DEFAULT '{}'::jsonb;
          END IF;
          IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='design_sessions' AND column_name='messages' AND data_type <> 'jsonb') THEN
            ALTER TABLE design_sessions ALTER COLUMN messages DROP DEFAULT, ALTER COLUMN messages TYPE JSONB USING xuanshu_jsonb_or(messages, '[]'::jsonb), ALTER COLUMN messages SET DEFAULT '[]'::jsonb;
          END IF;
          IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='design_sessions' AND column_name='proposal' AND data_type <> 'jsonb') THEN
            ALTER TABLE design_sessions ALTER COLUMN proposal DROP DEFAULT, ALTER COLUMN proposal TYPE JSONB USING xuanshu_jsonb_or(proposal, '{}'::jsonb), ALTER COLUMN proposal SET DEFAULT '{}'::jsonb;
          END IF;
          IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='design_sessions' AND column_name='active_job' AND data_type <> 'jsonb') THEN
            ALTER TABLE design_sessions ALTER COLUMN active_job DROP DEFAULT, ALTER COLUMN active_job TYPE JSONB USING xuanshu_jsonb_or(active_job, '{}'::jsonb), ALTER COLUMN active_job SET DEFAULT '{}'::jsonb;
          END IF;
        END $$;
        """,
        "DELETE FROM design_sessions WHERE application_id IS NULL AND status = 'generated'",
        """
        WITH ranked AS (
          SELECT id, application_id, ROW_NUMBER() OVER (
            PARTITION BY application_id ORDER BY updated_at DESC, created_at DESC, id DESC
          ) AS rank
          FROM design_sessions WHERE application_id IS NOT NULL
        ), merged AS (
          SELECT ds.application_id,
                 jsonb_agg(entry.message ORDER BY ds.created_at, entry.ordinality) AS messages
          FROM design_sessions ds
          CROSS JOIN LATERAL jsonb_array_elements(COALESCE(ds.messages, '[]'::jsonb))
            WITH ORDINALITY AS entry(message, ordinality)
          WHERE ds.application_id IS NOT NULL
          GROUP BY ds.application_id
        )
        UPDATE design_sessions keeper SET messages = merged.messages
        FROM ranked, merged
        WHERE keeper.id = ranked.id AND ranked.rank = 1
          AND merged.application_id = ranked.application_id
          AND EXISTS (SELECT 1 FROM ranked duplicate
                      WHERE duplicate.application_id = ranked.application_id AND duplicate.rank > 1)
        """,
        """
        DELETE FROM design_sessions stale USING (
          SELECT id, ROW_NUMBER() OVER (
            PARTITION BY application_id ORDER BY updated_at DESC, created_at DESC, id DESC
          ) AS rank
          FROM design_sessions WHERE application_id IS NOT NULL
        ) ranked WHERE stale.id = ranked.id AND ranked.rank > 1
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_design_sessions_application_id ON design_sessions (application_id) WHERE application_id IS NOT NULL",
        "DROP FUNCTION IF EXISTS xuanshu_jsonb_or(TEXT, JSONB)",
        "UPDATE skills SET content = content - 'category' WHERE content ? 'category'",
        "UPDATE plugins SET configuration = configuration - 'category' WHERE configuration ? 'category'",
        "UPDATE runs SET conversation_id = NULLIF(approval_payload->>'conversation_id', '') WHERE conversation_id IS NULL",
    ]
    for statement in statements:
        connection.execute(text(statement))


async def upgrade_async(connection=None):
    """Run tracked Alembic revisions without blocking the application loop."""
    config = Config(str(Path(__file__).resolve().parents[2] / 'alembic.ini'))
    await asyncio.to_thread(command.upgrade, config, 'head')
