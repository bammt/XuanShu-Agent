"""Run and conversation operations behind the authenticated run routes.
"""
import asyncio
import json
import secrets
from .api_models import (
    ApprovalIn,
    RunFeedbackIn,
    WorkflowRunIn,
)
from .auth_deps import (
    workspace_member,
)
from .db import (
    Application,
    ApplicationConversation,
    Run,
    SessionLocal,
    User,
)
from .external_api import (
    resolve_public_run,
)
from .persistence import (
    read_application,
    read_published_application,
)
from .run_service import (
    RuntimeIntent,
    apply_waiting_chat_message,
    authenticated_run_for_user,
    budgeted_run_history,
    conversation_workflow_bound,
    durable_attachment_payload,
    enqueue_application_run,
    merge_run_inputs_into_conversation,
    nonempty_input_patch,
    owned_conversation,
    require_model_for_definition,
    route_runtime_message,
    run_document,
    should_route_runtime_turn,
    update_conversation_state,
)
from .services import (
    RUN_QUEUE,
    redis,
    remove_app_session,
)
from .studio_jobs import (
    studio_model,
)
from .studio_proposals import (
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
    delete,
    select,
)


async def _delete_application_conversation_locked(workflow_id: int, conversation_id: str,
                                                  user: User):
    async with SessionLocal() as db:
        app_row = await db.get(Application, workflow_id)
        if not app_row:
            return Response(status_code=204)
        await workspace_member(db, app_row.workspace_id, user.id)
        row = await owned_conversation(db, conversation_id, app_row, user.id)
        active_run = await db.scalar(select(Run.id).where(
            Run.application_id == app_row.id,
            Run.conversation_id == conversation_id,
            Run.status.in_(['queued', 'running']),
        ).limit(1))
        if active_run:
            raise HTTPException(409, '对话仍有任务在运行，完成后才能删除')
        run_ids = list((await db.scalars(select(Run.id).where(
            Run.application_id == app_row.id,
            Run.conversation_id == conversation_id,
        ))).all())
        await db.execute(delete(Run).where(Run.id.in_(run_ids))) if run_ids else None
        await db.delete(row)
        await db.commit()
        remove_app_session(app_row.workspace_id, app_row.id, conversation_id, app_row.kind)
    if run_ids:
        await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
    return Response(status_code=204)


