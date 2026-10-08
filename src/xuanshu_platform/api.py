import ast, asyncio, base64, binascii, hashlib, io, json, logging, mimetypes, re, secrets, shutil
from datetime import UTC, datetime, timedelta
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse
from fastapi import Body, Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, field_validator
from pwdlib import PasswordHash
import jwt
from crewai import Agent
from sqlalchemy import delete, select, text
from sqlalchemy.orm import load_only
from sqlalchemy.exc import IntegrityError
from .run_events import encode_run_event, stream_run_frames
from .config import settings, validate_production_settings
from .db import (ApiKey, Application, ApplicationAgent, ApplicationAgentResource,
                 ApplicationConversation, ApplicationInput, ApplicationTask,
                 ApplicationTaskDependency, DesignSession, ExternalConversation,
                 KnowledgeBase, KnowledgeFile, ModelProfile, Plugin, Run,
                 SessionLocal, Skill, User, Workspace, WorkspaceInvitation,
                 WorkspaceMember, init_db)
from .persistence import read_application, read_published_application, write_application
from .crypto import decrypt_secret, encrypt_secret
from .resources import public_plugin_configuration, runtime_plugin_configuration, secure_plugin_configuration
from .services import KNOWLEDGE_QUEUE, RUN_QUEUE, STUDIO_QUEUE, app_dir, app_file_manifest, app_object_key, app_root_dir, app_session_dir, app_session_object_key, composer_dir, delete_app_file, delete_session_file, ensure_bucket, materialize_application_resources, minio, parse_skill_manifest, redis, relocate_app_root, remove_app_dir, remove_app_session, remove_composer_dir, remove_minio_prefix, remove_object_prefix, remove_workspace_dir, resolve_app_file, safe_name, safe_relative_path, store_upload, sync_app_file, sync_session_file, visible_app_files
from .schemas import ApplicationDefinition
from .composer import normalize_runtime_inputs, run_composer
from . import confirmations
from .capability_policy import apply_capability_policy
from .contracts import ensure_executable_contract, ensure_fixed_output_contracts, ensure_variable_contract, execution_graph, variable_contract_errors
from .naming import normalize_definition_names
from .conversation import (budget_chat_messages, budget_conversation_history,
                           conversation_lock, request_fingerprint)
from .knowledge import (chunks_for as knowledge_chunks, delete_collection as delete_knowledge_collection,
                        delete_file_vectors, embedding_config, extract_text as extract_knowledge_text,
                        ingest as ingest_knowledge)
from .model_runtime import kickoff_structured, parse_structured_output, profile_llm
from .runtime import execute_application
from .studio_contracts import (canonical_studio_variable_name, ensure_executable_design,
                               ensure_message_task_reference, ensure_stage_variable_contract,
                               normalize_studio_definition,
                               normalize_legacy_studio_references,
                               normalize_crew_execution_contract,
                               normalize_flow_crew_execution_contract,
                               normalize_studio_input_contract, preserve_confirmed_proposal,
                               )
from .studio_proposals import (  # noqa: F401  re-exported for callers and tests
    CANVAS_AGENT_ROW_GAP,
    CANVAS_AGENT_START_Y,
    CANVAS_COLUMN_GAP,
    CANVAS_TASK_Y,
    DRAFT_SYNC_FIELDS,
    REVISION_LOG_LIMIT,
    StudioChatIn,
    _LEGACY_GENERIC_ORCHESTRATION,
    _LEGACY_ORCHESTRATION_REQUEST,
    _STAGE_ORDER,
    _STUDIO_RETRY_MESSAGES,
    _compact_studio_message,
    _json_merge_patch,
    _legacy_generic_orchestration_request,
    _retry_original_request,
    _selected_studio_resource_ids,
    append_revision_log,
    apply_requested_crew_process,
    apply_studio_structured_patch,
    architecture_was_confirmed_in_history,
    bound_session_proposal,
    canonicalize_studio_proposal,
    capability_card,
    collect_architecture_constraints,
    compact_stage_history,
    discovery_preflight_complete,
    draft_sync_document,
    has_meaningful_studio_proposal,
    input_contract_needs_model_completion,
    is_conversation_only_proposal,
    is_legacy_conversation_only_session,
    is_studio_retry_message,
    latest_confirmed_crew_contract,
    lock_confirmed_stage_messages,
    mark_studio_conversation_only,
    missing_capability_requirements,
    normalize_capability_requirements,
    normalize_manual_changes,
    obvious_conversation,
    preflight_capability_card,
    ready_reply,
    recover_confirmed_architecture_after_constraint,
    remember_stage_summary,
    rewind_proposal,
    stage_after_input_confirmation,
    stage_summary,
    studio_composer_history,
    studio_discovery_history,
    studio_proposal,
    studio_workflow,
)
from .application_drafts import (  # noqa: F401  re-exported for callers and tests
    _resource_lookup,
    normalize_application_resources,
    persist_application_draft,
    plugin_document,
    skill_document,
    sync_design_sessions_for_application,
    validate_application_resources,
    validate_flow_tool_document,
    workflow_definition,
    workflow_document,
    workflow_name,
)
from .studio_jobs import (  # noqa: F401  re-exported for callers and tests
    StageRevisionDecision,
    complete_single_run_input_contract,
    extract_studio_attachment,
    persist_studio_job_failure,
    route_revision_decision,
    route_revision_stage,
    run_studio_job,
    studio_attachment_context,
    studio_composer_resources,
    studio_execution_resources,
    studio_model,
    studio_resources,
)
from .auth_deps import (  # noqa: F401  re-exported for callers and tests
    current_user,
    oauth2,
    token_for,
    workspace_member,
)
from .artifacts import (  # noqa: F401  re-exported for callers and tests
    artifact_documents,
    artifact_name,
    artifact_object_key,
    minio_download_response,
)
from .workspace_records import (  # noqa: F401  re-exported for callers and tests
    ALLOWED_PLUGIN_KINDS,
    delete_application_records,
    delete_workspace_records,
    design_session_document,
    knowledge_document,
    mark_design_sessions_deleted,
    model_usage_count,
    normalize_skill_package,
    overview_run_summary,
    public_model,
    resolve_design_session,
    resource_usage_count,
    user_document,
    validate_plugin_document,
)
from .run_service import (  # noqa: F401  re-exported for callers and tests
    RuntimeIntent,
    UPLOAD_ONLY_CHAT_MESSAGES,
    apply_waiting_chat_message,
    authenticated_run_for_user,
    budgeted_run_history,
    conversation_document,
    conversation_trace_document,
    conversation_workflow_bound,
    durable_attachment_payload,
    enqueue_application_run,
    input_is_supplied,
    is_upload_only_message,
    is_workflow_run,
    merge_run_inputs_into_conversation,
    nonempty_input_patch,
    owned_conversation,
    require_model_for_definition,
    require_latest_conversation_run,
    reset_run_for_manual_retry,
    route_runtime_message,
    run_document,
    run_event_response,
    should_route_runtime_turn,
    update_conversation_state,
    validate_run_contract,
)
from .external_api import (  # noqa: F401  re-exported for callers and tests
    external_application,
    external_conversation_document,
    external_conversation_for,
    external_key,
    external_run,
    external_run_document,
    external_create_run_response,
    external_upload_attachment,
    external_upload_metadata,
    is_new_conversation_command,
    public_conversation_document,
    require_conversation_identity,
    require_external_run_identity,
    resolve_public_application,
    resolve_public_run,
    run_external_user_id,
    store_external_upload,
    validate_external_key,
)
from .api_models import (  # noqa: F401  re-exported for callers and tests
    ApiKeyIn,
    ApprovalIn,
    ConversationCreateIn,
    DefaultModelIn,
    ExternalRunIn,
    InviteIn,
    MemberPermissionIn,
    PasswordResetIn,
    RunFeedbackIn,
    StudioSessionCreate,
    StudioSessionUpdate,
    UserIn,
    WorkflowRunIn,
    WorkspaceIn,
)
from .studio_service import (  # noqa: F401  re-exported for callers and tests
    _delete_studio_session_locked,
    _studio_chat_locked,
)
from .run_actions import (  # noqa: F401  re-exported for callers and tests
    _approve_run_locked,
    _delete_application_conversation_locked,
    _run_workflow_locked,
    _submit_run_feedback_locked,
)
from .external_actions import (  # noqa: F401  re-exported for callers and tests
    _external_approve_locked,
    _external_clear_conversation_locked,
    _external_create_run_locked,
    _public_run_locked,
)
from .app_lifecycle import (  # noqa: F401  re-exported for callers and tests
    lifespan,
    passwords,
)

app = FastAPI(title="玄枢 XuanShu API", version="0.1.0", lifespan=lifespan)




# These strings are local transcript labels used by older clients when the
# user submits files without typing a message. They are display text, not a
# value for the application's primary chat input.


# A DesignSession keeps the conversational stage and proposal, while the
# application tables keep the editable graph.  This compact projection is the
# bridge between them: it is intentionally limited to fields that can be
# changed on the canvas, so it never duplicates the full transcript or prompt
# text in every natural-language turn.


@app.get("/api/health")
async def health(response: Response):
    checks = {'postgres': False, 'redis': False, 'minio': False, 'qdrant': False, 'executor': False}
    try:
        async with SessionLocal() as db:
            await db.execute(text('SELECT 1'))
        checks['postgres'] = True
    except Exception:
        pass
    try:
        checks['redis'] = bool(await redis.ping())
    except Exception:
        pass
    try:
        checks['minio'] = await asyncio.to_thread(minio.bucket_exists, settings.minio_bucket)
    except Exception:
        pass
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2) as client:
            checks['qdrant'] = (await client.get(f'{settings.qdrant_url}/collections')).is_success
    except Exception:
        pass
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2) as client:
            result = await client.get(f'{settings.executor_url}/health')
            payload = result.json() if result.is_success else {}
            checks['executor'] = payload.get('status') == 'ok'
    except Exception:
        pass
    healthy = all(checks.values())
    if not healthy:
        response.status_code = 503
    return {'status': 'ok' if healthy else 'degraded', 'service': 'xuanshu', 'checks': checks}


@app.get("/api/overview")
async def overview(workspace_id:int,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db,workspace_id,user.id)
        applications=(await db.scalars(select(Application).where(Application.workspace_id==workspace_id))).all(); models=(await db.scalars(select(ModelProfile).where(ModelProfile.workspace_id==workspace_id))).all()
        app_ids=[x.id for x in applications]
        # The dashboard needs statuses and timestamps only. Loading the full
        # JSON event history here made a 35 MB response and blocked navigation.
        runs=(await db.scalars(select(Run).options(load_only(
            Run.id, Run.application_id, Run.conversation_id, Run.status,
            Run.approval_payload, Run.idempotency_key, Run.created_at,
        )).where(Run.application_id.in_(app_ids)).order_by(Run.created_at.desc()))).all() if app_ids else []
        conversations=(await db.scalars(select(ApplicationConversation).where(
            ApplicationConversation.workspace_id == workspace_id,
            ApplicationConversation.user_id == user.id,
        ))).all()
        workflows=[]
        for row in applications:
            definition = await read_application(db, row); workflows.append(workflow_document(row, definition))
    workflow_names = {row.id: row.name for row in applications}
    run_items = []
    conversation_ids = {str(row.id) for row in conversations}
    for row in runs:
        if row.conversation_id and str(row.conversation_id) not in conversation_ids:
            continue
        if not is_workflow_run(row):
            continue
        run_items.append(overview_run_summary(row, workflow_names))
    # Collapse turns only for dashboard counts; the detail trace endpoint still
    # returns every turn and its full events on demand.
    latest_by_conversation = {}
    for item in run_items:
        key = item['conversation_id'] or item['id']
        latest_by_conversation.setdefault(key, item)
    run_items = list(latest_by_conversation.values())
    return {'workflows':workflows,'runs':run_items,'models':[public_model(x) for x in models],
            'runtime':{'connected_apps':{'configured':bool(settings.crewai_platform_integration_token)}},
            'stats':{'workflows':len(applications),'published':sum(x.published for x in applications),
                     'runs':len(run_items),'successful':sum(x.get('status')=='completed' for x in run_items)}}

