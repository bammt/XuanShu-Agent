"""Operations behind the public and external (API key) routes.
"""
import json
import secrets
from .api_models import (
    ApprovalIn,
    ExternalRunIn,
)
from .db import (
    ExternalConversation,
    Run,
    SessionLocal,
)
from .external_api import (
    external_application,
    external_conversation_for,
    external_run,
    external_run_document,
    external_upload_attachment,
    is_new_conversation_command,
    require_conversation_identity,
    resolve_public_application,
)
from .persistence import (
    read_published_application,
)
from .run_service import (
    apply_waiting_chat_message,
    budgeted_run_history,
    durable_attachment_payload,
    enqueue_application_run,
    input_is_supplied,
    is_upload_only_message,
    merge_run_inputs_into_conversation,
    nonempty_input_patch,
    require_model_for_definition,
)
from .services import (
    RUN_QUEUE,
    redis,
    remove_app_session,
)
from datetime import (
    UTC,
    datetime,
)
from fastapi import (
    Cookie,
    File,
    Form,
    HTTPException,
    Header,
    Response,
    UploadFile,
)
from sqlalchemy import (
    delete,
    select,
)
from urllib.parse import (
    quote,
)


async def _external_clear_conversation_locked(
    public_token: str, conversation_id: str, user_id: str,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    row = await external_application(public_token, x_api_key, authorization)
    async with SessionLocal() as db:
        conversation = await external_conversation_for(db, row, user_id, conversation_id)
        active = await db.scalar(select(Run.id).where(
            Run.application_id == row.id, Run.conversation_id == conversation.id,
            Run.status.in_(['queued', 'running']),
        ).limit(1))
        if active:
            raise HTTPException(409, '对话仍有任务运行，完成后才能清空')
        run_ids = list((await db.scalars(select(Run.id).where(
            Run.application_id == row.id, Run.conversation_id == conversation.id,
        ))).all())
        await db.execute(delete(Run).where(Run.conversation_id == conversation.id))
        await db.delete(conversation); await db.commit()
    remove_app_session(row.workspace_id, row.id, conversation_id, row.kind)
    if run_ids:
        await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
    return Response(status_code=204)


async def _external_create_run_locked(
    public_token: str,
    body: ExternalRunIn,
    response: Response,
    xuanshu_user_id: str | None = Cookie(default=None),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    idempotency_header: str | None = None,
):
    row = await external_application(public_token, x_api_key, authorization)
    async with SessionLocal() as db:
        definition = await read_published_application(db, row)
        await require_model_for_definition(db, row.workspace_id, definition)
        text_field = next((item for item in definition.get('inputs', [])
                           if item.get('input_type') in {'text', 'long_text'}), None)
        external_user_id = str(body.user_id or '').strip()
        if not external_user_id:
            raise HTTPException(401, 'API 运行应用必须提供 user_id')
        require_conversation_identity(body.conversation_id, external_user_id)
        response.set_cookie(
            'xuanshu_user_id', external_user_id, max_age=60 * 60 * 24 * 30,
            httponly=True, samesite='lax', path=f'/api/v1/apps/{public_token}',
        )
        force_new = bool(body.new_conversation)
        new_command = is_new_conversation_command(body.message)
        conversation = await external_conversation_for(
            db, row, external_user_id,
            body.conversation_id if not force_new else '',
            create=not force_new and not new_command,
        )
        if force_new or new_command or conversation is None:
            conversation = ExternalConversation(
                id=secrets.token_urlsafe(12), application_id=row.id,
                workspace_id=row.workspace_id, external_user_id=external_user_id,
            )
            db.add(conversation)
            await db.flush()
        if new_command:
            await db.commit()
            return {
                'id': '', 'status': 'conversation_created', 'output': '已新建对话。',
                'user_id': external_user_id, 'conversation_id': conversation.id,
                'events_url': None, 'files': [], 'node_outputs': {},
            }
        if conversation.title == '新对话' and body.message.strip():
            conversation.title = body.message.strip().splitlines()[0][:80]
        active_run = await db.scalar(select(Run.id).where(
            Run.application_id == row.id, Run.conversation_id == conversation.id,
            Run.status.in_(['queued', 'running', 'waiting_approval']),
        ).limit(1))
        if active_run:
            raise HTTPException(409, '当前外部对话仍有任务在运行，请等待完成后再发送')
        conversation_history = await budgeted_run_history(db, row.id, conversation)
        runtime_resume = dict((conversation.state or {}).get('runtime_resume') or {})
        collected_inputs = dict(conversation.state.get('collected_fields') or {}) if conversation.state else {}
        incoming_inputs = nonempty_input_patch(definition, body.inputs)
        if body.message.strip():
            waiting = dict((runtime_resume or {}).get('waiting_input') or {})
            primary_name = str(text_field.get('name') or '') if text_field else ''
            incoming_inputs = apply_waiting_chat_message(
                incoming_inputs, body.message, waiting, primary_name,
                has_attachments=bool(body.files),
            )
        effective_inputs = {**collected_inputs, **incoming_inputs}
        durable_attachments = durable_attachment_payload(definition, conversation.state or {})
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        conversation_id = conversation.id
    attachments: dict[str, list[dict]] = {
        key: list(value) for key, value in durable_attachments.items()
    }
    for variable, upload_ids in body.files.items():
        field = next((item for item in definition.get('inputs', [])
                      if item.get('name') == variable), {})
        if not field.get('multiple'):
            attachments[variable] = []
        attachments.setdefault(variable, [])
        for upload_id in upload_ids:
            attachments[variable].append(
                await external_upload_attachment(row, external_user_id, upload_id)
            )
            # Keep the upload lease so the same returned ID can be referenced
            # by a later conversation until its configured retention expires.
    run = await enqueue_application_run(
        row, definition, message=body.message, inputs=effective_inputs,
        attachments=attachments, conversation_id=conversation_id,
        conversation_history=conversation_history, runtime_resume=runtime_resume,
        external_user_id=external_user_id,
        idempotency_key=idempotency_header or body.idempotency_key,
        runtime_mode='api',
    )
    async with SessionLocal() as db:
        current = await db.get(ExternalConversation, conversation_id)
        if current:
            if definition.get('interaction_mode') == 'multi_turn':
                state = merge_run_inputs_into_conversation(
                    dict(current.state or {}), run, definition,
                )
            else:
                state = dict(current.state or {})
            state.update({
                'status': 'running',
                'workflow_started': True,
                'routing_mode': 'workflow',
            })
            current.state = state
            await db.commit()
    result = external_run_document(public_token, run, row)
    result['events_url'] = (
        f'/api/v1/apps/{public_token}/runs/{run.id}/events'
        f'?user_id={quote(str(external_user_id))}'
    )
    return result


async def _external_approve_locked(public_token: str, run_id: str, body: ApprovalIn,
                                   x_api_key: str | None = Header(default=None),
                                   authorization: str | None = Header(default=None),
                                   external_user_id: str = ''):
    app_row, run = await external_run(
        public_token, run_id, x_api_key, authorization, external_user_id,
    )
    if run.status != 'waiting_approval':
        raise HTTPException(409, '当前运行不在等待审批状态')
    async with SessionLocal() as db:
        current = await db.get(Run, run_id); state = dict(current.approval_payload or {})
        required = next((item for item in reversed(current.events or []) if item.get('type') == 'approval.required'), {})
        outcomes = required.get('outcomes') or ['approved', 'revise']
        if body.outcome not in outcomes:
            raise HTTPException(422, '不支持的审批结果')
        state['decision'] = body.model_dump(); current.approval_payload = state
        events = list(current.events or []); events.append({'type':'approval.completed','outcome':body.outcome,'feedback':body.feedback})
        current.events = events
        resumes = body.outcome == 'approved' or bool(required.get('resume_any_outcome'))
        current.status = 'queued' if resumes else 'needs_revision'
        await db.commit(); await db.refresh(current)
    if current.status == 'queued':
        await redis.hset(f'run:{run_id}', mapping={'status':'queued'}); await redis.lpush(RUN_QUEUE, run_id)
    return external_run_document(public_token, current, app_row)


async def _public_run_locked(public_token:str, response: Response, message:str, inputs_json:str,
                     file_variables:list[str]=Form(default=[]), files:list[UploadFile]=File(default=[]),
                     upload_variables:list[str]=Form(default=[]), upload_ids:list[str]=Form(default=[]),
                     user_id:str="", conversation_id:str="", new_conversation:bool=False,
                     xuanshu_user_id: str | None = None, idempotency_key: str = ''):
    row=await resolve_public_application(public_token)
    async with SessionLocal() as db:
        definition = await read_published_application(db, row)
        await require_model_for_definition(db, row.workspace_id, definition)
    try:
        inputs = json.loads(inputs_json or '{}')
        if not isinstance(inputs, dict): raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, 'inputs_json 必须是 JSON 对象') from exc
    inputs = nonempty_input_patch(definition, inputs)
    text_field = next((item for item in definition.get('inputs', []) if item.get('input_type') in {'text','long_text'}), None)
    file_field = next((item for item in definition.get('inputs', []) if item.get('input_type') in {'file','image'}), None)
    if (text_field and message and not is_upload_only_message(message, bool(files))
            and not input_is_supplied(inputs.get(text_field.get('name')))):
        inputs[text_field['name']] = message
    external_user_id = str(user_id or xuanshu_user_id or '').strip()
    require_conversation_identity(conversation_id, external_user_id)
    external_user_id = external_user_id or secrets.token_urlsafe(16)
    response.set_cookie(
        'xuanshu_user_id', external_user_id, max_age=60 * 60 * 24 * 30,
        httponly=True, samesite='lax', path=f'/api/public/{public_token}',
    )
    async with SessionLocal() as db:
        new_command = is_new_conversation_command(message)
        conversation = await external_conversation_for(
            db, row, external_user_id,
            conversation_id if not new_conversation else '',
            create=not new_conversation and not new_command,
        )
        if new_conversation or new_command or conversation is None:
            conversation = ExternalConversation(
                id=secrets.token_urlsafe(12), application_id=row.id,
                workspace_id=row.workspace_id, external_user_id=external_user_id,
            )
            db.add(conversation); await db.flush()
        if new_command:
            await db.commit()
            return {'id': '', 'application': row.name, 'status': 'conversation_created',
                    'output': '已新建对话。', 'user_id': external_user_id,
                    'conversation_id': conversation.id, 'files': {}, 'events_url': None}
        if conversation.title == '新对话' and message.strip():
            conversation.title = message.strip().splitlines()[0][:80]
        active_run = await db.scalar(select(Run.id).where(
            Run.application_id == row.id, Run.conversation_id == conversation.id,
            Run.status.in_(['queued', 'running', 'waiting_approval']),
        ).limit(1))
        if active_run:
            raise HTTPException(409, '当前对话仍有任务在运行，请等待完成后再发送')
        conversation_history = await budgeted_run_history(db, row.id, conversation)
        runtime_resume = dict((conversation.state or {}).get('runtime_resume') or {})
        collected_inputs = dict((conversation.state or {}).get('collected_fields') or {})
        incoming_inputs = nonempty_input_patch(definition, inputs)
        if message.strip():
            waiting = dict((runtime_resume or {}).get('waiting_input') or {})
            primary_name = str(text_field.get('name') or '') if text_field else ''
            incoming_inputs = apply_waiting_chat_message(
                incoming_inputs, message, waiting, primary_name,
                has_attachments=bool(files),
            )
        effective_inputs = {**collected_inputs, **incoming_inputs}
        durable_attachments = durable_attachment_payload(definition, conversation.state or {})
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        conversation_id = conversation.id
    attachments = {
        key: list(value) for key, value in durable_attachments.items()
    }
    replaced_variables: set[str] = set()
    for index, upload_id in enumerate(upload_ids):
        variable = upload_variables[index] if index < len(upload_variables) else ''
        configured = next((item for item in definition.get('inputs', []) if item.get('name') == variable), None)
        if not configured or configured.get('input_type') not in {'file', 'image'}:
            raise HTTPException(422, '上传文件未绑定到有效的文件输入')
        if not configured.get('multiple') and variable not in replaced_variables:
            attachments[variable] = []
            replaced_variables.add(variable)
        attachments.setdefault(variable, []).append(
            await external_upload_attachment(row, external_user_id, upload_id)
        )
    for index, upload in enumerate(files):
        variable = file_variables[index] if index < len(file_variables) else (file_field.get('name') if file_field else '')
        configured = next((item for item in definition.get('inputs', []) if item.get('name') == variable), None)
        if not configured or configured.get('input_type') not in {'file', 'image'}:
            raise HTTPException(422, '此应用没有配置文件输入')
        if not configured.get('multiple') and variable not in replaced_variables:
            attachments[variable] = []
            replaced_variables.add(variable)
        attachments.setdefault(variable, []).append({'name':upload.filename or 'upload','data':await upload.read()})
    run = await enqueue_application_run(
        row, definition, message=message, inputs=effective_inputs, attachments=attachments,
        conversation_id=conversation_id, conversation_history=conversation_history,
        runtime_resume=runtime_resume, external_user_id=external_user_id,
        idempotency_key=idempotency_key,
        runtime_mode='application',
    )
    async with SessionLocal() as db:
        current = await db.get(ExternalConversation, conversation_id)
        if current:
            if definition.get('interaction_mode') == 'multi_turn':
                state = merge_run_inputs_into_conversation(
                    dict(current.state or {}), run, definition,
                )
            else:
                state = dict(current.state or {})
            state.update({
                'status': 'running',
                'workflow_started': True,
                'routing_mode': 'workflow',
            })
            current.state = state
            await db.commit()
    state = run.approval_payload or {}
    return {"id":run.id,"application":row.name,"status":"queued","user_id":external_user_id,
            "conversation_id":conversation_id,"files":state.get('attachment_names',{}),
            "events_url":f"/api/public/{public_token}/runs/{run.id}/events"}
