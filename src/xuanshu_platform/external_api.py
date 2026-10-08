"""Public and external (API key) application access helpers.
"""
import hashlib
import asyncio
import json
import re
import secrets
from .artifacts import (
    artifact_documents,
)
from .config import (
    settings,
)
from .db import (
    ApiKey,
    Application,
    ExternalConversation,
    Run,
    SessionLocal,
)
from .run_service import (
    run_document,
)
from .run_events import STOPPED_RUN_STATUSES, encode_run_event, stream_run_frames
from fastapi.responses import StreamingResponse
from .services import (
    ensure_bucket,
    minio,
    redis,
    safe_name,
)
from fastapi import (
    HTTPException,
    UploadFile,
)
from sqlalchemy import (
    select,
)
from urllib.parse import (
    quote,
)


async def resolve_public_application(public_token: str):
    async with SessionLocal() as db:
        row=await db.scalar(select(Application).where(Application.public_token==public_token,Application.published==True))
        if not row: raise HTTPException(404,"应用不存在或未发布")
        return row


def public_conversation_document(public_token: str, row: ExternalConversation,
                                 runs: list[Run] | None = None,
                                 app_row: Application | None = None) -> dict:
    document = external_conversation_document(row)
    documents = []
    for run in runs or []:
        item = run_document(run, app_row)
        item['files'] = artifact_documents(
            run, app_row, f'/api/public/{public_token}/runs/{run.id}/files',
        )
        documents.append(item)
    document['runs'] = documents
    return document


def run_external_user_id(run: Run) -> str:
    return str((run.approval_payload or {}).get('user_id') or '').strip()


def require_external_run_identity(run: Run, external_user_id: str) -> None:
    """Prevent a run ID from crossing public/API user conversations."""
    owner = run_external_user_id(run)
    identity = str(external_user_id or '').strip()
    if owner and not identity:
        raise HTTPException(401, '请提供首次调用返回的 user_id')
    if owner and identity != owner:
        raise HTTPException(404, '运行不存在')


def require_conversation_identity(conversation_id: str, external_user_id: str) -> None:
    if str(conversation_id or '').strip() and not str(external_user_id or '').strip():
        raise HTTPException(401, '继续已有对话时必须提供首次调用返回的 user_id')


async def resolve_public_run(public_token:str,run_id:str,external_user_id: str | None = None):
    app_row=await resolve_public_application(public_token)
    async with SessionLocal() as db: run=await db.get(Run,run_id)
    if not run or run.application_id!=app_row.id: raise HTTPException(404,"运行不存在")
    if external_user_id is not None:
        require_external_run_identity(run, external_user_id)
    return app_row,run


async def validate_external_key(public_token: str, api_key: str | None):
    if not api_key: raise HTTPException(401,"请提供应用 API Key")
    row=await resolve_public_application(public_token)
    async with SessionLocal() as db:
        valid=await db.scalar(select(ApiKey).where(ApiKey.application_id==row.id,ApiKey.key_hash==hashlib.sha256(api_key.encode()).hexdigest()))
        if not valid: raise HTTPException(401,"API Key 无效")
    return row