@app.post('/api/studio/chat')
async def studio_chat(body: StudioChatIn, x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    # Keep the distributed lock scoped to the owner as well as the draft id.
    # A reused id from another browser must not serialize unrelated users.
    async with conversation_lock(f'studio:{x_workspace_id}:{user.id}:{body.orchestration_id}', ttl=3600):
        return await _studio_chat_locked(body, x_workspace_id, user)


@app.get('/api/studio/jobs/{job_id}')
async def studio_job(job_id: str, user: User = Depends(current_user)):
    raw = await redis.get(f'xuanshu:studio:job:{job_id}')
    if raw and json.loads(raw).get('_owner_user_id') == user.id:
        document = json.loads(raw)
        document.pop('_owner_user_id', None)
        return document
    async with SessionLocal() as db:
        row = await db.scalar(select(DesignSession).where(
            DesignSession.active_job['job_id'].astext == job_id,
        ))
        if row:
            await workspace_member(db, row.workspace_id, user.id)
            active = dict(row.active_job or {})
            active.pop('request', None)
            return active.get('result') or {
                'intent': 'orchestrate', 'phase': active.get('status', 'queued'),
                'job_id': job_id, 'reply': '',
            }
    if not raw:
        raise HTTPException(404, '编排任务不存在或已过期')
    raise HTTPException(404, '编排任务不存在或已过期')

@app.post('/api/studio/jobs/{job_id}/retry')
async def retry_studio_job(job_id: str, user: User = Depends(current_user)):
    """Retry a failed Studio turn from the session's saved proposal state."""
    async with SessionLocal() as db:
        row = await db.scalar(select(DesignSession).where(
            DesignSession.active_job['job_id'].astext == job_id,
        ))
        if not row:
            # active_job always holds the latest turn, so an older job id is
            # not found here: only the most recent failure can be retried.
            raise HTTPException(409, '只能重试最新一轮失败的编排；之后已发送过新消息，请直接继续对话')
        await workspace_member(db, row.workspace_id, user.id, True)
        active = dict(row.active_job or {})
        if active.get('status') != 'failed':
            raise HTTPException(409, '只有失败的编排任务可以重试')
        request_data = dict(active.get('request') or {})
        if not request_data:
            raise HTTPException(409, '该失败任务没有可恢复的请求状态，请重新发送')
        if row.proposal:
            request_data['proposal'] = row.proposal
        request_data['orchestration_id'] = str(row.application_id or row.id)
        # Keep the structured architecture/generation cards.  The worker
        # applies its own bounded prompt history later; truncating here can
        # discard the confirmed topology while leaving only a bare retry turn.
        request_data['history'] = list(row.messages or [])
        request_data['action'] = 'retry'
        request = StudioChatIn.model_validate(request_data)
        new_job_id = secrets.token_hex(8)
        request_data = request.model_dump()
        row.active_job = {
            'job_id': new_job_id, 'status': 'queued', 'result': {},
            'request': request_data, 'requested_by_user_id': user.id,
            'updated_at': datetime.now(UTC).replace(tzinfo=None).isoformat(),
        }
        row.messages = list(row.messages or []) + [
            {'role': 'assistant', 'content': '', 'job_id': new_job_id}
        ]
        await db.commit()
        session_id = row.id
    pending = {'intent': 'orchestrate', 'phase': 'queued', 'job_id': new_job_id, 'reply': ''}
    await redis.set(f'xuanshu:studio:job:{new_job_id}',
                    json.dumps({**pending, '_owner_user_id': user.id}, ensure_ascii=False), ex=3600)
    await redis.lpush(STUDIO_QUEUE, session_id)
    return pending

@app.get('/api/studio/jobs/{job_id}/events')
async def studio_job_events(job_id: str, user: User = Depends(current_user)):
    key = f'xuanshu:studio:job:{job_id}'
    raw = await redis.get(key)
    if not raw or json.loads(raw).get('_owner_user_id') != user.id:
        async with SessionLocal() as db:
            row = await db.scalar(select(DesignSession).where(
                DesignSession.active_job['job_id'].astext == job_id,
            ))
            if not row:
                raise HTTPException(404, '编排任务不存在或已过期')
            await workspace_member(db, row.workspace_id, user.id)
    async def stream():
        cursor = 0
        while True:
            events = await redis.lrange(f'{key}:events', cursor, -1)
            for raw in events:
                cursor += 1
                event = json.loads(raw)
                yield f'id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                if event.get('type') in {'done', 'error'}:
                    return
            raw_state = await redis.get(key)
            if raw_state:
                state = json.loads(raw_state)
            else:
                async with SessionLocal() as db:
                    row = await db.scalar(select(DesignSession).where(
                        DesignSession.active_job['job_id'].astext == job_id,
                    ))
                    active = dict(row.active_job or {}) if row else {}
                    state = active.get('result') or {
                        'intent': 'orchestrate', 'phase': active.get('status', 'failed'),
                        'job_id': job_id, 'reply': '',
                    }
            if state.get('phase') in {'answered', 'awaiting_confirmation', 'ready', 'validated', 'failed'} and not events:
                yield f'data: {json.dumps({"type": "done", "response": state}, ensure_ascii=False)}\n\n'
                return
            yield ': keep-alive\n\n'
            await asyncio.sleep(.25)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.get('/api/studio/sessions')
async def studio_sessions(x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        rows = (await db.scalars(select(DesignSession).where(
            DesignSession.workspace_id == x_workspace_id,
            (DesignSession.application_id.is_not(None)) | (DesignSession.user_id == user.id),
        ).order_by(DesignSession.updated_at.desc()).limit(30))).all()
        return [design_session_document(row) for row in rows]

@app.post('/api/studio/sessions')
async def create_studio_session(body: StudioSessionCreate,
                                x_workspace_id: int = Header(alias='X-Workspace-Id'),
                                user: User = Depends(current_user)):
    kind = body.kind if body.kind in {'crew', 'flow'} else 'crew'
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        row = DesignSession(
            id=secrets.token_urlsafe(18),
            workspace_id=x_workspace_id,
            user_id=user.id,
            kind=kind,
            stage='discovery',
            status='draft',
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return design_session_document(row, detail=not bool(row.application_id))

@app.get('/api/studio/sessions/{session_id}/messages')
async def studio_session_messages(session_id: str, limit: int = 30, before: int = 0,
                                   user: User = Depends(current_user)):
    async with SessionLocal() as db:
        row = await resolve_design_session(db, session_id)
        if not row:
            raise HTTPException(404, '编排会话不存在')
        await workspace_member(db, row.workspace_id, user.id)
        if not row.application_id and row.user_id != user.id:
            raise HTTPException(404, '编排会话不存在')
        size = max(1, min(int(limit), 100))
        all_messages = list(row.messages or [])
        offset = max(0, min(int(before), len(all_messages)))
        end = len(all_messages) - offset
        start = max(0, end - size)
        page = all_messages[start:end]
        return {
            'session_id': row.id,
            'messages': [_compact_studio_message(item) for item in page],
            'has_more': start > 0,
            'next_before': len(all_messages) - start if start > 0 else None,
        }

@app.get('/api/studio/sessions/{session_id}')
async def studio_session(session_id: str, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        row = await resolve_design_session(db, session_id)
        if not row:
            raise HTTPException(404, '编排会话不存在')
        await workspace_member(db, row.workspace_id, user.id)
        if not row.application_id and row.user_id != user.id:
            raise HTTPException(404, '编排会话不存在')
        return design_session_document(row, detail=not bool(row.application_id))

@app.patch('/api/studio/sessions/{session_id}')
async def update_studio_session(session_id: str, body: StudioSessionUpdate,
                                user: User = Depends(current_user)):
    async with SessionLocal() as db:
        row = await resolve_design_session(db, session_id)
        if not row:
            raise HTTPException(404, '编排会话不存在')
        await workspace_member(db, row.workspace_id, user.id, True)
        if not row.application_id and row.user_id != user.id:
            raise HTTPException(404, '编排会话不存在')
        if (row.active_job or {}).get('status') in {'queued', 'planning'}:
            raise HTTPException(409, '当前编排会话仍有任务在运行')
        if body.proposal is not None:
            row.proposal = canonicalize_studio_proposal(body.proposal)
            row.stage = str(row.proposal.get('stage') or row.stage)
        if body.workflow:
            sync = draft_sync_document(body.workflow, body.manual_changes)
            proposal = canonicalize_studio_proposal(row.proposal)
            proposal.update(json.loads(json.dumps(sync['workflow'], ensure_ascii=False)))
            proposal['draft_sync'] = sync
            proposal['structure_confirmed'] = bool(body.workflow.get('structure_confirmed'))
            if proposal['structure_confirmed']:
                proposal['stage'] = 'generation'
                row.stage = 'generation'
                row.status = 'generated'
            row.proposal = canonicalize_studio_proposal(proposal)
        if body.kind in {'crew', 'flow'}:
            row.kind = body.kind
        if body.title is not None and body.title.strip():
            row.title = body.title.strip()[:200]
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        await db.refresh(row)
        return design_session_document(row, detail=True)

@app.delete('/api/studio/sessions/{session_id}', status_code=204)
async def delete_studio_session(session_id: str, user: User = Depends(current_user)):
    async with conversation_lock(f'studio-session:{session_id}', ttl=3600):
        return await _delete_studio_session_locked(session_id, user)


@app.post('/api/studio/attachments')
async def upload_studio_attachments(files: list[UploadFile] = File(...), x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    if len(files) > 8:
        raise HTTPException(400, '最多一次上传 8 个文件')
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
    await ensure_bucket()
    saved = []
    for upload in files:
        data = await upload.read()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f'{upload.filename or "文件"} 超过 {settings.max_upload_mb} MB')
        attachment_id = secrets.token_hex(8)
        filename = safe_name(upload.filename or 'attachment')
        path = composer_dir(user.id) / f'{attachment_id}-{filename}'
        path.write_bytes(data)
        key = f'composer/{user.id}/{attachment_id}/{filename}'
        minio.put_object(settings.minio_bucket, key, __import__('io').BytesIO(data), len(data), content_type=upload.content_type or 'application/octet-stream')
        metadata = {'id': attachment_id, 'name': upload.filename or filename, 'content_type': upload.content_type or 'application/octet-stream',
                    'size': len(data), 'path': str(path), 'workspace_id': x_workspace_id, 'user_id': user.id, 'minio_key': key}
        await redis.set(f'xuanshu:studio:attachment:{attachment_id}', json.dumps(metadata, ensure_ascii=False), ex=86400)
        saved.append({key: value for key, value in metadata.items() if key not in {'path', 'workspace_id', 'user_id', 'minio_key'}})
    return saved

@app.delete('/api/studio/attachments/{attachment_id}', status_code=204)
async def delete_studio_attachment(attachment_id: str, user: User = Depends(current_user)):
    key = f'xuanshu:studio:attachment:{attachment_id}'
    raw = await redis.get(key)
    if raw:
        metadata = json.loads(raw)
        if metadata.get('user_id') != user.id:
            raise HTTPException(403, '无权删除该附件')
        Path(metadata['path']).unlink(missing_ok=True)
        try:
            minio.remove_object(settings.minio_bucket, metadata['minio_key'])
        except Exception:
            pass
        await redis.delete(key)
    return Response(status_code=204)
@app.post("/api/auth/token")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    async with SessionLocal() as db: user = await db.scalar(select(User).where(User.username == form.username))
    if not user or not passwords.verify(form.password, user.password_hash): raise HTTPException(401, "用户名或密码错误")
    return {"access_token": token_for(user), "token_type":"bearer", "user":{"id":user.id,"username":user.username,"is_admin":user.is_admin}}
@app.get('/api/auth/me')
async def auth_me(user: User = Depends(current_user)):
    return {'id': user.id, 'username': user.username, 'is_admin': user.is_admin,
            'created_at': user.created_at.isoformat()}
@app.get("/api/workspaces")
async def workspaces(user: User = Depends(current_user)):
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Workspace, WorkspaceMember.can_edit)
            .join(WorkspaceMember)
            .where(WorkspaceMember.user_id == user.id)
        )).all()
        return [
            {
                "id": workspace.id,
                "name": workspace.name,
                "owner_id": workspace.owner_id,
                "can_edit": bool(can_edit or workspace.owner_id == user.id),
            }
            for workspace, can_edit in rows
        ]
@app.post("/api/workspaces")
async def create_workspace(body: WorkspaceIn, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        ws=Workspace(name=body.name, owner_id=user.id); db.add(ws); await db.flush(); db.add(WorkspaceMember(workspace_id=ws.id,user_id=user.id,can_edit=True)); await db.commit(); return {"id":ws.id,"name":ws.name}
@app.delete('/api/workspaces/{workspace_id}', status_code=204)
async def delete_workspace(workspace_id: int, user: User = Depends(current_user)):
    run_ids: list[str] = []
    async with SessionLocal() as db:
        workspace = await db.get(Workspace, workspace_id)
        if not workspace:
            return Response(status_code=204)
        if workspace.owner_id != user.id:
            raise HTTPException(403, '只有工作空间所有者可以删除工作空间')
        run_ids = await delete_workspace_records(db, workspace)
        await db.commit()
    if run_ids:
        await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
    return Response(status_code=204)
@app.post("/api/workspaces/{workspace_id}/members")
async def invite_member(workspace_id:int, body:InviteIn, user:User=Depends(current_user)):
    async with SessionLocal() as db:
        owner=await db.scalar(select(Workspace).where(Workspace.id==workspace_id,Workspace.owner_id==user.id)); target=await db.scalar(select(User).where(User.username==body.username))
        if not owner: raise HTTPException(403,"只有工作空间所有者可以邀请成员")
        if not target: raise HTTPException(404,"用户不存在")
        existing=await db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id==workspace_id,WorkspaceMember.user_id==target.id))
        if existing: existing.can_edit=body.can_edit; await db.commit(); return {"username":target.username,"can_edit":body.can_edit,"status":"member"}
        invitation=await db.scalar(select(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id==workspace_id,WorkspaceInvitation.invitee_id==target.id))
        if invitation: invitation.can_edit=body.can_edit; invitation.status="pending"; invitation.inviter_id=user.id
        else: db.add(WorkspaceInvitation(workspace_id=workspace_id,inviter_id=user.id,invitee_id=target.id,can_edit=body.can_edit))
        await db.commit(); return {"username":target.username,"can_edit":body.can_edit,"status":"pending"}