async def _run_workflow_locked(workflow_id: int, body: WorkflowRunIn, user: User,
                               conversation_id: str, idempotency_key: str):
    effective_inputs = dict(body.inputs)
    effective_attachment_ids = {key: list(value) for key, value in body.attachments.items()}
    resume_conversation = False
    async with SessionLocal() as db:
        app_row = await db.get(Application, workflow_id)
        if not app_row: raise HTTPException(404, '应用不存在')
        await workspace_member(db, app_row.workspace_id, user.id)
        if body.conversation_id:
            conversation = await owned_conversation(db, body.conversation_id, app_row, user.id)
        else:
            conversation = ApplicationConversation(
                id=conversation_id, application_id=app_row.id,
                workspace_id=app_row.workspace_id, user_id=user.id,
            )
            db.add(conversation)
        existing = await db.scalar(select(Run).where(
            Run.application_id == app_row.id,
            Run.idempotency_key == idempotency_key,
        ))
        if existing:
            return run_document(existing, app_row)
        if conversation.title == '新对话' and body.message.strip():
            conversation.title = body.message.strip().splitlines()[0][:80]
        active_run = await db.scalar(select(Run.id).where(
            Run.application_id == app_row.id,
            Run.conversation_id == conversation.id,
            Run.status.in_(['queued', 'running', 'waiting_approval']),
        ).limit(1))
        if active_run:
            raise HTTPException(409, '当前对话仍有任务在运行，请等待完成后再发送')
        conversation_history = await budgeted_run_history(db, app_row.id, conversation)
        workflow_bound = await conversation_workflow_bound(db, app_row.id, conversation)
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        preview = bool(body.preview or (conversation.state or {}).get('preview'))
        if not app_row.published and not preview:
            raise HTTPException(409, '应用尚未发布，不能运行')
        definition = await read_application(db, app_row) if preview else await read_published_application(db, app_row)
        await require_model_for_definition(db, app_row.workspace_id, definition)
        if definition.get('interaction_mode') == 'multi_turn':
            # The chat box is the primary text input for conversational Flows.
            # Accept it even when an API client omits the duplicated input field.
            collection_inputs = nonempty_input_patch(definition, body.inputs)
            if body.message.strip():
                previous_state = getattr(conversation, 'state', {}) or {}
                waiting = dict((previous_state.get('runtime_resume') or {}).get('waiting_input') or {})
                primary = next((item for item in definition.get('inputs', [])
                                if item.get('input_type') in {'text', 'long_text'}), None)
                primary_name = str(primary.get('name') or '') if primary else ''
                collection_inputs = apply_waiting_chat_message(
                    collection_inputs, body.message, waiting, primary_name,
                    has_attachments=bool(body.attachments),
                )
            state, effective_inputs, effective_attachment_ids, missing = update_conversation_state(
                definition, getattr(conversation, 'state', {}) or {}, collection_inputs, body.attachments,
            )
            durable_attachments = durable_attachment_payload(definition, state)
            conversation.state = state
        else:
            durable_attachments = {}
        await db.commit()
        conversation_id = conversation.id
        runtime_resume = dict((conversation.state or {}).get('runtime_resume') or {})
        resume_conversation = bool(runtime_resume) or (conversation.state or {}).get('status') == 'waiting_input'
    direct = obvious_conversation(body.message)
    if should_route_runtime_turn(
        body.message, has_attachments=bool(body.attachments),
        resuming=resume_conversation, workflow_bound=workflow_bound,
    ):
        decision = RuntimeIntent(needs_workflow=False, reply=direct) if direct else await asyncio.to_thread(
            route_runtime_message, body.message, {**definition, 'name': app_row.name},
            await studio_model(app_row.workspace_id, definition.get('model_profile_id')),
            conversation_history,
        )
        if not decision.needs_workflow:
            run_id = secrets.token_urlsafe(12)
            output = decision.reply or '你好！有什么我可以帮你的？'
            state = {'files': [], 'inputs': body.inputs, 'attachments': {},
                     'conversation_id': conversation_id, 'runtime_type': 'conversation_router'}
            async with SessionLocal() as db:
                run = Run(id=run_id, application_id=app_row.id, conversation_id=conversation_id,
                          idempotency_key=idempotency_key,
                          status='completed', input_text=body.message,
                          output=output, events=[{'type': 'run.completed', 'output': output,
                                                'runtime_type': 'conversation_router'}], approval_payload=state)
                db.add(run)
                current = await db.get(ApplicationConversation, conversation_id)
                if current:
                    conversation_state = dict(current.state or {})
                    conversation_state['status'] = 'completed'
                    if not workflow_bound:
                        conversation_state.update({
                            'workflow_started': False,
                            'routing_mode': 'conversation',
                        })
                    current.state = conversation_state
                await db.commit(); await db.refresh(run)
            document = run_document(run, app_row)
            document['metrics'] = {'runtime_type': 'conversation_router'}
            return document
    resolved_attachments: dict[str, list[dict]] = {
        key: list(value) for key, value in durable_attachments.items()
    }
    for variable, ids in effective_attachment_ids.items():
        field = next((item for item in definition.get('inputs', [])
                      if item.get('name') == variable), {})
        if not field.get('multiple'):
            resolved_attachments[variable] = []
        resolved_attachments.setdefault(variable, [])
        for attachment_id in ids:
            raw = await redis.get(f'xuanshu:studio:attachment:{attachment_id}')
            if not raw: raise HTTPException(404, f'附件 {attachment_id} 已过期，请重新上传')
            metadata = json.loads(raw)
            if metadata.get('user_id') != user.id or metadata.get('workspace_id') != app_row.workspace_id:
                raise HTTPException(403, '附件不属于当前用户或工作空间')
            resolved_attachments[variable].append({'name': metadata['name'], 'path': metadata['path']})
    run = await enqueue_application_run(
        app_row, definition, message=body.message, inputs=effective_inputs,
        attachments=resolved_attachments, conversation_id=conversation_id,
        conversation_history=conversation_history,
        runtime_resume=runtime_resume,
        idempotency_key=idempotency_key,
        # ``preview`` also comes from the conversation state.  A follow-up
        # turn in a draft preview session normally omits ``body.preview``;
        # using the request flag here would silently switch that turn to the
        # published snapshot in the worker.
        preview=preview,
        runtime_mode='preview' if preview else 'application',
    )
    async with SessionLocal() as db:
        conversation = await db.get(ApplicationConversation, conversation_id)
        if conversation:
            if definition.get('interaction_mode') == 'multi_turn':
                state = merge_run_inputs_into_conversation(
                    dict(conversation.state or {}), run, definition,
                )
            else:
                state = dict(conversation.state or {})
            state.update({
                'status': 'running',
                'workflow_started': True,
                'routing_mode': 'workflow',
            })
            conversation.state = state
            await db.commit()
    return run_document(run, app_row)


async def _submit_run_feedback_locked(run_id: str, body: RunFeedbackIn, user: User):
    async with SessionLocal() as db:
        run, app_row = await authenticated_run_for_user(db, run_id, user)
        if run.status != 'waiting_approval': raise HTTPException(409, '当前运行不在等待审批状态')
        state = dict(run.approval_payload or {})
        required = next((item for item in reversed(run.events or []) if item.get('type') == 'approval.required'), {})
        outcomes = required.get('outcomes') or ['approved', 'revise']
        if body.outcome not in outcomes: raise HTTPException(422, '不支持的审批结果')
        resumes = body.outcome == 'approved' or bool(required.get('resume_any_outcome'))
        run.status = 'queued' if resumes else 'needs_revision'
        state['decision'] = body.model_dump()
        run.approval_payload = state; await db.commit()
    if run.status == 'queued': await redis.lpush(RUN_QUEUE, run_id)
    return run_document(run, app_row)


async def _approve_run_locked(public_token: str, run_id: str, body: ApprovalIn,
                              external_user_id: str = ''):
    app_row,_=await resolve_public_run(public_token, run_id, external_user_id)
    async with SessionLocal() as db:
        row=await db.get(Run,run_id)
        if not row: raise HTTPException(404,"运行不存在")
        state=dict(row.approval_payload or {})
        if row.status!='waiting_approval': raise HTTPException(409,"当前运行不在等待审批状态")
        required=next((item for item in reversed(row.events or []) if item.get('type')=='approval.required'),{})
        outcomes=required.get('outcomes') or ['approved','revise']
        if body.outcome not in outcomes: raise HTTPException(422,"不支持的审批结果")
        resumes=body.outcome=='approved' or bool(required.get('resume_any_outcome'))
        row.status='queued' if resumes else 'needs_revision'; state['decision']=body.model_dump(); row.approval_payload=state
        events=list(row.events or []); events.append({'type':'approval.completed','outcome':body.outcome,'feedback':body.feedback}); row.events=events; await db.commit()
    if resumes:
        await redis.hset(f'run:{run_id}',mapping={'status':'queued'}); await redis.lpush(RUN_QUEUE,run_id)
    return {"id":run_id,"status":'queued' if resumes else 'needs_revision'}