def external_key(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith('bearer '):
        return authorization[7:].strip()
    return None


async def external_application(public_token: str, x_api_key: str | None, authorization: str | None):
    return await validate_external_key(public_token, external_key(x_api_key, authorization))


async def external_run(public_token: str, run_id: str, x_api_key: str | None,
                       authorization: str | None, external_user_id: str):
    app_row = await external_application(public_token, x_api_key, authorization)
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
    if not run or run.application_id != app_row.id:
        raise HTTPException(404, '运行不存在')
    require_external_run_identity(run, external_user_id)
    return app_row, run


async def external_create_run_response(public_token, body, document, x_api_key, authorization):
    """Deliver the first application reply, including input/approval pauses."""
    if not document.get('id') or body.response_mode == 'async':
        return document
    app_row, _ = await external_run(
        public_token, document['id'], x_api_key, authorization, body.user_id,
    )

    async def load_run():
        async with SessionLocal() as db:
            current = await db.get(Run, document['id'])
        if not current or current.application_id != app_row.id:
            raise HTTPException(404, '运行不存在')
        require_external_run_identity(current, body.user_id)
        return current

    def result_for(current):
        result = external_run_document(public_token, current, app_row)
        result['events_url'] = document.get('events_url')
        result['status_url'] = (
            f'/api/v1/apps/{public_token}/runs/{current.id}'
            f'?user_id={quote(body.user_id)}'
        )
        return result

    if body.response_mode == 'streaming':
        async def stream():
            yield encode_run_event({'type': 'run.accepted', **document})
            async for frame in stream_run_frames(load_run, completed_document=result_for):
                yield encode_run_event(frame) if frame is not None else ': keep-alive\n\n'
            current = await load_run()
            if current.status != 'completed':
                result = result_for(current)
                yield encode_run_event({
                    'type': ('approval.required' if current.status == 'waiting_approval'
                             else f'run.{current.status}'),
                    'run_id': current.id, 'conversation_id': result['conversation_id'],
                    'output': current.output, 'result': result,
                    'event_cursor': len(current.events or []),
                    'run_attempt': (current.approval_payload or {}).get('run_attempt', 0),
                })
        return StreamingResponse(stream(), media_type='text/event-stream', headers={
            'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
        })

    deadline = asyncio.get_running_loop().time() + body.wait_timeout_seconds
    while True:
        current = await load_run()
        if current.status in STOPPED_RUN_STATUSES:
            return result_for(current)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return {**result_for(current), 'wait_timed_out': True}
        await asyncio.sleep(min(0.4, remaining))


def is_new_conversation_command(message: str) -> bool:
    return re.sub(r'[\s。.!！,，]+', '', str(message or '')).lower() in {
        '新建对话', '开始新对话', '清空对话', '清空历史', 'newconversation', 'clearconversation',
    }


async def external_conversation_for(
    db, app_row: Application, user_id: str, conversation_id: str = '', *, create: bool = True,
) -> ExternalConversation | None:
    if conversation_id:
        conversation = await db.get(ExternalConversation, conversation_id)
        if (not conversation or conversation.application_id != app_row.id
                or conversation.external_user_id != user_id):
            raise HTTPException(404, '外部对话不存在')
        return conversation
    if not create:
        return None
    conversation = ExternalConversation(
        id=secrets.token_urlsafe(12), application_id=app_row.id,
        workspace_id=app_row.workspace_id, external_user_id=user_id,
    )
    db.add(conversation)
    await db.flush()
    return conversation


def external_conversation_document(row: ExternalConversation) -> dict:
    state = row.state or {}
    return {
        'id': row.id, 'user_id': row.external_user_id, 'application_id': str(row.application_id),
        'title': row.title, 'status': state.get('status', 'ready'),
        'history_summary': getattr(row, 'history_summary', '') or '',
        'history_tokens': getattr(row, 'history_tokens', 0) or 0,
        'state': state,
        'created_at': row.created_at.isoformat(), 'updated_at': row.updated_at.isoformat(),
    }


def external_run_document(public_token: str, run: Run, app_row: Application, *, public_page: bool = False) -> dict:
    state = run.approval_payload or {}
    events = run.events or []
    external_user_id = str(state.get('user_id') or '')
    user_query = f'?user_id={quote(external_user_id)}' if external_user_id else ''
    approval = next((item for item in reversed(events) if item.get('type') == 'approval.required'), None)
    return {
        'id': run.id, 'status': run.status, 'output': run.output, 'created_at': run.created_at.isoformat(),
        'idempotency_key': getattr(run, 'idempotency_key', '') or '',
        'inputs': state.get('inputs', {}), 'user_id': state.get('user_id', ''),
        'conversation_id': state.get('conversation_id', ''),
        'node_outputs': state.get('outputs', {}),
        'checkpoint': state.get('checkpoint', {}),
        'files': artifact_documents(
            run, app_row,
            (f'/api/public/{public_token}/runs/{run.id}/files' if public_page
             else f'/api/v1/apps/{public_token}/runs/{run.id}/files'),
            '' if public_page else user_query,
        ),
        'approval': ({'node_id': approval.get('node_id'), 'message': approval.get('message'),
                      'output': approval.get('output'), 'outcomes': approval.get('outcomes') or ['approved', 'revise'],
                      'default_outcome': approval.get('default_outcome')}
                     if run.status == 'waiting_approval' and approval else None),
        'waiting_input': state.get('waiting_input') if run.status == 'waiting_input' else None,
        'error': run.output if run.status == 'failed' else None,
    }


async def store_external_upload(row: Application, external_user_id: str,
                                file: UploadFile) -> dict:
    """Store a reusable upload owned by one external application user."""
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f'文件不能超过 {settings.max_upload_mb} MB')
    await ensure_bucket()
    upload_id = secrets.token_urlsafe(18)
    filename = safe_name(file.filename or 'upload')
    object_key = f'api-uploads/{row.id}/{upload_id}/{filename}'
    minio.put_object(settings.minio_bucket, object_key, __import__('io').BytesIO(data), len(data),
                     content_type=file.content_type or 'application/octet-stream')
    # Uploads belong to the API user, not to a conversation.  The upload ID can
    # therefore be referenced by any later conversation owned by this user.
    metadata = {'id': upload_id, 'application_id': row.id,
                'external_user_id': external_user_id,
                'name': file.filename or filename,
                'content_type': file.content_type or 'application/octet-stream', 'size': len(data), 'minio_key': object_key}
    await redis.set(
        f'xuanshu:api-upload:{upload_id}', json.dumps(metadata, ensure_ascii=False),
        ex=max(1, int(settings.external_upload_retention_days)) * 86400,
    )
    return {
        key: value for key, value in metadata.items()
        if key not in {'application_id', 'minio_key'}
    } | {'user_id': external_user_id}


def external_upload_metadata(raw: str, row: Application, external_user_id: str,
                             upload_id: str) -> dict:
    metadata = json.loads(raw)
    if metadata.get('application_id') != row.id:
        raise HTTPException(403, '上传文件不属于当前应用')
    if metadata.get('external_user_id') != external_user_id:
        raise HTTPException(403, '上传文件不属于当前用户')
    if str(metadata.get('id') or '') != str(upload_id):
        raise HTTPException(404, f'上传文件 {upload_id} 不存在或已过期')
    return metadata


async def external_upload_attachment(row: Application, external_user_id: str,
                                     upload_id: str) -> dict:
    raw = await redis.get(f'xuanshu:api-upload:{upload_id}')
    if not raw:
        raise HTTPException(404, f'上传文件 {upload_id} 不存在或已过期')
    metadata = external_upload_metadata(raw, row, external_user_id, upload_id)
    object_response = minio.get_object(settings.minio_bucket, metadata['minio_key'])
    try:
        data = object_response.read()
    finally:
        object_response.close(); object_response.release_conn()
    return {'name': metadata['name'], 'data': data}