@app.get('/api/workspaces/{workspace_id}/members')
async def workspace_members(workspace_id:int,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db,workspace_id,user.id);ws=await db.get(Workspace,workspace_id)
        memberships=(await db.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id==workspace_id))).all();result=[]
        for item in memberships:
            account=await db.get(User,item.user_id);result.append({'user_id':account.id,'username':account.username,'is_owner':account.id==ws.owner_id,'can_edit':item.can_edit})
        return {'owner_id':ws.owner_id,'can_manage':ws.owner_id==user.id,'members':result}
@app.get('/api/workspaces/{workspace_id}/invite-candidates')
async def workspace_invite_candidates(workspace_id:int,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        ws=await db.get(Workspace,workspace_id)
        if not ws or ws.owner_id!=user.id: raise HTTPException(403,'只有工作空间所有者可以邀请成员')
        member_ids=select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id==workspace_id)
        rows=(await db.scalars(select(User).where(User.id.not_in(member_ids)).order_by(User.username))).all()
        return [{'id':row.id,'username':row.username} for row in rows]
@app.put('/api/workspaces/{workspace_id}/members/{member_user_id}')
async def update_member_permission(workspace_id:int,member_user_id:int,body:MemberPermissionIn,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        ws=await db.get(Workspace,workspace_id)
        if not ws or ws.owner_id!=user.id: raise HTTPException(403,'只有工作空间所有者可以修改权限')
        if member_user_id==ws.owner_id: raise HTTPException(422,'不能修改所有者权限')
        member=await db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id==workspace_id,WorkspaceMember.user_id==member_user_id))
        if not member: raise HTTPException(404,'成员不存在')
        member.can_edit=body.can_edit;await db.commit();return {'user_id':member_user_id,'can_edit':member.can_edit}
@app.get("/api/invitations")
async def invitations(user:User=Depends(current_user)):
    async with SessionLocal() as db:
        rows=(await db.scalars(select(WorkspaceInvitation).where(WorkspaceInvitation.invitee_id==user.id,WorkspaceInvitation.status=="pending"))).all(); result=[]
        for x in rows:
            ws=await db.get(Workspace,x.workspace_id); inviter=await db.get(User,x.inviter_id); result.append({"id":x.id,"workspace_id":ws.id,"workspace_name":ws.name,"inviter":inviter.username,"can_edit":x.can_edit})
        return result
@app.post("/api/invitations/{invitation_id}/{decision}")
async def decide_invitation(invitation_id:int,decision:str,user:User=Depends(current_user)):
    if decision not in {"accept","reject"}: raise HTTPException(422,"决定必须是 accept 或 reject")
    async with SessionLocal() as db:
        invite=await db.get(WorkspaceInvitation,invitation_id)
        if not invite or invite.invitee_id!=user.id or invite.status!="pending": raise HTTPException(404,"邀请不存在")
        invite.status="accepted" if decision=="accept" else "rejected"
        if decision=="accept": db.add(WorkspaceMember(workspace_id=invite.workspace_id,user_id=user.id,can_edit=invite.can_edit))
        await db.commit(); return {"id":invite.id,"status":invite.status}


@app.post("/api/admin/users")
async def create_user(body: UserIn, user: User = Depends(current_user)):
    if not user.is_admin: raise HTTPException(403, "仅 admin 可创建账号")
    async with SessionLocal() as db:
        if await db.scalar(select(User).where(User.username == body.username)): raise HTTPException(409, "用户名已存在")
        row=User(username=body.username,password_hash=passwords.hash(body.password)); db.add(row); await db.flush()
        workspace = Workspace(name=f"{row.username} 的工作空间", owner_id=row.id); db.add(workspace); await db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=row.id, can_edit=True))
        await db.commit(); return user_document(row, workspace.id)
@app.get('/api/admin/users')
async def admin_users(user:User=Depends(current_user)):
    if not user.is_admin: raise HTTPException(403,'仅 admin 可查看账号')
    async with SessionLocal() as db:
        rows=(await db.scalars(select(User).order_by(User.created_at))).all();return [user_document(x) for x in rows]
@app.post('/api/admin/users/{user_id}/reset-password', status_code=204)
async def reset_user_password(user_id: int, body: PasswordResetIn, user: User = Depends(current_user)):
    if not user.is_admin: raise HTTPException(403, '仅 admin 可重置密码')
    async with SessionLocal() as db:
        target = await db.get(User, user_id)
        if not target: raise HTTPException(404, '账号不存在')
        target.password_hash = passwords.hash(body.password)
        await db.commit()
    return Response(status_code=204)
@app.delete('/api/admin/users/{user_id}', status_code=204)
async def delete_user(user_id: int, user: User = Depends(current_user)):
    if not user.is_admin: raise HTTPException(403, '仅 admin 可删除账号')
    if user_id == user.id: raise HTTPException(409, '不能删除当前登录的管理员账号')
    run_ids: list[str] = []
    async with SessionLocal() as db:
        target = await db.get(User, user_id)
        if not target: return Response(status_code=204)
        if target.is_admin: raise HTTPException(409, '不能删除其他管理员账号')
        owned = (await db.scalars(select(Workspace).where(Workspace.owner_id == target.id))).all()
        for workspace in owned:
            run_ids.extend(await delete_workspace_records(db, workspace))
        await db.execute(delete(WorkspaceInvitation).where(
            (WorkspaceInvitation.inviter_id == target.id) | (WorkspaceInvitation.invitee_id == target.id)))
        await db.execute(delete(ApplicationConversation).where(ApplicationConversation.user_id == target.id))
        await db.execute(delete(DesignSession).where(DesignSession.user_id == target.id))
        await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == target.id))
        await db.delete(target)
        await db.commit()
    remove_object_prefix(f'composer/{user_id}/')
    remove_composer_dir(user_id)
    if run_ids:
        await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
    return Response(status_code=204)
