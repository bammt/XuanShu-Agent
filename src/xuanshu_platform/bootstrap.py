"""One-shot production schema bootstrap used by Docker Compose."""
import asyncio
import logging

from sqlalchemy import select

from .config import validate_production_settings
from .db import Application, SessionLocal, Skill, init_db
from .persistence import read_application
from .services import materialize_application_resources, migrate_legacy_app_dir


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


async def bootstrap() -> None:
    await init_db()
    await migrate_application_workspaces()


if __name__ == '__main__':
    validate_production_settings()
    asyncio.run(bootstrap())
