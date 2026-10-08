"""Application startup and shutdown.
"""
from .config import (
    settings,
    validate_production_settings,
)
from .db import (
    SessionLocal,
    User,
    Workspace,
    WorkspaceMember,
    init_db,
)
from .services import (
    ensure_bucket,
)
from contextlib import (
    asynccontextmanager,
)
from pwdlib import (
    PasswordHash,
)
from sqlalchemy import (
    select,
)


passwords = PasswordHash.recommended()


@asynccontextmanager
async def lifespan(app):
    validate_production_settings()
    await init_db()
    await ensure_bucket()
    async with SessionLocal() as db:
        if not (await db.scalar(select(User).where(User.username == settings.admin_username))):
            admin = User(username=settings.admin_username, password_hash=passwords.hash(settings.admin_password), is_admin=True)
            db.add(admin); await db.flush(); ws = Workspace(name="主工作空间", owner_id=admin.id); db.add(ws); await db.flush(); db.add(WorkspaceMember(workspace_id=ws.id, user_id=admin.id, can_edit=True)); await db.commit()
    yield