@app.get('/api/workflows')
async def list_workflows(x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        rows = (await db.scalars(
            select(Application)
            .where(Application.workspace_id == x_workspace_id)
            .order_by(Application.created_at.desc(), Application.id.desc())
        )).all()
        return [workflow_document(row, await read_application(db, row)) for row in rows]

@app.get('/api/workflows/{workflow_id}')
async def get_workflow(workflow_id: int, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        row = await db.get(Application, workflow_id)
        if not row:
            raise HTTPException(404, '应用不存在')
        await workspace_member(db, row.workspace_id, user.id)
        document = workflow_document(row, await read_application(db, row))
        session = await db.scalar(select(DesignSession).where(
            DesignSession.application_id == row.id,
        ))
        document['studio_session'] = (
            design_session_document(session, detail=False) if session else None
        )
        return document


@app.get('/api/workflows/{workflow_id}/runtime')
async def get_runtime_workflow(workflow_id: int, user: User = Depends(current_user)):
    """Return only the immutable definition used by the live run page."""
    async with SessionLocal() as db:
        row = await db.get(Application, workflow_id)
        if not row:
            raise HTTPException(404, '应用不存在')
        await workspace_member(db, row.workspace_id, user.id)
        if not row.published:
            raise HTTPException(409, '应用尚未发布')
        definition = await read_published_application(db, row)
    resources = await studio_execution_resources(row.workspace_id)
    document = workflow_document(row, definition)
    document['selected_resource_details'] = resources
    return document

@app.post('/api/workflows')
async def save_workflow(document: dict = Body(...), x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    manual_changes = normalize_manual_changes(document.get('_manual_changes'))
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        raw_id = document.get('id')
        row = await db.get(Application, int(raw_id)) if str(raw_id or '').isdigit() else None
        session = None
        if not str(raw_id or '').isdigit() and raw_id:
            session = await db.get(DesignSession, str(raw_id))
            if session and session.workspace_id == x_workspace_id and session.user_id == user.id and session.application_id:
                row = await db.get(Application, session.application_id)
        if session and (session.workspace_id != x_workspace_id or session.user_id != user.id):
            raise HTTPException(403, '编排会话不属于当前工作空间')
        expected_revision = document.get('_base_revision')
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, '草稿版本号无效') from exc
        try:
            row, result = await persist_application_draft(
                db,
                x_workspace_id,
                document,
                application=row,
                session=session,
                manual_changes=manual_changes,
                expected_revision=expected_revision,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(422, f'应用编排无效：{exc}') from exc
        await db.commit()
        await db.refresh(row)
        return result


@app.post('/api/workflows/{workflow_id}/publish')
async def publish_workflow(workflow_id: int, user: User = Depends(current_user)):
    """Promote the current draft to the immutable runtime definition."""
    async with SessionLocal() as db:
        row = await db.get(Application, workflow_id)
        if not row:
            raise HTTPException(404, '应用不存在')
        await workspace_member(db, row.workspace_id, user.id, True)
        definition = await read_application(db, row)
        definition['inputs'] = normalize_studio_input_contract(
            definition.get('inputs', []), definition.get('interaction_mode'),
        )
        normalize_studio_definition(definition)
        if not definition.get('tasks') or (row.kind == 'crew' and not definition.get('agents')):
            raise HTTPException(422, '应用至少需要一个 Agent 和可执行 Task')
        ensure_message_task_reference(definition)
        ensure_fixed_output_contracts(definition)
        try:
            ensure_executable_contract(definition)
        except ValueError as exc:
            raise HTTPException(422, f'应用编排无效，无法发布：{exc}') from exc
        definition = await normalize_application_resources(db, row.workspace_id, definition)
        try:
            validated = ApplicationDefinition.model_validate(definition)
        except Exception as exc:
            raise HTTPException(422, f'应用编排无效：{exc}') from exc
        await validate_application_resources(db, row.workspace_id, validated)
        row.published_config = definition
        row.published = True
        if not row.public_token:
            row.public_token = secrets.token_urlsafe(24)
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        await db.refresh(row)
        return workflow_document(row, definition)

@app.delete('/api/workflows/{workflow_id}', status_code=204)
async def delete_workflow(workflow_id: int, user: User = Depends(current_user)):
    run_ids: list[str] = []
    async with SessionLocal() as db:
        row = await db.get(Application, workflow_id)
        if not row:
            return Response(status_code=204)
        await workspace_member(db, row.workspace_id, user.id, True)
        remove_minio_prefix(row.workspace_id, row.id)
        remove_app_dir(row.workspace_id, row.id, row.kind)
        session_ids = list((await db.scalars(select(DesignSession.id).where(
            DesignSession.application_id == row.id,
        ))).all())
        await mark_design_sessions_deleted(session_ids)
        _, _, run_ids = await delete_application_records(db, row)
    if run_ids:
        await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
    return Response(status_code=204)


@app.get('/api/workflows/{workflow_id}/conversations')
async def list_application_conversations(workflow_id: int, preview: bool = False, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        app_row = await db.get(Application, workflow_id)
        if not app_row:
            raise HTTPException(404, '应用不存在')
        await workspace_member(db, app_row.workspace_id, user.id)
        rows = (await db.scalars(select(ApplicationConversation).where(
            ApplicationConversation.application_id == workflow_id,
            ApplicationConversation.user_id == user.id,
        ).order_by(ApplicationConversation.updated_at.desc()))).all()
        return [conversation_document(row) for row in rows
                if bool((row.state or {}).get('preview')) == preview]

@app.post('/api/workflows/{workflow_id}/conversations')
async def create_application_conversation(workflow_id: int, body: ConversationCreateIn | None = Body(default=None), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        app_row = await db.get(Application, workflow_id)
        if not app_row:
            raise HTTPException(404, '应用不存在')
        await workspace_member(db, app_row.workspace_id, user.id)
        preview = bool(body and body.preview)
        if not app_row.published and not preview:
            raise HTTPException(409, '应用尚未发布，不能创建运行会话')
        row = ApplicationConversation(
            id=secrets.token_urlsafe(12), application_id=app_row.id,
            workspace_id=app_row.workspace_id, user_id=user.id,
            state={'preview': True} if preview else {},
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return conversation_document(row)

@app.get('/api/workflows/{workflow_id}/conversations/{conversation_id}')
async def get_application_conversation(workflow_id: int, conversation_id: str,
                                       preview: bool = False, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        app_row = await db.get(Application, workflow_id)
        if not app_row:
            raise HTTPException(404, '应用不存在')
        await workspace_member(db, app_row.workspace_id, user.id)
        row = await owned_conversation(db, conversation_id, app_row, user.id)
        if bool((row.state or {}).get('preview')) != preview:
            raise HTTPException(404, '对话不存在')
        runs = (await db.scalars(select(Run).where(
            Run.application_id == app_row.id,
            Run.conversation_id == conversation_id,
        ).order_by(Run.created_at))).all()
        return conversation_document(row, list(runs), app_row)

@app.delete('/api/workflows/{workflow_id}/conversations/{conversation_id}', status_code=204)
async def delete_application_conversation(workflow_id: int, conversation_id: str,
                                          user: User = Depends(current_user)):
    async with conversation_lock(conversation_id):
        return await _delete_application_conversation_locked(workflow_id, conversation_id, user)


        # File inputs are always lists.  ``multiple`` is retained only for
        # reading old definitions and is no longer a user-facing switch.


@app.post('/api/workflows/{workflow_id}/run')
async def run_workflow(
    workflow_id: int,
    body: WorkflowRunIn,
    user: User = Depends(current_user),
    idempotency_header: str | None = Header(default=None, alias='Idempotency-Key'),
):
    conversation_id = str(body.conversation_id or secrets.token_urlsafe(12))
    idempotency_key = str(idempotency_header or body.idempotency_key or secrets.token_urlsafe(18))
    async with conversation_lock(conversation_id):
        return await _run_workflow_locked(
            workflow_id, body, user, conversation_id, idempotency_key,
        )

@app.get('/api/runs')
async def list_runs(x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        apps = (await db.scalars(select(Application).where(Application.workspace_id == x_workspace_id))).all()
        app_map = {x.id: x for x in apps}
        rows = (await db.scalars(select(Run).where(Run.application_id.in_(app_map)).order_by(Run.created_at.desc()))).all() if app_map else []
        return [run_document(row, app_map.get(row.application_id)) for row in rows]


@app.get('/api/traces')
async def list_conversation_traces(
    x_workspace_id: int = Header(alias='X-Workspace-Id'),
    user: User = Depends(current_user),
):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        applications = (await db.scalars(select(Application).where(
            Application.workspace_id == x_workspace_id,
        ))).all()
        app_map = {item.id: item for item in applications}
        conversations = list((await db.scalars(select(ApplicationConversation).where(
            ApplicationConversation.workspace_id == x_workspace_id,
            ApplicationConversation.user_id == user.id,
        ).order_by(ApplicationConversation.updated_at.desc()))).all())
        external_conversations = list((await db.scalars(select(ExternalConversation).where(
            ExternalConversation.workspace_id == x_workspace_id,
        ).order_by(ExternalConversation.updated_at.desc()))).all())
        conversations.extend(external_conversations)
        if not conversations:
            return []
        rows = (await db.scalars(select(Run).options(load_only(
            Run.id, Run.application_id, Run.conversation_id, Run.status,
            Run.approval_payload, Run.idempotency_key, Run.created_at,
        )).where(
            Run.conversation_id.in_([item.id for item in conversations]),
        ).order_by(Run.created_at))).all()
        grouped: dict[str, list[Run]] = {}
        for row in rows:
            grouped.setdefault(str(row.conversation_id or ''), []).append(row)
        return [
            conversation_trace_document(
                conversation,
                grouped.get(conversation.id, []),
                app_map[conversation.application_id],
                include_details=False,
            )
            for conversation in conversations
            # Conversations can outlive an application row when an older
            # installation did not have the cascade cleanup.  A trace for a
            # deleted application is not actionable and must not appear in
            # the observability UI.
            if conversation.application_id in app_map
            and any(is_workflow_run(item) for item in grouped.get(conversation.id, []))
        ]


@app.get('/api/traces/{conversation_id}')
async def get_conversation_trace(conversation_id: str, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        conversation = await db.get(ApplicationConversation, conversation_id)
        if conversation and conversation.user_id != user.id:
            raise HTTPException(404, 'Trace 不存在')
        if not conversation:
            conversation = await db.get(ExternalConversation, conversation_id)
        if not conversation:
            raise HTTPException(404, 'Trace 不存在')
        await workspace_member(db, conversation.workspace_id, user.id)
        app_row = await db.get(Application, conversation.application_id)
        if not app_row:
            raise HTTPException(404, 'Trace 不存在')
        runs = (await db.scalars(select(Run).where(
            Run.application_id == conversation.application_id,
            Run.conversation_id == conversation.id,
        ).order_by(Run.created_at))).all()
        if not any(is_workflow_run(item) for item in runs):
            raise HTTPException(404, 'Trace 不存在')
        return conversation_trace_document(conversation, list(runs), app_row)


@app.get('/api/traces/{conversation_id}/runs/{run_id}/events')
async def get_trace_run_events(conversation_id: str, run_id: str,
                               user: User = Depends(current_user)):
    async with SessionLocal() as db:
        run, _ = await authenticated_run_for_user(db, run_id, user)
        if str(run.conversation_id or '') != conversation_id:
            raise HTTPException(404, 'Trace 不存在')
        compacted = []
        for event in run.events or []:
            if event.get('type') != 'llm.delta' or not event.get('text'):
                compacted.append(event)
                continue
            key = (
                event.get('node_id') or event.get('step_id') or event.get('task_id') or '',
                event.get('agent_id') or '',
                event.get('llm_call_id') or '',
            )
            previous = compacted[-1] if compacted else None
            previous_key = (
                (previous or {}).get('node_id') or (previous or {}).get('step_id') or (previous or {}).get('task_id') or '',
                (previous or {}).get('agent_id') or '',
                (previous or {}).get('llm_call_id') or '',
            )
            if previous and previous.get('type') == 'llm.delta' and previous_key == key:
                previous['text'] = f"{previous.get('text') or ''}{event.get('text') or ''}"
            else:
                compacted.append(dict(event))
        return {'run_id': run.id, 'events': compacted}


@app.delete('/api/traces/{conversation_id}', status_code=204)
async def delete_conversation_trace(conversation_id: str, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        conversation = await db.get(ApplicationConversation, conversation_id)
        external = False
        if conversation and conversation.user_id != user.id:
            raise HTTPException(404, 'Trace 不存在')
        if not conversation:
            conversation = await db.get(ExternalConversation, conversation_id)
            external = True
        if not conversation:
            return Response(status_code=204)
        await workspace_member(db, conversation.workspace_id, user.id, True)
        app_row = await db.get(Application, conversation.application_id)
        runs = list((await db.scalars(select(Run).where(
            Run.application_id == conversation.application_id,
            Run.conversation_id == conversation.id,
        ))).all())
        if any(run.status in {'queued', 'running'} for run in runs):
            raise HTTPException(409, '当前 Trace 仍有任务在运行，结束后才能删除')
        run_ids = [run.id for run in runs]
        await db.execute(delete(Run).where(
            Run.application_id == conversation.application_id,
            Run.conversation_id == conversation.id,
        ))
        await db.delete(conversation)
        await db.commit()
    if app_row:
        remove_app_session(app_row.workspace_id, app_row.id, conversation_id, app_row.kind)
    if run_ids:
        await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
    return Response(status_code=204)

@app.get('/api/runs/{run_id}')
async def get_run(run_id: str, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        run, app_row = await authenticated_run_for_user(db, run_id, user)
        return run_document(run, app_row)


@app.post('/api/runs/{run_id}/retry')
async def retry_run(run_id: str, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        run, _app = await authenticated_run_for_user(db, run_id, user)
        lock_id = str(run.conversation_id or f'run:{run_id}')
    async with conversation_lock(lock_id):
        async with SessionLocal() as db:
            run, _app = await authenticated_run_for_user(db, run_id, user)
            await require_latest_conversation_run(db, run)
            if run.status not in {'failed', 'completed', 'waiting_input'}:
                raise HTTPException(409, '只有已完成、失败或等待输入的运行可以重试')
            if int(run.retry_count or 0) >= max(0, int(settings.run_max_retries)):
                raise HTTPException(409, f'该运行已达到最大重试次数（{settings.run_max_retries}）')
            event_cursor = len(run.events or [])
            reset_run_for_manual_retry(run)
            await db.commit()
    await redis.hset(f'run:{run_id}', mapping={'status': 'queued'})
    await redis.lpush(RUN_QUEUE, run_id)
    return {'id': run_id, 'status': 'queued', 'event_cursor': event_cursor}


@app.delete('/api/runs/{run_id}', status_code=204)
async def delete_run(run_id: str, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        run, app_row = await authenticated_run_for_user(db, run_id, user)
        if run.status in {'queued', 'running'}:
            raise HTTPException(409, '当前运行尚未结束，不能删除')
        state = dict(run.approval_payload or {})
        conversation_id = str(run.conversation_id or '')
        await db.delete(run)
        await db.commit()
    if not conversation_id:
        scope = str(state.get('execution_scope') or '')
        if scope:
            remove_app_session(app_row.workspace_id, app_row.id, scope, app_row.kind)
    await redis.delete(f'run:{run_id}')
    return Response(status_code=204)

@app.get('/api/runs/{run_id}/files')
async def authenticated_run_files(run_id: str, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        run, app_row = await authenticated_run_for_user(db, run_id, user)
    return artifact_documents(run, app_row, f'/api/runs/{run.id}/files')

@app.get('/api/runs/{run_id}/files/{filename:path}')
async def authenticated_run_file(run_id: str, filename: str, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        run, app_row = await authenticated_run_for_user(db, run_id, user)
    name = artifact_name(run, filename)
    return await minio_download_response(artifact_object_key(run, app_row, name), name)


@app.get('/api/runs/{run_id}/events')
async def authenticated_run_events(run_id: str, after_event: int = 0, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        run, app_row = await authenticated_run_for_user(db, run_id, user)
        definition = (
            await read_application(db, app_row)
            if (run.approval_payload or {}).get('preview')
            else await read_published_application(db, app_row)
        )
    plan = [{'step_id': item.get('id'), 'step_index': index, 'step_name': item.get('name', '执行步骤'),
             'agent_role': next((a.get('role') for a in definition.get('agents', []) if a.get('id') == item.get('agent_id')), 'CrewAI Agent'),
             'node_type': item.get('node_type', 'task')} for index, item in enumerate(definition.get('tasks', []))]
    return await run_event_response(
        run_id, after_event, plan=plan,
        completed_document=lambda current: run_document(current, app_row),
    )

@app.post('/api/runs/{run_id}/feedback')
async def submit_run_feedback(run_id: str, body: RunFeedbackIn, user: User = Depends(current_user)):
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
        conversation_id = str(run.conversation_id or f'run:{run_id}') if run else f'run:{run_id}'
    async with conversation_lock(conversation_id):
        return await _submit_run_feedback_locked(run_id, body, user)

@app.get("/api/apps/{app_id}/files")
async def list_app_files(app_id:int,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        row=await db.get(Application,app_id)
        if not row: raise HTTPException(404,"应用不存在")
        await workspace_member(db,row.workspace_id,user.id)
    root=app_dir(row.workspace_id,row.id,row.kind); return [{"name":p.relative_to(root).as_posix(),"size":p.stat().st_size,"url":f"/api/apps/{app_id}/files/{p.relative_to(root).as_posix()}"} for p in visible_app_files(root)]
@app.get("/api/apps/{app_id}/files/{filename:path}")
async def download_app_file(app_id:int,filename:str,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        row=await db.get(Application,app_id)
        if not row: raise HTTPException(404,"应用不存在")
        await workspace_member(db,row.workspace_id,user.id)
    try: path=resolve_app_file(app_dir(row.workspace_id,row.id,row.kind),filename)
    except ValueError as exc: raise HTTPException(404,"文件不存在") from exc
    if not path.is_file(): raise HTTPException(404,"文件不存在")
    return FileResponse(path,filename=path.name)
@app.get('/api/apps/{app_id}/api-keys')
async def list_api_keys(app_id:int,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        app_row=await db.get(Application,app_id)
        if not app_row: raise HTTPException(404,'应用不存在')
        await workspace_member(db,app_row.workspace_id,user.id,True)
        rows=(await db.scalars(select(ApiKey).where(ApiKey.application_id==app_id).order_by(ApiKey.created_at.desc()))).all()
        return [{'id':x.id,'name':x.name,'created_at':x.created_at.isoformat()} for x in rows]
@app.post('/api/apps/{app_id}/api-keys')
async def create_application_api_key(app_id:int,body:ApiKeyIn,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        app_row=await db.get(Application,app_id)
        if not app_row: raise HTTPException(404,'应用不存在')
        await workspace_member(db,app_row.workspace_id,user.id,True)
        raw='xsk_'+secrets.token_urlsafe(32); row=ApiKey(workspace_id=app_row.workspace_id,application_id=app_id,name=body.name,key_hash=hashlib.sha256(raw.encode()).hexdigest()); db.add(row); await db.commit(); return {"id":row.id,"key":raw,"name":body.name}
@app.delete('/api/apps/{app_id}/api-keys/{key_id}',status_code=204)
async def delete_application_api_key(app_id:int,key_id:int,user:User=Depends(current_user)):
    async with SessionLocal() as db:
        app_row=await db.get(Application,app_id)
        if not app_row: raise HTTPException(404,'应用不存在')
        await workspace_member(db,app_row.workspace_id,user.id,True);row=await db.get(ApiKey,key_id)
        if row and row.application_id==app_id: await db.delete(row);await db.commit()
    return Response(status_code=204)

@app.get('/api/models')
async def models(x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        rows = (await db.scalars(select(ModelProfile).where(ModelProfile.workspace_id == x_workspace_id))).all()
        return [public_model(row) for row in rows]

@app.post('/api/models')
async def save_model(body: dict = Body(...), x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        row = await db.get(ModelProfile, int(body['id'])) if str(body.get('id', '')).isdigit() else None
        if row and row.workspace_id != x_workspace_id:
            raise HTTPException(403, '模型不属于当前工作空间')
        if not row:
            row = ModelProfile(workspace_id=x_workspace_id)
            db.add(row)
        row.name = str(body.get('name', '')).strip()
        row.provider = body.get('provider', 'openai')
        row.model = str(body.get('model', '')).strip()
        row.model_type = str(body.get('model_type') or 'chat')
        if row.model_type not in {'chat', 'embedding'}:
            raise HTTPException(422, '模型类型必须是 chat 或 embedding')
        if 'supports_vision' in body and not isinstance(body['supports_vision'], bool):
            raise HTTPException(422, '图片理解能力必须是布尔值')
        row.supports_vision = bool(body.get('supports_vision', getattr(row, 'supports_vision', False))) if row.model_type == 'chat' else False
        row.base_url = str(body.get('base_url', '')).strip()
        row.temperature = float(body['temperature']) if body.get('temperature') not in {None, ''} else None
        row.max_tokens = int(body['max_tokens']) if body.get('max_tokens') not in {None, ''} else None
        row.timeout_seconds = max(10, min(int(body.get('timeout') or 180), 3600))
        row.max_retries = max(0, min(int(body.get('max_retries') or 0), 20))
        row.thinking_mode = str(body.get('thinking_mode') or 'auto').strip().lower()
        if row.thinking_mode not in {'auto', 'enabled', 'disabled'}:
            raise HTTPException(422, '思考模式必须是 auto、enabled 或 disabled')
        row.thinking_effort = str(body.get('thinking_effort') or '').strip().lower() or None
        if row.thinking_effort not in {None, 'minimal', 'low', 'medium', 'high', 'max'}:
            raise HTTPException(422, '思考强度必须是 minimal、low、medium、high 或 max')
        if row.thinking_mode != 'enabled':
            row.thinking_effort = None
        if not row.name or not row.model:
            raise HTTPException(422, '连接名称和 Model ID 不能为空')
        if body.get('api_key'):
            row.api_key_encrypted = encrypt_secret(body['api_key'])
        if body.get('is_default'):
            for item in (await db.scalars(select(ModelProfile).where(
                ModelProfile.workspace_id == x_workspace_id,
                ModelProfile.model_type == row.model_type,
            ))).all():
                item.is_default = False
            row.is_default = True
        await db.commit(); await db.refresh(row)
        return public_model(row)

@app.put('/api/models/default')
async def set_default_model(body: DefaultModelIn, x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        row = await db.get(ModelProfile, int(body.model_id)) if body.model_id.isdigit() else None
        if not row or row.workspace_id != x_workspace_id:
            raise HTTPException(404, '模型连接不存在')
        if body.model_type not in {'chat', 'embedding'}:
            raise HTTPException(422, '默认模型类型必须是 chat 或 embedding')
        if row.model_type != body.model_type:
            raise HTTPException(422, '所选模型类型与默认用途不匹配')
        for item in (await db.scalars(select(ModelProfile).where(
            ModelProfile.workspace_id == x_workspace_id,
            ModelProfile.model_type == body.model_type,
        ))).all():
            item.is_default = item.id == row.id
        await db.commit()
        return public_model(row)

@app.delete('/api/models/{model_id}', status_code=204)
async def delete_model(model_id: int, x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        row = await db.get(ModelProfile, model_id)
        if row and row.workspace_id == x_workspace_id:
            usage = await model_usage_count(db, x_workspace_id, model_id)
            knowledge_usage = await db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.embedding_model_id == model_id).limit(1))
            if knowledge_usage:
                raise HTTPException(409, '该 Embedding 模型正被知识库使用，请先修改或删除知识库')
            if usage:
                raise HTTPException(409, f'该模型正被 {usage} 个应用使用，请先在编排中解除引用')
            await db.delete(row); await db.commit()
    return Response(status_code=204)

@app.post('/api/models/{model_id}/test')
async def test_model(model_id: int, x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        row = await db.get(ModelProfile, model_id)
        if not row or row.workspace_id != x_workspace_id:
            raise HTTPException(404, '模型连接不存在')
        profile = {'provider': row.provider, 'model': row.model, 'base_url': row.base_url, 'api_key': decrypt_secret(row.api_key_encrypted),
                   'temperature': row.temperature, 'max_tokens': row.max_tokens,
                   'timeout': row.timeout_seconds, 'max_retries': row.max_retries,
                   'thinking_mode': row.thinking_mode, 'thinking_effort': row.thinking_effort,
                   'model_type': row.model_type}
    started = __import__('time').monotonic()
    try:
        if profile['model_type'] == 'embedding':
            from openai import OpenAI
            client = OpenAI(api_key=profile['api_key'] or 'not-required', base_url=profile['base_url'] or None,
                            timeout=profile['timeout'], max_retries=profile['max_retries'])
            await asyncio.wait_for(asyncio.to_thread(client.embeddings.create, model=profile['model'], input=['连接测试']), timeout=profile['timeout'] + 5)
        else:
            llm = profile_llm(profile)
            await asyncio.wait_for(asyncio.to_thread(llm.call, [{'role': 'user', 'content': '只回复 OK'}]), timeout=profile['timeout'] + 5)
        return {'ok': True, 'latency_ms': round((__import__('time').monotonic() - started) * 1000), 'message': '连接成功'}
    except Exception as exc:
        return {'ok': False, 'latency_ms': round((__import__('time').monotonic() - started) * 1000),
                'message': f'连接失败：{str(exc)[:300]}'}


@app.get('/api/knowledge')
async def list_knowledge(x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        rows = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.workspace_id == x_workspace_id)
                                 .order_by(KnowledgeBase.updated_at.desc()))).all()
        result = []
        for row in rows:
            files = (await db.scalars(select(KnowledgeFile).where(KnowledgeFile.knowledge_base_id == row.id)
                                      .order_by(KnowledgeFile.created_at))).all()
            result.append(knowledge_document(row, list(files)))
        return result

@app.get('/api/knowledge/{knowledge_id}')
async def get_knowledge(knowledge_id: int, x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        row = await db.get(KnowledgeBase, knowledge_id)
        if not row or row.workspace_id != x_workspace_id:
            raise HTTPException(404, '知识库不存在')
        files = (await db.scalars(select(KnowledgeFile).where(
            KnowledgeFile.knowledge_base_id == row.id).order_by(KnowledgeFile.created_at))).all()
        return knowledge_document(row, list(files))

@app.post('/api/knowledge')
async def save_knowledge(body: dict = Body(...), x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        model_id = str(body.get('embedding_model_id') or '')
        profile = await db.get(ModelProfile, int(model_id)) if model_id.isdigit() else None
        if not profile or profile.workspace_id != x_workspace_id or profile.model_type != 'embedding':
            raise HTTPException(422, '请选择当前工作空间已配置的 Embedding 模型')
        row = await db.get(KnowledgeBase, int(body['id'])) if str(body.get('id', '')).isdigit() else None
        if row and row.workspace_id != x_workspace_id: raise HTTPException(403, '知识库不属于当前工作空间')
        existing_file = (await db.scalar(select(KnowledgeFile.id).where(
            KnowledgeFile.knowledge_base_id == row.id).limit(1))) if row else None
        if existing_file and (
            row.embedding_model_id != profile.id
            or row.parsing_strategy != str(body.get('parsing_strategy') or 'auto')
            or row.chunk_size != max(200, min(int(body.get('chunk_size') or 800), 8000))
            or row.chunk_overlap != max(0, min(int(body.get('chunk_overlap') or 120),
                                               max(200, min(int(body.get('chunk_size') or 800), 8000)) // 2))
        ):
            raise HTTPException(409, '知识库已有文件。如需更换 Embedding 或分片参数，请新建知识库后重新上传。')
        if not row:
            row = KnowledgeBase(workspace_id=x_workspace_id, embedding_model_id=profile.id)
            db.add(row)
        row.name = str(body.get('name') or '').strip(); row.description = str(body.get('description') or '').strip()
        if not row.name: raise HTTPException(422, '知识库名称不能为空')
        row.embedding_model_id = profile.id
        row.parsing_strategy = str(body.get('parsing_strategy') or 'auto')
        row.chunk_size = max(200, min(int(body.get('chunk_size') or 800), 8000))
        row.chunk_overlap = max(0, min(int(body.get('chunk_overlap') or 120), row.chunk_size // 2))
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        if not existing_file:
            row.status = 'empty'
        await db.commit(); await db.refresh(row)
        files = (await db.scalars(select(KnowledgeFile).where(KnowledgeFile.knowledge_base_id == row.id))).all()
        return knowledge_document(row, list(files))

@app.post('/api/knowledge/{knowledge_id}/files')
async def upload_knowledge_files(knowledge_id: int, files: list[UploadFile] = File(...),
                                 x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    if not files or len(files) > 20: raise HTTPException(422, '每次请选择 1-20 个文件')
    result = []
    queued_ids = []
    stored_keys: list[str] = []
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        knowledge_base = await db.get(KnowledgeBase, knowledge_id)
        if not knowledge_base or knowledge_base.workspace_id != x_workspace_id: raise HTTPException(404, '知识库不存在')
        profile = await db.get(ModelProfile, knowledge_base.embedding_model_id)
        if not profile or profile.model_type != 'embedding': raise HTTPException(409, '知识库缺少可用 Embedding 模型')
        await ensure_bucket()
        try:
            for upload in files:
                data = await upload.read()
                if not data or len(data) > settings.max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, '知识文件为空或超过上传限制')
                file_row = KnowledgeFile(knowledge_base_id=knowledge_id, workspace_id=x_workspace_id,
                                         name=safe_name(upload.filename or 'knowledge.txt'), object_key='pending',
                                         content_type=upload.content_type or 'application/octet-stream', size=len(data),
                                         status='queued')
                db.add(file_row); await db.flush()
                key = f'workspaces/{x_workspace_id}/knowledge/{knowledge_id}/{file_row.id}/{file_row.name}'
                file_row.object_key = key
                await asyncio.to_thread(
                    minio.put_object, settings.minio_bucket, key, io.BytesIO(data), len(data),
                    content_type=file_row.content_type,
                )
                stored_keys.append(key)
                result.append(file_row)
                queued_ids.append(file_row.id)
            knowledge_base.status = 'processing'
            knowledge_base.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
        except Exception:
            await db.rollback()
            for key in stored_keys:
                try:
                    await asyncio.to_thread(minio.remove_object, settings.minio_bucket, key)
                except Exception:
                    pass
            raise
        response = knowledge_document(knowledge_base, result)['files']
    try:
        for file_id in queued_ids:
            await redis.lpush(KNOWLEDGE_QUEUE, str(file_id))
    except Exception as exc:
        async with SessionLocal() as db:
            rows = (await db.scalars(select(KnowledgeFile).where(
                KnowledgeFile.id.in_(queued_ids),
            ))).all()
            for row in rows:
                row.status = 'failed'
                row.error = '解析队列暂时不可用，请删除后重新上传'
            base = await db.get(KnowledgeBase, knowledge_id)
            if base:
                base.status = 'failed'
                base.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
        raise HTTPException(503, '解析队列暂时不可用，请稍后重新上传') from exc
    return response


@app.get('/api/knowledge/{knowledge_id}/files/{file_id}/chunks')
async def knowledge_file_chunks(
    knowledge_id: int,
    file_id: int,
    x_workspace_id: int = Header(alias='X-Workspace-Id'),
    user: User = Depends(current_user),
):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        knowledge_base = await db.get(KnowledgeBase, knowledge_id)
        row = await db.get(KnowledgeFile, file_id)
        if (not knowledge_base or knowledge_base.workspace_id != x_workspace_id
                or not row or row.knowledge_base_id != knowledge_id
                or row.workspace_id != x_workspace_id):
            raise HTTPException(404, '知识文件不存在')
        if row.status != 'ready':
            raise HTTPException(409, '文件尚未完成解析')
        object_key = row.object_key
        name = row.name
        content_type = row.content_type
        parsing_strategy = knowledge_base.parsing_strategy
        chunk_size = knowledge_base.chunk_size
        chunk_overlap = knowledge_base.chunk_overlap
    try:
        source = await asyncio.to_thread(minio.get_object, settings.minio_bucket, object_key)
        try:
            data = await asyncio.to_thread(source.read)
        finally:
            source.close()
            source.release_conn()
        text_content = (
            data.decode('utf-8', errors='replace')
            if parsing_strategy == 'plain'
            else await asyncio.to_thread(extract_knowledge_text, name, data, content_type)
        )
        chunks = knowledge_chunks(text_content, chunk_size, chunk_overlap)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f'无法读取分片内容：{str(exc)[:200]}') from exc
    return {
        'file_id': str(file_id), 'name': name, 'count': len(chunks),
        'chunks': [
            {'index': index + 1, 'content': content, 'characters': len(content)}
            for index, content in enumerate(chunks)
        ],
    }

@app.delete('/api/knowledge/{knowledge_id}/files/{file_id}', status_code=204)
async def delete_knowledge_file(knowledge_id: int, file_id: int, x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        row = await db.get(KnowledgeFile, file_id)
        if not row or row.knowledge_base_id != knowledge_id or row.workspace_id != x_workspace_id: return Response(status_code=204)
        try:
            await asyncio.to_thread(minio.remove_object, settings.minio_bucket, row.object_key)
        except Exception:
            pass
        await asyncio.to_thread(delete_file_vectors, x_workspace_id, knowledge_id, file_id)
        await db.delete(row); await db.flush()
        knowledge_base = await db.get(KnowledgeBase, knowledge_id)
        if knowledge_base:
            remaining_statuses = list((await db.scalars(select(KnowledgeFile.status).where(
                KnowledgeFile.knowledge_base_id == knowledge_id))).all())
            knowledge_base.status = (
                'processing' if any(status in {'queued', 'processing'} for status in remaining_statuses)
                else 'ready' if any(status == 'ready' for status in remaining_statuses)
                else 'failed' if remaining_statuses
                else 'empty'
            )
            knowledge_base.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
    return Response(status_code=204)

@app.delete('/api/knowledge/{knowledge_id}', status_code=204)
async def delete_knowledge(knowledge_id: int, x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        row = await db.get(KnowledgeBase, knowledge_id)
        if not row or row.workspace_id != x_workspace_id: return Response(status_code=204)
        usage = await resource_usage_count(db, x_workspace_id, 'knowledge', knowledge_id)
        if usage: raise HTTPException(409, f'该知识库正被 {usage} 个智能体使用，请先解除引用')
        remove_object_prefix(f'workspaces/{x_workspace_id}/knowledge/{knowledge_id}/')
        await asyncio.to_thread(delete_knowledge_collection, x_workspace_id, knowledge_id)
        await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_base_id == knowledge_id))
        await db.delete(row); await db.commit()
    return Response(status_code=204)
@app.get('/api/skills')
async def skills(x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id)
        rows = (await db.scalars(select(Skill).where(Skill.workspace_id == x_workspace_id))).all()
        return [skill_document(row) for row in rows]


@app.post('/api/skills')
async def save_skill(body: dict = Body(...), x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    try:
        document = normalize_skill_package(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    async with SessionLocal() as db:
        await workspace_member(db, x_workspace_id, user.id, True)
        row = await db.get(Skill, int(body['id'])) if str(body.get('id', '')).isdigit() else None
        if row and row.workspace_id != x_workspace_id: raise HTTPException(403, 'Skill 不属于当前工作空间')
        if row:
            expected = body.get('_base_revision', body.get('revision'))
            try:
                revision_matches = expected is None or int(expected) == int(row.revision or 1)
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, 'Skill revision 必须是整数') from exc
            if not revision_matches:
                raise HTTPException(409, 'Skill 已在其他页面更新，请重新打开后继续编辑')
            row.revision = int(row.revision or 1) + 1
        else:
            row = Skill(
                workspace_id=x_workspace_id,
                name=document['name'],
                description=document['description'],
                content={},
                revision=1,
            )
            db.add(row)
        row.name = document['name']
        row.description = document['description']
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        excluded = {'id', 'revision', 'updated_at', '_base_revision'}
        row.content = {key: value for key, value in document.items() if key not in excluded}
        await db.commit(); await db.refresh(row); return skill_document(row)

@app.delete('/api/skills/{skill_id}', status_code=204)
async def delete_skill(skill_id: int, x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db,x_workspace_id,user.id,True); row=await db.get(Skill,skill_id)
        if row and row.workspace_id==x_workspace_id:
            usage = await resource_usage_count(db, x_workspace_id, 'skill', skill_id)
            if usage: raise HTTPException(409, f'该 Skill 正被 {usage} 个应用使用，请先在编排中解除引用')
            await db.delete(row); await db.commit()
    return Response(status_code=204)

@app.post('/api/skills/import')
async def import_skills(files: list[UploadFile] = File(...), x_workspace_id: int = Header(alias='X-Workspace-Id'), user: User = Depends(current_user)):
    async with SessionLocal() as db: await workspace_member(db,x_workspace_id,user.id,True)
    if len(files)>200: raise HTTPException(400,'单次最多导入 200 个文件')
    contents={}; total=0
    for upload in files:
        data=await upload.read(); total+=len(data)
        if total>settings.max_upload_mb*1024*1024: raise HTTPException(413,f'Skill package 超过 {settings.max_upload_mb} MB')
        path=(upload.filename or '').replace('\\','/').strip('/')
        if not path or '..' in path.split('/'): raise HTTPException(422,f'文件路径不安全：{path}')
        contents[path]=data
    manifests=[path for path in contents if path.endswith('SKILL.md')]
    if len(manifests)!=1: raise HTTPException(422,'Skill 文件夹必须且只能包含一个 SKILL.md')
    try:
        manifest=contents[manifests[0]].decode('utf-8')
    except UnicodeDecodeError as exc:
        raise HTTPException(422,'SKILL.md 必须使用 UTF-8 编码') from exc
    try:
        parsed=parse_skill_manifest(manifest)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    root=manifests[0].rsplit('/',1)[0]+'/' if '/' in manifests[0] else ''
    instructions=parsed['instructions']; slug=parsed['name']; description=parsed['description']
    extras=[]
    for path,data in contents.items():
        if path==manifests[0]: continue
        relative=path[len(root):] if path.startswith(root) else path
        kind='script' if relative.startswith('scripts/') else 'asset' if relative.startswith('assets/') else 'reference'
        try: content=data.decode('utf-8'); encoding='utf8'
        except UnicodeDecodeError: content=__import__('base64').b64encode(data).decode(); encoding='base64'
        extras.append({'path':relative,'kind':kind,'content':content,'encoding':encoding,'executable':kind=='script'})
    try:
        document = normalize_skill_package({
            'name': slug.replace('-', ' ').title(),
            'slug': slug,
            'description': description,
            'instructions': instructions,
            'version': '1.0.0',
            'author': 'local',
            'source': 'local',
            'registry_ref': '',
            'files': extras,
            'enabled': True,
            'status': 'published',
        })
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    async with SessionLocal() as db:
        row=Skill(workspace_id=x_workspace_id,name=document['name'],description=description,content=document);db.add(row);await db.commit();await db.refresh(row)
        return {'imported':[skill_document(row)]}
@app.get('/api/plugins')
async def plugins(x_workspace_id:int=Header(alias='X-Workspace-Id'),user:User=Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db,x_workspace_id,user.id);rows=(await db.scalars(select(Plugin).where(Plugin.workspace_id==x_workspace_id))).all()
        return [plugin_document(row) for row in rows]
@app.post('/api/plugins')
async def save_plugin(body:dict=Body(...),x_workspace_id:int=Header(alias='X-Workspace-Id'),user:User=Depends(current_user)):
    validate_plugin_document(body)
    async with SessionLocal() as db:
        await workspace_member(db,x_workspace_id,user.id,True);row=await db.get(Plugin,int(body['id'])) if str(body.get('id','')).isdigit() else None
        if row and row.workspace_id!=x_workspace_id: raise HTTPException(403,'工具不属于当前工作空间')
        if not row: row=Plugin(workspace_id=x_workspace_id);db.add(row)
        configuration={k:v for k,v in body.items() if k not in {'id','name','kind','category','has_auth_token','has_headers'}}
        if row.id and not configuration.get('auth_token') and (row.configuration or {}).get('auth_token_encrypted'):
            configuration.pop('auth_token',None)
        if row.id and not configuration.get('headers') and (row.configuration or {}).get('headers_encrypted'):
            configuration.pop('headers',None)
        try: secured=secure_plugin_configuration(configuration,row.configuration if row.id else None)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        row.name=body['name'].strip();row.kind=body.get('kind','http');row.configuration=secured;await db.commit();await db.refresh(row)
        return plugin_document(row)
@app.delete('/api/plugins/{plugin_id}',status_code=204)
async def delete_plugin(plugin_id:int,x_workspace_id:int=Header(alias='X-Workspace-Id'),user:User=Depends(current_user)):
    async with SessionLocal() as db:
        await workspace_member(db,x_workspace_id,user.id,True);row=await db.get(Plugin,plugin_id)
        if row and row.workspace_id==x_workspace_id:
            usage = await resource_usage_count(db, x_workspace_id, 'plugin', plugin_id)
            if usage: raise HTTPException(409, f'该工具正被 {usage} 个应用使用，请先在编排中解除引用')
            await db.delete(row);await db.commit()
    return Response(status_code=204)
@app.get('/api/public/{public_token}')
async def public_application(public_token: str, response: Response,
                             xuanshu_user_id: str | None = Cookie(default=None)):
    row = await resolve_public_application(public_token)
    if not xuanshu_user_id:
        response.set_cookie(
            'xuanshu_user_id', secrets.token_urlsafe(16),
            max_age=60 * 60 * 24 * 30, httponly=True,
            samesite='lax', path=f'/api/public/{public_token}',
        )
    async with SessionLocal() as db:
        definition = await read_published_application(db, row)
    return {'name': row.name, 'description': definition.get('description', ''), 'kind': row.kind,
            'interaction_mode': definition.get('interaction_mode', 'single_run'),
            'inputs': definition.get('inputs', []), 'welcome': definition.get('description') or '你好，请输入消息或上传文件开始运行。'}


@app.get('/api/public/{public_token}/conversations')
async def public_list_conversations(
    public_token: str,
    xuanshu_user_id: str | None = Cookie(default=None),
):
    row = await resolve_public_application(public_token)
    identity = str(xuanshu_user_id or '').strip()
    if not identity:
        return []
    async with SessionLocal() as db:
        conversations = (await db.scalars(select(ExternalConversation).where(
            ExternalConversation.application_id == row.id,
            ExternalConversation.external_user_id == identity,
        ).order_by(ExternalConversation.updated_at.desc()))).all()
        return [public_conversation_document(public_token, item) for item in conversations]


@app.post('/api/public/{public_token}/files')
async def public_upload(
    public_token: str,
    response: Response,
    file: UploadFile = File(...),
    xuanshu_user_id: str | None = Cookie(default=None),
):
    """Upload once for the anonymous user; later turns reference the file ID."""
    row = await resolve_public_application(public_token)
    identity = str(xuanshu_user_id or '').strip() or secrets.token_urlsafe(16)
    response.set_cookie(
        'xuanshu_user_id', identity, max_age=60 * 60 * 24 * 30,
        httponly=True, samesite='lax', path=f'/api/public/{public_token}',
    )
    return await store_external_upload(row, identity, file)


@app.post('/api/public/{public_token}/conversations')
async def public_new_conversation(
    public_token: str,
    response: Response,
    xuanshu_user_id: str | None = Cookie(default=None),
):
    row = await resolve_public_application(public_token)
    identity = str(xuanshu_user_id or '').strip() or secrets.token_urlsafe(16)
    response.set_cookie(
        'xuanshu_user_id', identity, max_age=60 * 60 * 24 * 30,
        httponly=True, samesite='lax', path=f'/api/public/{public_token}',
    )
    async with SessionLocal() as db:
        conversation = ExternalConversation(
            id=secrets.token_urlsafe(12), application_id=row.id,
            workspace_id=row.workspace_id, external_user_id=identity,
        )
        db.add(conversation)
        await db.commit(); await db.refresh(conversation)
        return public_conversation_document(public_token, conversation)


@app.get('/api/public/{public_token}/conversations/{conversation_id}')
async def public_get_conversation(
    public_token: str,
    conversation_id: str,
    xuanshu_user_id: str | None = Cookie(default=None),
):
    row = await resolve_public_application(public_token)
    identity = str(xuanshu_user_id or '').strip()
    require_conversation_identity(conversation_id, identity)
    async with SessionLocal() as db:
        conversation = await external_conversation_for(
            db, row, identity, conversation_id, create=False,
        )
        runs = (await db.scalars(select(Run).where(
            Run.application_id == row.id,
            Run.conversation_id == conversation.id,
        ).order_by(Run.created_at))).all()
        return public_conversation_document(public_token, conversation, list(runs), row)


@app.delete('/api/public/{public_token}/conversations/{conversation_id}', status_code=204)
async def public_delete_conversation(
    public_token: str,
    conversation_id: str,
    xuanshu_user_id: str | None = Cookie(default=None),
):
    identity = str(xuanshu_user_id or '').strip()
    require_conversation_identity(conversation_id, identity)
    async with conversation_lock(f'public:{public_token}:{conversation_id}'):
        row = await resolve_public_application(public_token)
        async with SessionLocal() as db:
            conversation = await external_conversation_for(
                db, row, identity, conversation_id, create=False,
            )
            active = await db.scalar(select(Run.id).where(
                Run.application_id == row.id,
                Run.conversation_id == conversation.id,
                Run.status.in_(['queued', 'running']),
            ).limit(1))
            if active:
                raise HTTPException(409, '对话仍有任务运行，完成后才能删除')
            run_ids = list((await db.scalars(select(Run.id).where(
                Run.application_id == row.id,
                Run.conversation_id == conversation.id,
            ))).all())
            if run_ids:
                await db.execute(delete(Run).where(Run.id.in_(run_ids)))
            await db.delete(conversation)
            await db.commit()
        remove_app_session(row.workspace_id, row.id, conversation_id, row.kind)
        if run_ids:
            await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
        return Response(status_code=204)


@app.post('/api/v1/apps/{public_token}/conversations')
async def external_new_conversation(
    public_token: str,
    response: Response,
    user_id: str = '',
    xuanshu_user_id: str | None = Cookie(default=None),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    row = await external_application(public_token, x_api_key, authorization)
    external_user_id = str(user_id or '').strip()
    if not external_user_id:
        raise HTTPException(401, 'API 创建对话必须提供 user_id')
    response.set_cookie(
        'xuanshu_user_id', external_user_id, max_age=60 * 60 * 24 * 30,
        httponly=True, samesite='lax', path=f'/api/v1/apps/{public_token}',
    )
    async with SessionLocal() as db:
        conversation = ExternalConversation(
            id=secrets.token_urlsafe(12), application_id=row.id,
            workspace_id=row.workspace_id, external_user_id=external_user_id,
        )
        db.add(conversation)
        await db.commit(); await db.refresh(conversation)
        return external_conversation_document(conversation)


@app.get('/api/v1/apps/{public_token}/conversations')
async def external_list_conversations(
    public_token: str,
    user_id: str,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    row = await external_application(public_token, x_api_key, authorization)
    async with SessionLocal() as db:
        rows = (await db.scalars(select(ExternalConversation).where(
            ExternalConversation.application_id == row.id,
            ExternalConversation.external_user_id == user_id,
        ).order_by(ExternalConversation.updated_at.desc()))).all()
        return [external_conversation_document(item) for item in rows]


@app.delete('/api/v1/apps/{public_token}/conversations/{conversation_id}', status_code=204)
async def external_clear_conversation(
    public_token: str, conversation_id: str, user_id: str,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    async with conversation_lock(f'v1:{public_token}:{conversation_id}'):
        return await _external_clear_conversation_locked(
            public_token, conversation_id, user_id, x_api_key, authorization,
        )


@app.get('/api/v1/apps/{public_token}/conversations/{conversation_id}')
async def external_get_conversation(
    public_token: str, conversation_id: str, user_id: str,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    row = await external_application(public_token, x_api_key, authorization)
    async with SessionLocal() as db:
        conversation = await external_conversation_for(db, row, user_id, conversation_id)
        runs = (await db.scalars(select(Run).where(
            Run.application_id == row.id, Run.conversation_id == conversation.id,
        ).order_by(Run.created_at.asc()))).all()
        document = external_conversation_document(conversation)
        document['runs'] = [external_run_document(public_token, item, row) for item in runs]
        return document


@app.post('/api/v1/apps/{public_token}/files')
async def external_upload(
    public_token: str,
    response: Response,
    file: UploadFile = File(...),
    user_id: str = Form(default=''),
    xuanshu_user_id: str | None = Cookie(default=None),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    row = await external_application(public_token, x_api_key, authorization)
    external_user_id = str(user_id or '').strip()
    if not external_user_id:
        raise HTTPException(401, 'API 上传文件必须提供 user_id')
    return await store_external_upload(row, external_user_id, file)


@app.post('/api/v1/apps/{public_token}/runs')
async def external_create_run(
    public_token: str,
    body: ExternalRunIn,
    response: Response,
    xuanshu_user_id: str | None = Cookie(default=None),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    idempotency_header: str | None = Header(default=None, alias='Idempotency-Key'),
):
    if not str(body.user_id or '').strip():
        raise HTTPException(401, 'API 调用必须提供 user_id')
    identity = str(body.conversation_id or body.user_id or xuanshu_user_id or secrets.token_urlsafe(12))
    async with conversation_lock(f'v1:{public_token}:{identity}'):
        document = await _external_create_run_locked(
            public_token, body, response, xuanshu_user_id, x_api_key, authorization,
            idempotency_header,
        )
    # Release the conversation mutation lock before waiting or streaming.
    return await external_create_run_response(
        public_token, body, document, x_api_key, authorization,
    )

@app.get('/api/v1/apps/{public_token}/runs/{run_id}')
async def external_get_run(public_token: str, run_id: str, user_id: str = '',
                           xuanshu_user_id: str | None = Cookie(default=None),
                           x_user_id: str | None = Header(default=None, alias='X-User-Id'),
                           x_api_key: str | None = Header(default=None),
                           authorization: str | None = Header(default=None)):
    external_user_id = str(user_id or '').strip()
    if not external_user_id:
        raise HTTPException(401, 'API 查询运行必须提供 user_id')
    app_row, run = await external_run(public_token, run_id, x_api_key, authorization, external_user_id)
    return external_run_document(public_token, run, app_row)

@app.post('/api/v1/apps/{public_token}/runs/{run_id}/retry')
async def external_retry_run(public_token: str, run_id: str, user_id: str = '',
                             x_api_key: str | None = Header(default=None),
                             authorization: str | None = Header(default=None)):
    external_user_id = str(user_id or '').strip()
    if not external_user_id:
        raise HTTPException(401, 'API 重试运行必须提供 user_id')
    app_row, initial_run = await external_run(public_token, run_id, x_api_key, authorization, external_user_id)
    lock_ids = sorted({external_user_id, initial_run.conversation_id or external_user_id})
    async with AsyncExitStack() as locks:
        for lock_id in lock_ids:
            await locks.enter_async_context(conversation_lock(f'v1:{public_token}:{lock_id}'))
        app_row, _ = await external_run(public_token, run_id, x_api_key, authorization, external_user_id)
        async with SessionLocal() as db:
            run = await db.get(Run, run_id)
            if not run or run.application_id != app_row.id:
                raise HTTPException(404, '运行不存在')
            await require_latest_conversation_run(db, run)
            if run.status not in {'failed', 'completed', 'waiting_input'}:
                raise HTTPException(409, '只有已完成、失败或等待输入的运行可以重试')
            event_cursor = len(run.events or [])
            reset_run_for_manual_retry(run)
            await db.commit()
    await redis.hset(f'run:{run_id}', mapping={'status': 'queued'})
    await redis.lpush(RUN_QUEUE, run_id)
    document = external_run_document(public_token, run, app_row)
    document['event_cursor'] = event_cursor
    return document

@app.get('/api/v1/apps/{public_token}/runs/{run_id}/events')
async def external_run_events(public_token: str, run_id: str, after_event: int = 0, user_id: str = '',
                              xuanshu_user_id: str | None = Cookie(default=None),
                              x_user_id: str | None = Header(default=None, alias='X-User-Id'),
                              x_api_key: str | None = Header(default=None),
                              authorization: str | None = Header(default=None)):
    external_user_id = str(user_id or '').strip()
    if not external_user_id:
        raise HTTPException(401, 'API 读取运行事件必须提供 user_id')
    app_row, _ = await external_run(public_token, run_id, x_api_key, authorization, external_user_id)
    return await run_event_response(run_id, after_event,
        completed_document=lambda current: external_run_document(public_token, current, app_row))

@app.post('/api/v1/apps/{public_token}/runs/{run_id}/approval')
async def external_approve(public_token: str, run_id: str, body: ApprovalIn, user_id: str = '',
                           xuanshu_user_id: str | None = Cookie(default=None),
                           x_user_id: str | None = Header(default=None, alias='X-User-Id'),
                           x_api_key: str | None = Header(default=None),
                           authorization: str | None = Header(default=None)):
    external_user_id = str(user_id or '').strip()
    if not external_user_id:
        raise HTTPException(401, 'API 审批必须提供 user_id')
    app_row, run = await external_run(public_token, run_id, x_api_key, authorization, external_user_id)
    async with conversation_lock(f'v1:{public_token}:{run.conversation_id or run_id}'):
        return await _external_approve_locked(
            public_token, run_id, body, x_api_key, authorization, external_user_id,
        )


@app.get('/api/v1/apps/{public_token}/runs/{run_id}/files')
async def external_run_files(public_token: str, run_id: str,
                             user_id: str = '', xuanshu_user_id: str | None = Cookie(default=None),
                             x_user_id: str | None = Header(default=None, alias='X-User-Id'),
                             x_api_key: str | None = Header(default=None),
                             authorization: str | None = Header(default=None)):
    external_user_id = str(user_id or '').strip()
    if not external_user_id:
        raise HTTPException(401, 'API 下载文件必须提供 user_id')
    app_row, run = await external_run(
        public_token, run_id, x_api_key, authorization, external_user_id,
    )
    return artifact_documents(
        run, app_row, f'/api/v1/apps/{public_token}/runs/{run.id}/files',
        f'?user_id={quote(external_user_id)}' if external_user_id else '',
    )


@app.get('/api/v1/apps/{public_token}/runs/{run_id}/files/{filename:path}')
async def external_run_file(public_token: str, run_id: str, filename: str,
                            user_id: str = '', xuanshu_user_id: str | None = Cookie(default=None),
                            x_user_id: str | None = Header(default=None, alias='X-User-Id'),
                            x_api_key: str | None = Header(default=None),
                            authorization: str | None = Header(default=None)):
    external_user_id = str(user_id or '').strip()
    if not external_user_id:
        raise HTTPException(401, 'API 下载文件必须提供 user_id')
    app_row, run = await external_run(
        public_token, run_id, x_api_key, authorization, external_user_id,
    )
    name = artifact_name(run, filename)
    return await minio_download_response(artifact_object_key(run, app_row, name), name)


@app.post("/api/public/{public_token}/run")
async def public_run(
    public_token: str,
    response: Response,
    message: str = Form(default=""),
    inputs_json: str = Form(default="{}"),
    file_variables: list[str] = Form(default=[]),
    files: list[UploadFile] = File(default=[]),
    upload_variables: list[str] = Form(default=[]),
    upload_ids: list[str] = Form(default=[]),
    user_id: str = Form(default=""),
    conversation_id: str = Form(default=""),
    new_conversation: bool = Form(default=False),
    idempotency_key: str = Form(default=""),
    idempotency_header: str | None = Header(default=None, alias='Idempotency-Key'),
    xuanshu_user_id: str | None = Cookie(default=None),
):
    identity = str(conversation_id or user_id or xuanshu_user_id or secrets.token_urlsafe(12))
    async with conversation_lock(f'public:{public_token}:{identity}'):
        return await _public_run_locked(
            public_token, response, message, inputs_json, file_variables, files,
            upload_variables, upload_ids,
            user_id, conversation_id, new_conversation, xuanshu_user_id,
            idempotency_header or idempotency_key,
        )
@app.get("/api/public/{public_token}/runs/{run_id}/events")
async def run_events(public_token: str, run_id: str, after_event: int = 0,
                     xuanshu_user_id: str | None = Cookie(default=None)):
    app_row, run = await resolve_public_run(public_token, run_id, xuanshu_user_id or '')
    return await run_event_response(
        run_id,
        after_event,
        completed_document=lambda current: external_run_document(public_token, current, app_row, public_page=True),
    )

@app.post("/api/public/{public_token}/runs/{run_id}/retry")
async def retry_public_run(public_token: str, run_id: str,
                           xuanshu_user_id: str | None = Cookie(default=None)):
    app_row, initial_run = await resolve_public_run(public_token, run_id, xuanshu_user_id or '')
    lock_ids = sorted({str(xuanshu_user_id or ''), initial_run.conversation_id or run_id} - {''})
    async with AsyncExitStack() as locks:
        for lock_id in lock_ids:
            await locks.enter_async_context(conversation_lock(f'public:{public_token}:{lock_id}'))
        app_row, _ = await resolve_public_run(public_token, run_id, xuanshu_user_id or '')
        async with SessionLocal() as db:
            run = await db.get(Run, run_id)
            if not run or run.application_id != app_row.id:
                raise HTTPException(404, '运行不存在')
            await require_latest_conversation_run(db, run)
            if run.status not in {'failed', 'completed', 'waiting_input'}:
                raise HTTPException(409, '只有已完成、失败或等待输入的运行可以重试')
            event_cursor = len(run.events or [])
            reset_run_for_manual_retry(run)
            await db.commit()
    await redis.hset(f'run:{run_id}', mapping={'status': 'queued'})
    await redis.lpush(RUN_QUEUE, run_id)
    return {'id': run_id, 'status': 'queued', 'event_cursor': event_cursor}
@app.post("/api/public/{public_token}/runs/{run_id}/approval")
async def approve_run(public_token: str, run_id: str, body: ApprovalIn,
                      xuanshu_user_id: str | None = Cookie(default=None)):
    app_row, existing_run = await resolve_public_run(public_token, run_id, xuanshu_user_id or '')
    async with conversation_lock(f'public:{public_token}:{existing_run.conversation_id or run_id}'):
        return await _approve_run_locked(public_token, run_id, body, xuanshu_user_id or '')

@app.get("/api/public/{public_token}/runs/{run_id}/files")
async def public_run_files(public_token:str,run_id:str,
                           xuanshu_user_id: str | None = Cookie(default=None)):
    app_row,run=await resolve_public_run(public_token, run_id, xuanshu_user_id or '')
    return artifact_documents(run, app_row, f'/api/public/{public_token}/runs/{run_id}/files')
@app.get("/api/public/{public_token}/runs/{run_id}/files/{filename:path}")
async def public_run_file(public_token:str,run_id:str,filename:str,
                          xuanshu_user_id: str | None = Cookie(default=None)):
    app_row,run=await resolve_public_run(public_token, run_id, xuanshu_user_id or '')
    name=artifact_name(run,filename)
    return await minio_download_response(artifact_object_key(run, app_row, name),name)
