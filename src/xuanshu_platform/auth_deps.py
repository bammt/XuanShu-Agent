"""Authentication and workspace-membership dependencies used by HTTP routes.
"""
import jwt
from .config import (
    settings,
)
from .db import (
    SessionLocal,
    User,
    WorkspaceMember,
)
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from fastapi import (
    Depends,
    HTTPException,
)
from fastapi.security import (
    OAuth2PasswordBearer,
)
from sqlalchemy import (
    select,
)


oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def token_for(user: User):
    expires = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": str(user.id), "admin": user.is_admin, "exp": expires}, settings.jwt_secret, algorithm="HS256")


async def current_user(token: str = Depends(oauth2)):
    try: uid = int(jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["sub"])
    except Exception: raise HTTPException(401, "登录已失效")
    async with SessionLocal() as db:
        user = await db.get(User, uid)
        if not user: raise HTTPException(401, "用户不存在")
        return user


async def workspace_member(db,workspace_id:int,user_id:int,edit:bool=False):
    query=select(WorkspaceMember).where(WorkspaceMember.workspace_id==workspace_id,WorkspaceMember.user_id==user_id)
    if edit: query=query.where(WorkspaceMember.can_edit==True)
    member=await db.scalar(query)
    if not member: raise HTTPException(403,"需要工作空间编辑权限" if edit else "没有工作空间访问权限")
    return member
