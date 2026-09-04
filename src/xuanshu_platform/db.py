from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from .config import settings
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
class Base(DeclarativeBase): pass
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False)
class WorkspaceInvitation(Base):
    __tablename__ = "workspace_invitations"
    __table_args__ = (UniqueConstraint("workspace_id", "invitee_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    invitee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20), default="crew")
    description: Mapped[str] = mapped_column(Text, default="")
    process: Mapped[str] = mapped_column(String(30), default="sequential")
    memory: Mapped[bool] = mapped_column(Boolean, default=False)
    planning: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Immutable runtime snapshot. The normalized relations above represent
    # the editable draft; this JSON document changes only on explicit publish.
    published_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    draft_revision: Mapped[int] = mapped_column(Integer, default=1)
    public_token: Mapped[str | None] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ApplicationInput(Base):
    __tablename__ = "application_inputs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    label: Mapped[str] = mapped_column(String(160), default="")
    input_type: Mapped[str] = mapped_column(String(30), default="text")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    multiple: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)

class ApplicationAgent(Base):
    __tablename__ = "application_agents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(240))
    goal: Mapped[str] = mapped_column(Text, default="")
    backstory: Mapped[str] = mapped_column(Text, default="")
    memory: Mapped[bool] = mapped_column(Boolean, default=False)
    reasoning: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    position_x: Mapped[int] = mapped_column(Integer, default=80)
    position_y: Mapped[int] = mapped_column(Integer, default=80)

class ApplicationTask(Base):
    __tablename__ = "application_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    task_key: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    expected_output: Mapped[str] = mapped_column(Text, default="")
    agent_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    node_type: Mapped[str] = mapped_column(String(30), default="task")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    position_x: Mapped[int] = mapped_column(Integer, default=430)
    position_y: Mapped[int] = mapped_column(Integer, default=80)

class ApplicationTaskDependency(Base):
    __tablename__ = "application_task_dependencies"
    __table_args__ = (UniqueConstraint("application_id", "task_key", "depends_on_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    task_key: Mapped[str] = mapped_column(String(120))
    depends_on_key: Mapped[str] = mapped_column(String(120))

class ApplicationAgentResource(Base):
    __tablename__ = "application_agent_resources"
    __table_args__ = (UniqueConstraint("application_id", "agent_key", "resource_type", "resource_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(120))
    resource_type: Mapped[str] = mapped_column(String(20))
    resource_id: Mapped[int] = mapped_column(Integer)
class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
class ModelProfile(Base):
    __tablename__ = "model_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(80), default="openai")
    model: Mapped[str] = mapped_column(String(160))
    model_type: Mapped[str] = mapped_column(String(30), default="chat")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=180)
    max_retries: Mapped[int] = mapped_column(Integer, default=5)
    thinking_mode: Mapped[str] = mapped_column(String(20), default="auto")
    thinking_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
class Plugin(Base):
    __tablename__ = "plugins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(80), default="tool")
    configuration: Mapped[dict] = mapped_column(JSONB, default=dict)
class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    embedding_model_id: Mapped[int] = mapped_column(ForeignKey("model_profiles.id"), index=True)
    parsing_strategy: Mapped[str] = mapped_column(String(30), default="auto")
    chunk_size: Mapped[int] = mapped_column(Integer, default=800)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=120)
    status: Mapped[str] = mapped_column(String(30), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
class KnowledgeFile(Base):
    __tablename__ = "knowledge_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    knowledge_base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    object_key: Mapped[str] = mapped_column(String(600), unique=True)
    content_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="processing")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    input_text: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="")
    events: Mapped[list] = mapped_column(JSONB, default=list)
    approval_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    worker_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(240), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class ApplicationConversation(Base):
    __tablename__ = "application_conversations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    history_summary: Mapped[str] = mapped_column(Text, default="")
    history_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class ExternalConversation(Base):
    """Conversation identity used by the token-authenticated public API.

    External callers do not have a local ``users`` row.  Their random
    ``user_id`` is therefore kept separate from authenticated application
    conversations while using the same Run history and runtime state shape.
    """

    __tablename__ = "external_conversations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    external_user_id: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    history_summary: Mapped[str] = mapped_column(Text, default="")
    history_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

class DesignSession(Base):
    __tablename__ = "design_sessions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="未命名智能体")
    kind: Mapped[str] = mapped_column(String(20), default="crew")
    stage: Mapped[str] = mapped_column(String(30), default="inputs")
    status: Mapped[str] = mapped_column(String(30), default="draft")
    messages: Mapped[list] = mapped_column(JSONB, default=list)
    proposal: Mapped[dict] = mapped_column(JSONB, default=dict)
    active_job: Mapped[dict] = mapped_column(JSONB, default=dict)
    history_summary: Mapped[str] = mapped_column(Text, default="")
    history_tokens: Mapped[int] = mapped_column(Integer, default=0)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE applications ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''"))
        await conn.execute(text("ALTER TABLE applications ADD COLUMN IF NOT EXISTS process VARCHAR(30) NOT NULL DEFAULT 'sequential'"))
        await conn.execute(text("ALTER TABLE applications ADD COLUMN IF NOT EXISTS memory BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE applications ADD COLUMN IF NOT EXISTS planning BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE applications ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE applications ADD COLUMN IF NOT EXISTS published_config JSONB NOT NULL DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE applications ADD COLUMN IF NOT EXISTS draft_revision INTEGER NOT NULL DEFAULT 1"))
        # Before application deletion removed its DesignSession rows, old
        # versions detached generated sessions by setting application_id to
        # NULL.  A generated session without an application cannot be opened
        # or resumed, so remove those historical tombstones at startup.
        await conn.execute(text("DELETE FROM design_sessions WHERE application_id IS NULL AND status = 'generated'"))
        await conn.execute(text('ALTER TABLE applications ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP'))
        await conn.execute(text('ALTER TABLE applications ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP'))
        await conn.execute(text('ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS application_id INTEGER REFERENCES applications(id)'))
        await conn.execute(text('CREATE INDEX IF NOT EXISTS ix_api_keys_application_id ON api_keys (application_id)'))
        await conn.execute(text('ALTER TABLE runs ADD COLUMN IF NOT EXISTS worker_id VARCHAR(160)'))
        await conn.execute(text('ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP'))
        await conn.execute(text('ALTER TABLE runs ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0'))
        await conn.execute(text('ALTER TABLE runs ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(240)'))
        await conn.execute(text('CREATE INDEX IF NOT EXISTS ix_runs_idempotency_key ON runs (idempotency_key)'))
        await conn.execute(text('''
            CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_application_idempotency
            ON runs (application_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
        '''))
        await conn.execute(text('ALTER TABLE runs ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(80)'))
        await conn.execute(text('CREATE INDEX IF NOT EXISTS ix_runs_conversation_id ON runs (conversation_id)'))
        await conn.execute(text('CREATE INDEX IF NOT EXISTS ix_runs_worker_id ON runs (worker_id)'))
        await conn.execute(text('CREATE INDEX IF NOT EXISTS ix_runs_heartbeat_at ON runs (heartbeat_at)'))
        await conn.execute(text('ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS temperature DOUBLE PRECISION'))
        await conn.execute(text("ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS model_type VARCHAR(30) NOT NULL DEFAULT 'chat'"))
        await conn.execute(text('ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS max_tokens INTEGER'))
        await conn.execute(text('ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER NOT NULL DEFAULT 180'))
        await conn.execute(text('ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 5'))
        await conn.execute(text("ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS thinking_mode VARCHAR(20) NOT NULL DEFAULT 'auto'"))
        await conn.execute(text('ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS thinking_effort VARCHAR(20)'))
        await conn.execute(text('ALTER TABLE skills ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1'))
        await conn.execute(text('ALTER TABLE skills ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP'))
        await conn.execute(text("UPDATE skills SET content = content - 'category' WHERE content ? 'category'"))
        await conn.execute(text("UPDATE plugins SET configuration = configuration - 'category' WHERE configuration ? 'category'"))
        await conn.execute(text('ALTER TABLE design_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP'))
        await conn.execute(text("ALTER TABLE design_sessions ADD COLUMN IF NOT EXISTS active_job JSONB NOT NULL DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE design_sessions ADD COLUMN IF NOT EXISTS history_summary TEXT NOT NULL DEFAULT ''"))
        await conn.execute(text("ALTER TABLE design_sessions ADD COLUMN IF NOT EXISTS history_tokens INTEGER NOT NULL DEFAULT 0"))
        # One editable application owns exactly one Composer conversation.
        # Preserve the latest session state and merge older transcripts before
        # adding the database-level uniqueness guarantee on existing installs.
        await conn.execute(text("""
            WITH ranked AS (
                SELECT id, application_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY application_id
                           ORDER BY updated_at DESC, created_at DESC, id DESC
                       ) AS rank
                FROM design_sessions
                WHERE application_id IS NOT NULL
            ), merged AS (
                SELECT ds.application_id,
                       jsonb_agg(entry.message ORDER BY ds.created_at, entry.ordinality) AS messages
                FROM design_sessions ds
                CROSS JOIN LATERAL jsonb_array_elements(
                    COALESCE(ds.messages, '[]'::jsonb)
                ) WITH ORDINALITY AS entry(message, ordinality)
                WHERE ds.application_id IS NOT NULL
                GROUP BY ds.application_id
            )
            UPDATE design_sessions keeper
            SET messages = merged.messages
            FROM ranked, merged
            WHERE keeper.id = ranked.id
              AND ranked.rank = 1
              AND merged.application_id = ranked.application_id
              AND EXISTS (
                  SELECT 1 FROM ranked duplicate
                  WHERE duplicate.application_id = ranked.application_id
                    AND duplicate.rank > 1
              )
        """))
        await conn.execute(text("""
            DELETE FROM design_sessions stale
            USING (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY application_id
                    ORDER BY updated_at DESC, created_at DESC, id DESC
                ) AS rank
                FROM design_sessions
                WHERE application_id IS NOT NULL
            ) ranked
            WHERE stale.id = ranked.id AND ranked.rank > 1
        """))
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_design_sessions_application_id
            ON design_sessions (application_id)
            WHERE application_id IS NOT NULL
        """))
        await conn.execute(text("ALTER TABLE application_conversations ADD COLUMN IF NOT EXISTS state JSONB NOT NULL DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE application_conversations ADD COLUMN IF NOT EXISTS history_summary TEXT NOT NULL DEFAULT ''"))
        await conn.execute(text("ALTER TABLE application_conversations ADD COLUMN IF NOT EXISTS history_tokens INTEGER NOT NULL DEFAULT 0"))
        await conn.execute(text("ALTER TABLE external_conversations ADD COLUMN IF NOT EXISTS history_summary TEXT NOT NULL DEFAULT ''"))
        await conn.execute(text("ALTER TABLE external_conversations ADD COLUMN IF NOT EXISTS history_tokens INTEGER NOT NULL DEFAULT 0"))
        columns = (await conn.execute(text("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND (table_name, column_name) IN (
                ('skills', 'content'), ('plugins', 'configuration'),
                ('runs', 'events'), ('runs', 'approval_payload'),
                ('design_sessions', 'messages'), ('design_sessions', 'proposal'),
                ('design_sessions', 'active_job')
              )
        """))).all()
        text_columns = {(row.table_name, row.column_name) for row in columns if row.data_type != 'jsonb'}
        if text_columns:
            await conn.execute(text("""
                CREATE OR REPLACE FUNCTION xuanshu_jsonb_or(value TEXT, fallback JSONB)
                RETURNS JSONB LANGUAGE plpgsql IMMUTABLE AS $$
                BEGIN
                    RETURN value::jsonb;
                EXCEPTION WHEN OTHERS THEN
                    RETURN fallback;
                END;
                $$
            """))
            conversions = {
                ('skills', 'content'): (
                    "ALTER TABLE skills ALTER COLUMN content DROP DEFAULT, "
                    "ALTER COLUMN content TYPE JSONB USING xuanshu_jsonb_or(content, jsonb_build_object('instructions', content)), "
                    "ALTER COLUMN content SET DEFAULT '{}'::jsonb"
                ),
                ('plugins', 'configuration'): (
                    "ALTER TABLE plugins ALTER COLUMN configuration DROP DEFAULT, "
                    "ALTER COLUMN configuration TYPE JSONB USING xuanshu_jsonb_or(configuration, '{}'::jsonb), "
                    "ALTER COLUMN configuration SET DEFAULT '{}'::jsonb"
                ),
                ('runs', 'events'): (
                    "ALTER TABLE runs ALTER COLUMN events DROP DEFAULT, "
                    "ALTER COLUMN events TYPE JSONB USING xuanshu_jsonb_or(events, '[]'::jsonb), "
                    "ALTER COLUMN events SET DEFAULT '[]'::jsonb"
                ),
                ('runs', 'approval_payload'): (
                    "ALTER TABLE runs ALTER COLUMN approval_payload DROP DEFAULT, "
                    "ALTER COLUMN approval_payload TYPE JSONB USING xuanshu_jsonb_or(approval_payload, '{}'::jsonb), "
                    "ALTER COLUMN approval_payload SET DEFAULT '{}'::jsonb"
                ),
                ('design_sessions', 'messages'): (
                    "ALTER TABLE design_sessions ALTER COLUMN messages DROP DEFAULT, "
                    "ALTER COLUMN messages TYPE JSONB USING xuanshu_jsonb_or(messages, '[]'::jsonb), "
                    "ALTER COLUMN messages SET DEFAULT '[]'::jsonb"
                ),
                ('design_sessions', 'proposal'): (
                    "ALTER TABLE design_sessions ALTER COLUMN proposal DROP DEFAULT, "
                    "ALTER COLUMN proposal TYPE JSONB USING xuanshu_jsonb_or(proposal, '{}'::jsonb), "
                    "ALTER COLUMN proposal SET DEFAULT '{}'::jsonb"
                ),
                ('design_sessions', 'active_job'): (
                    "ALTER TABLE design_sessions ALTER COLUMN active_job DROP DEFAULT, "
                    "ALTER COLUMN active_job TYPE JSONB USING xuanshu_jsonb_or(active_job, '{}'::jsonb), "
                    "ALTER COLUMN active_job SET DEFAULT '{}'::jsonb"
                ),
            }
            for column in text_columns:
                await conn.execute(text(conversions[column]))
            await conn.execute(text('DROP FUNCTION xuanshu_jsonb_or(TEXT, JSONB)'))
        await conn.execute(text("UPDATE runs SET conversation_id = NULLIF(approval_payload->>'conversation_id', '') WHERE conversation_id IS NULL"))
        await conn.execute(text("""
            INSERT INTO application_conversations
                (id, application_id, workspace_id, user_id, title, created_at, updated_at)
            SELECT r.conversation_id, r.application_id, a.workspace_id, w.owner_id,
                   LEFT(COALESCE(NULLIF((array_agg(r.input_text ORDER BY r.created_at))[1], ''), '历史对话'), 200),
                   MIN(r.created_at), MAX(r.created_at)
            FROM runs r
            JOIN applications a ON a.id = r.application_id
            JOIN workspaces w ON w.id = a.workspace_id
            WHERE r.conversation_id IS NOT NULL
            GROUP BY r.conversation_id, r.application_id, a.workspace_id, w.owner_id
            ON CONFLICT (id) DO NOTHING
        """))
