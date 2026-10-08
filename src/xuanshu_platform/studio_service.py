"""Studio chat and session operations behind the /api/studio routes.
"""
import json
import secrets
from .auth_deps import (
    workspace_member,
)
from .conversation import (
    budget_chat_messages,
)
from .db import (
    Application,
    DesignSession,
    SessionLocal,
    User,
)
from .services import (
    STUDIO_QUEUE,
    redis,
)
from .studio_jobs import (
    persist_studio_job_failure,
)
from .studio_proposals import (
    StudioChatIn,
    apply_studio_structured_patch,
    canonicalize_studio_proposal,
    has_meaningful_studio_proposal,
    is_conversation_only_proposal,
    is_legacy_conversation_only_session,
    mark_studio_conversation_only,
    obvious_conversation,
)
from datetime import (
    UTC,
    datetime,
)
from fastapi import (
    HTTPException,
    Response,
)
from sqlalchemy import (
    select,
)


async def _studio_chat_locked(body: StudioChatIn, x_workspace_id: int, user: User):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
    if await redis.exists(f'xuanshu:studio:deleted:{body.orchestration_id}'):
        raise HTTPException(410, '该编排会话已删除，请新建会话后继续')
    # Short social turns are answered without invoking the composer, but are
    # still persisted as part of the same server-owned conversation.
    direct_reply = obvious_conversation(body.message)
    if direct_reply:
        result = {'intent': 'conversation', 'phase': 'answered', 'job_id': '',
                  'reply': direct_reply}
        async with SessionLocal() as db:
            application = None
            row = None
            if str(body.orchestration_id).isdigit():
                application = await db.get(Application, int(body.orchestration_id))
                if application and application.workspace_id != x_workspace_id:
                    raise HTTPException(403, '应用不属于当前工作空间')
                if application:
                    row = await db.scalar(select(DesignSession).where(
                        DesignSession.application_id == application.id,
                        DesignSession.workspace_id == x_workspace_id,
                    ))
            if not row:
                row = await db.get(DesignSession, body.orchestration_id)
            if row and (row.workspace_id != x_workspace_id or (
                not row.application_id and row.user_id != user.id
            )):
                raise HTTPException(403, '编排会话不属于当前工作空间')
            if is_legacy_conversation_only_session(row):
                mark_studio_conversation_only(row)
            if row and (row.active_job or {}).get('status') in {'queued', 'planning'}:
                raise HTTPException(409, '当前编排会话仍有任务在运行，请等待完成后再发送')
            if not row:
                row = DesignSession(id=body.orchestration_id, workspace_id=x_workspace_id,
                                    user_id=user.id, application_id=application.id if application else None)
                db.add(row)
            elif application and row.application_id is None:
                row.application_id = application.id
            messages = list(row.messages or [])
            messages.extend([
                {'role': 'user', 'content': body.message},
                {'role': 'assistant', 'content': direct_reply},
            ])
            row.messages = messages
            _kept, row.history_summary, row.history_tokens = budget_chat_messages(
                messages, token_budget=1800,
            )
            if not has_meaningful_studio_proposal(row.proposal):
                mark_studio_conversation_only(row)
            if row.title == '未命名智能体':
                row.title = body.message.strip().splitlines()[0][:80]
            row.active_job = {'job_id': '', 'status': 'answered', 'result': result,
                              'requested_by_user_id': user.id,
                              'updated_at': datetime.now(UTC).replace(tzinfo=None).isoformat()}
            await db.commit()
        return result
    job_id = secrets.token_hex(8)
    pending = {'intent': 'orchestrate', 'phase': 'queued', 'job_id': job_id, 'reply': ''}
    async with SessionLocal() as db:
        application = None
        row = None
        if str(body.orchestration_id).isdigit():
            application = await db.get(Application, int(body.orchestration_id))
            if application and application.workspace_id != x_workspace_id:
                raise HTTPException(403, '应用不属于当前工作空间')
            if application:
                row = await db.scalar(select(DesignSession).where(
                    DesignSession.application_id == application.id,
                    DesignSession.workspace_id == x_workspace_id,
                ))
        if not row:
            row = await db.get(DesignSession, body.orchestration_id)
        if row and (row.workspace_id != x_workspace_id or (
            not row.application_id and row.user_id != user.id
        )):
            raise HTTPException(403, '编排会话不属于当前工作空间')
        if row and (row.active_job or {}).get('status') in {'queued', 'planning'}:
            raise HTTPException(409, '当前编排会话仍有任务在运行，请等待完成后再发送')
        if not row:
            row = DesignSession(id=body.orchestration_id, workspace_id=x_workspace_id,
                                user_id=user.id, application_id=application.id if application else None)
            db.add(row)
        elif application and row.application_id is None:
            row.application_id = application.id
        if is_legacy_conversation_only_session(row):
            mark_studio_conversation_only(row)
        messages = list(row.messages or [])
        database_history, row.history_summary, row.history_tokens = budget_chat_messages(
            messages, token_budget=1800,
        )
        stored_proposal = canonicalize_studio_proposal(row.proposal)
        if is_conversation_only_proposal(stored_proposal):
            stored_proposal = {}
        previous_kind = stored_proposal.get('recommended_kind') or stored_proposal.get('kind') or ''
        if body.action in {'confirm_stage', 'resolve_clarification', 'confirm_capabilities'}:
            request_proposal, architecture_changed = apply_studio_structured_patch(
                stored_proposal, body.proposal, body,
            )
        else:
            request_proposal = stored_proposal or canonicalize_studio_proposal(body.proposal)
            architecture_changed = False
        typed_confirmation = str(body.message or '').strip() in {
            '确认', '确认方案', '确认架构', '确认编排',
            '确认运行输入', '确认运行输入。', '确认编排架构。',
        }
        proposal_stage = str(request_proposal.get('stage') or '')
        if body.action == 'message' and typed_confirmation and proposal_stage in {'inputs', 'architecture'}:
            body = body.model_copy(update={
                'action': 'confirm_stage',
                'confirmation_stage': proposal_stage,
            })
            request_proposal, architecture_changed = apply_studio_structured_patch(
                stored_proposal, request_proposal, body,
            )
        request_proposal = canonicalize_studio_proposal(request_proposal)
        request = body.model_copy(update={
            'history': database_history,
            'proposal': request_proposal,
            'architecture_changed': architecture_changed,
            'previous_kind': previous_kind,
        })
        messages.extend([
            {'role': 'user', 'content': body.message},
            {'role': 'assistant', 'content': '', 'job_id': job_id},
        ])
        row.messages = messages
        row.active_job = {'job_id': job_id, 'status': 'queued', 'result': {},
                          'request': request.model_dump(),
                          'requested_by_user_id': user.id,
                          'updated_at': datetime.now(UTC).replace(tzinfo=None).isoformat()}
        row.status = 'draft'
        queue_session_id = row.id
        await db.commit()
    try:
        await redis.set(f'xuanshu:studio:job:{job_id}', json.dumps({**pending, '_owner_user_id': user.id}, ensure_ascii=False), ex=3600)
        await redis.lpush(STUDIO_QUEUE, queue_session_id)
    except Exception as exc:
        await persist_studio_job_failure(
            job_id, body.orchestration_id, x_workspace_id, user.id,
            f'任务队列暂时不可用：{exc}',
        )
        raise HTTPException(503, '任务队列暂时不可用，请稍后重试') from exc
    return pending


async def _delete_studio_session_locked(session_id: str, user: User):
    async with SessionLocal() as db:
        row = await db.get(DesignSession, session_id)
        if row and row.user_id != user.id:
            raise HTTPException(403, '无权删除该编排会话')
        # Set the tombstone before deleting the row so an in-flight worker
        # cannot recreate the session between the database delete and cleanup.
        await redis.set(f'xuanshu:studio:deleted:{session_id}', '1', ex=86400)
        if not row:
            return Response(status_code=204)
        await workspace_member(db, row.workspace_id, user.id)
        await db.delete(row)
        await db.commit()
    await redis.delete(f'xuanshu:composer-flow:{session_id}')
    return Response(status_code=204)
