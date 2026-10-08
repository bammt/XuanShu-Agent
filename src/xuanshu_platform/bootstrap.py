"""One-shot production schema bootstrap used by Docker Compose."""
import asyncio
import logging

from sqlalchemy import select

from .config import validate_production_settings
from .db import (
    Application, ApplicationTask, ApplicationTaskDependency, DesignSession, SessionLocal, Skill,
    init_db,
)
from .persistence import read_application, write_application
from .services import materialize_application_resources, migrate_legacy_app_dir
from .studio_contracts import normalize_legacy_studio_references


DEFINITION_STORAGE_VERSION = 3


def _workflow_tasks(application: Application) -> list[dict]:
    config = application.config or {}
    draft = ((config.get('draft_sync') or {}).get('workflow') or {})
    tasks = draft.get('tasks') if isinstance(draft, dict) else None
    if isinstance(tasks, list) and tasks:
        return [item for item in tasks if isinstance(item, dict) and item.get('id')]
    published = application.published_config or {}
    tasks = published.get('tasks') if isinstance(published, dict) else None
    return [item for item in (tasks or []) if isinstance(item, dict) and item.get('id')]


def _match_storage_nodes(rows: list[ApplicationTask], target_tasks: list[dict]) -> dict[int, str]:
    """Match legacy relational rows to the document's stable node IDs once."""
    unused = list(target_tasks)
    matched: dict[int, str] = {}
    for row in rows:
        candidates = [item for item in unused if str(item.get('name') or '') == str(row.name or '')]
        if len(candidates) != 1:
            candidates = [item for item in unused if str(item.get('node_type') or 'task') == str(row.node_type or 'task')]
        if not candidates and len(unused) == len(rows):
            candidates = [unused[rows.index(row)]]
        if candidates:
            target = candidates[0]
            matched[row.id] = str(target['id'])
            unused.remove(target)
    return matched


async def migrate_application_node_storage() -> None:
    """Move every application relation to the IDs already used by its document.

    The old schema generated task_1/task_2 from relation order.  This migration
    makes the canvas document authoritative: Flow IDs remain agent_1/crew_1/
    node_1, while Crew tasks remain task_1/task_2 in their own local namespace.
    """
    async with SessionLocal() as db:
        applications = (await db.scalars(select(Application))).all()
        for application in applications:
            if (application.config or {}).get('node_storage_version') == 2:
                continue
            targets = _workflow_tasks(application)
            rows = (await db.scalars(select(ApplicationTask).where(
                ApplicationTask.application_id == application.id,
            ).order_by(ApplicationTask.id))).all()
            if not rows or len(rows) != len(targets):
                continue
            mapping = _match_storage_nodes(rows, targets)
            if len(mapping) != len(rows) or len(set(mapping.values())) != len(rows):
                continue
            old_to_new = {str(row.node_key): new for row in rows for new in [mapping[row.id]]}
            for row in rows:
                row.node_key = mapping[row.id]
            edges = (await db.scalars(select(ApplicationTaskDependency).where(
                ApplicationTaskDependency.application_id == application.id,
            ))).all()
            for edge in edges:
                edge.node_key = old_to_new.get(str(edge.node_key), str(edge.node_key))
                edge.depends_on_node_key = old_to_new.get(
                    str(edge.depends_on_node_key), str(edge.depends_on_node_key),
                )
            config = dict(application.config or {})
            config['node_storage_version'] = 2
            application.config = config
        await db.commit()


async def migrate_application_workspaces() -> None:
    async with SessionLocal() as db:
        applications = (await db.scalars(select(Application))).all()
        for application in applications:
            try:
                definition = await read_application(db, application)
                selected = {str(skill_id) for agent in definition.get('agents', []) for skill_id in agent.get('skills', [])}
                skill_rows = (await db.scalars(select(Skill).where(
                    Skill.workspace_id == application.workspace_id,
                    Skill.id.in_([int(skill_id) for skill_id in selected]) if selected else False,
                ))).all()
                root = migrate_legacy_app_dir(application.workspace_id, application.id, application.kind)
                materialize_application_resources(
                    root,{str(item.id):{'id':str(item.id),**(item.content or {})} for item in skill_rows},selected,
                    include_code=any(bool(agent.get('allow_code_execution')) for agent in definition.get('agents', [])),
                    refresh=True,
                )
            except Exception:
                logging.exception('failed to migrate application workspace %s', application.id)


def _normalize_session_proposals(session: DesignSession) -> None:
    """Rewrite persisted Studio proposal cards to the current contract once."""
    if isinstance(session.proposal, dict) and session.proposal:
        session.proposal = normalize_legacy_studio_references(
            dict(session.proposal), normalize_names=True,
        )
    messages = []
    for message in session.messages or []:
        item = dict(message) if isinstance(message, dict) else message
        if isinstance(item, dict) and isinstance(item.get('proposal'), dict):
            item['proposal'] = normalize_legacy_studio_references(
                dict(item['proposal']), normalize_names=True,
            )
        messages.append(item)
    session.messages = messages


async def migrate_application_definitions() -> None:
    """Convert existing application documents to the current contract once.

    This is intentionally a bootstrap migration. Runtime reads do not need to
    keep accepting old input names, legacy process fields, or old output
    envelopes after this version marker is written.
    """
    async with SessionLocal() as db:
        applications = (await db.scalars(select(Application))).all()
        for application in applications:
            if int((application.config or {}).get('definition_storage_version') or 0) >= DEFINITION_STORAGE_VERSION:
                continue
            try:
                document = await read_application(db, application)
                document = normalize_legacy_studio_references(document)
                await write_application(db, application, document)
                config = dict(application.config or {})
                config['definition_storage_version'] = DEFINITION_STORAGE_VERSION
                application.config = config
                if application.published_config:
                    snapshot = normalize_legacy_studio_references(
                        dict(application.published_config),
                    )
                    application.published_config = snapshot
                sessions = (await db.scalars(select(DesignSession).where(
                    DesignSession.application_id == application.id,
                ))).all()
                for session in sessions:
                    _normalize_session_proposals(session)
            except Exception:
                logging.exception('failed to migrate application definition %s', application.id)
        await db.commit()


async def bootstrap() -> None:
    await init_db()
    await migrate_application_node_storage()
    await migrate_application_definitions()
    await migrate_application_workspaces()


if __name__ == '__main__':
    validate_production_settings()
    asyncio.run(bootstrap())
