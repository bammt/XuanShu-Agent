import asyncio
import copy
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, or_, select, update

from xuanshu_platform.config import settings
from xuanshu_platform.crypto import decrypt_secret
from xuanshu_platform.db import Application, ApplicationConversation, DesignSession, ExternalConversation, KnowledgeBase, KnowledgeFile, ModelProfile, Plugin, Run, SessionLocal, Skill
from xuanshu_platform.api import (StudioChatIn, persist_studio_job_failure, run_studio_job,
                                  studio_execution_resources, studio_model, studio_resources)
from xuanshu_platform.persistence import read_application, read_published_application
from xuanshu_platform.retry import is_transient_error
from xuanshu_platform.runtime import execute_application, unique_events
from xuanshu_platform.state_machine import RuntimeCheckpoint
from xuanshu_platform.conversation import budget_chat_messages
from xuanshu_platform.knowledge import delete_file_vectors, embedding_config, ingest as ingest_knowledge
from xuanshu_platform.resources import runtime_plugin_configuration
from xuanshu_platform.services import (
    RUN_QUEUE,
    KNOWLEDGE_QUEUE,
    STUDIO_PROCESSING_QUEUE,
    STUDIO_QUEUE,
    app_dir,
    app_file_manifest,
    app_session_dir,
    ensure_bucket,
    minio,
    remove_app_session,
    redis,
    sync_existing_app_file,
    sync_existing_session_file,
)

logging.basicConfig(level=logging.INFO)
WORKER_ID = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'
HEARTBEAT_SECONDS = 15
STALE_AFTER_SECONDS = 90
WORKFLOW_CONCURRENCY = max(1, int(os.getenv('WORKFLOW_WORKER_CONCURRENCY', '4')))
KNOWLEDGE_CONCURRENCY = max(1, int(os.getenv('KNOWLEDGE_WORKER_CONCURRENCY', '2')))
STUDIO_LOCK_TTL = int(os.getenv('STUDIO_SESSION_LOCK_SECONDS', '3600'))
RUN_CONVERSATION_LOCK_TTL = int(os.getenv('RUN_CONVERSATION_LOCK_SECONDS', '7200'))

async def recover_interrupted_studio_jobs() -> None:
    async with SessionLocal() as db:
        rows = (await db.scalars(select(DesignSession))).all()
        queued = [
            row.id for row in rows
            if (row.active_job or {}).get('status') in {'queued', 'planning'}
            and (row.active_job or {}).get('request')
        ]
    processing = await redis.lrange(STUDIO_PROCESSING_QUEUE, 0, -1)
    await redis.delete(STUDIO_PROCESSING_QUEUE)
    recovered = dict.fromkeys([*processing, *queued])
    # A worker crash can leave the session lock behind until its long TTL
    # expires. At startup there are no consumers yet, so these locks are
    # necessarily stale and must not prevent recovery of the persisted job.
    for session_id in recovered:
        await redis.delete(f'xuanshu:studio:session-lock:{session_id}')
        await redis.lpush(STUDIO_QUEUE, session_id)

async def _process_studio_session(session_id: str) -> None:
    async with SessionLocal() as db:
        row = await db.get(DesignSession, session_id)
        if not row:
            await redis.lrem(STUDIO_PROCESSING_QUEUE, 0, session_id)
            return
        active = dict(row.active_job or {})
        if active.get('status') not in {'queued', 'planning'}:
            await redis.lrem(STUDIO_PROCESSING_QUEUE, 0, session_id)
            return
        request = active.get('request') or {}
        job_id = str(active.get('job_id') or '')
        if not request or not job_id:
            await redis.lrem(STUDIO_PROCESSING_QUEUE, 0, session_id)
            return
        request_history, summary, history_tokens = budget_chat_messages(list(row.messages or []))
        request = {**request, 'history': request_history}
        row.history_summary = summary
        row.history_tokens = history_tokens
        active['request'] = request
        active['status'] = 'planning'
        active['updated_at'] = datetime.now(UTC).replace(tzinfo=None).isoformat()
        row.active_job = active
        workspace_id, user_id = row.workspace_id, row.user_id
        await db.commit()
    try:
        body = StudioChatIn.model_validate(request)
        model = await studio_model(workspace_id, body.model_profile_id)
        resources = await studio_resources(workspace_id)
        runtime_resources = await studio_execution_resources(workspace_id)
        await run_studio_job(job_id, body, workspace_id, user_id, model, resources, runtime_resources)
    except Exception as exc:
        logging.exception('studio session %s failed before composer execution', session_id)
        detail = exc.detail if hasattr(exc, 'detail') else str(exc)
        await persist_studio_job_failure(job_id, session_id, workspace_id, user_id, detail)
    finally:
        await redis.lrem(STUDIO_PROCESSING_QUEUE, 0, session_id)


async def process_studio_session(session_id: str) -> None:
    """Run one Studio job while preventing duplicate session advancement."""
    lock_key = f'xuanshu:studio:session-lock:{session_id}'
    lock_token = uuid.uuid4().hex
    acquired = await redis.set(lock_key, lock_token, nx=True, ex=STUDIO_LOCK_TTL)
    if not acquired:
        await redis.lrem(STUDIO_PROCESSING_QUEUE, 0, session_id)
        return
    try:
        await _process_studio_session(session_id)
    finally:
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1, lock_key, lock_token,
        )

async def process_knowledge_file(file_id: int) -> None:
    async with SessionLocal() as db:
        claimed = await db.execute(update(KnowledgeFile).where(
            KnowledgeFile.id == file_id, KnowledgeFile.status == 'queued',
        ).values(status='processing', error=''))
        if claimed.rowcount != 1:
            await db.rollback()
            return
        await db.commit()
        file_row = await db.get(KnowledgeFile, file_id)
        knowledge_base = await db.get(KnowledgeBase, file_row.knowledge_base_id) if file_row else None
        profile = await db.get(ModelProfile, knowledge_base.embedding_model_id) if knowledge_base else None
        if not file_row or not knowledge_base or not profile:
            return
        settings_key = file_row.object_key
        workspace_id = file_row.workspace_id
        parameters = (knowledge_base.chunk_size, knowledge_base.chunk_overlap, knowledge_base.parsing_strategy,
                      knowledge_base.id, file_row.name, file_row.content_type)
    try:
        response = await asyncio.to_thread(minio.get_object, settings.minio_bucket, settings_key)
        data = await asyncio.to_thread(response.read)
        await asyncio.to_thread(response.close)
        chunk_count = await asyncio.to_thread(
            ingest_knowledge, workspace_id, parameters[3], profile, parameters[4], data,
            parameters[5], parameters[0], parameters[1], file_id, parameters[2])
        async with SessionLocal() as db:
            row = await db.get(KnowledgeFile, file_id)
            if row:
                row.chunk_count = chunk_count; row.status = 'ready'; row.error = ''
                base = await db.get(KnowledgeBase, row.knowledge_base_id)
                if base:
                    pending = await db.scalar(select(KnowledgeFile.id).where(
                        KnowledgeFile.knowledge_base_id == base.id,
                        KnowledgeFile.status.in_(['queued', 'processing']),
                    ).limit(1))
                    base.status = 'processing' if pending else 'ready'
                    base.updated_at = datetime.now(UTC).replace(tzinfo=None)
                await db.commit()
            else:
                # A delete can race with an in-flight embedding request. The
                # missing row is the cancellation marker; remove vectors that
                # finished writing after the delete endpoint ran.
                await asyncio.to_thread(
                    delete_file_vectors, workspace_id, parameters[3], file_id,
                )
    except Exception as exc:
        logging.exception('knowledge file %s failed', file_id)
        async with SessionLocal() as db:
            row = await db.get(KnowledgeFile, file_id)
            if row:
                row.status = 'failed'; row.error = str(exc)[:500]
                base = await db.get(KnowledgeBase, row.knowledge_base_id)
                if base:
                    pending = await db.scalar(select(KnowledgeFile.id).where(
                        KnowledgeFile.knowledge_base_id == base.id,
                        KnowledgeFile.status.in_(['queued', 'processing']),
                    ).limit(1))
                    base.status = 'processing' if pending else 'failed'
                    base.updated_at = datetime.now(UTC).replace(tzinfo=None)
                await db.commit()

async def recover_queued_knowledge_files() -> None:
    async with SessionLocal() as db:
        rows = (await db.scalars(select(KnowledgeFile.id).where(
            KnowledgeFile.status.in_(['queued', 'processing']),
        ))).all()
        if rows:
            await db.execute(update(KnowledgeFile).where(
                KnowledgeFile.id.in_(rows),
            ).values(status='queued'))
            await db.commit()
    for file_id in rows:
        await redis.lpush(KNOWLEDGE_QUEUE, str(file_id))

async def recover_interrupted_runs() -> None:
    """Requeue only runs whose owning worker stopped heartbeating."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=STALE_AFTER_SECONDS)
    recovered: list[str] = []
    async with SessionLocal() as db:
        rows = (await db.scalars(select(Run).where(
            Run.status == 'running', or_(Run.heartbeat_at.is_(None), Run.heartbeat_at < cutoff),
        ))).all()
        for run in rows:
            events = list(run.events or [])
            events.append({'type': 'run.recovered', 'message': '执行服务中断，已从已保存节点继续'})
            claimed = await db.execute(update(Run).where(
                Run.id == run.id, Run.status == 'running',
                or_(Run.heartbeat_at.is_(None), Run.heartbeat_at < cutoff),
            ).values(status='queued', worker_id=None, heartbeat_at=None, events=events))
            if claimed.rowcount == 1:
                recovered.append(run.id)
        await db.commit()
    for run_id in recovered:
        await redis.hset(f'run:{run_id}', mapping={'status': 'queued'})
        await redis.lpush(RUN_QUEUE, run_id)
    if recovered:
        logging.warning('recovered %d interrupted runs', len(recovered))


async def cleanup_expired_uploads() -> None:
    """Remove temporary objects only after their Redis lease expired."""
    now = datetime.now(UTC)
    composer_cutoff = now - timedelta(hours=24)
    api_cutoff = now - timedelta(days=max(1, int(settings.external_upload_retention_days)))
    objects = await asyncio.to_thread(
        lambda: [item for prefix in ('api-uploads/', 'composer/')
                 for item in minio.list_objects(settings.minio_bucket, prefix=prefix, recursive=True)]
    )
    removed = 0
    for item in objects:
        parts = item.object_name.split('/')
        cutoff = api_cutoff if parts[0] == 'api-uploads' else composer_cutoff
        if not item.last_modified or item.last_modified >= cutoff:
            continue
        if parts[0] == 'api-uploads' and len(parts) >= 4:
            lease = f'xuanshu:api-upload:{parts[2]}'
        elif parts[0] == 'composer' and len(parts) >= 4:
            lease = f'xuanshu:studio:attachment:{parts[2]}'
        else:
            continue
        if await redis.exists(lease):
            continue
        await asyncio.to_thread(minio.remove_object, settings.minio_bucket, item.object_name)
        if parts[0] == 'composer':
            root = Path('/var/lib/xuanshu/workspaces/composer') / parts[1]
            for path in root.glob(f'{parts[2]}-*'):
                path.unlink(missing_ok=True)
        removed += 1
    if removed:
        logging.info('removed %d expired temporary uploads', removed)


async def cleanup_expired_external_sessions() -> None:
    """Delete stale public/API conversations and their run history."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=max(1, int(settings.external_session_retention_days)),
    )
    run_ids: list[str] = []
    sessions: list[tuple[int, int, str, str]] = []
    async with SessionLocal() as db:
        rows = (await db.scalars(select(ExternalConversation).where(
            ExternalConversation.updated_at < cutoff,
        ))).all()
        for conversation in rows:
            active = await db.scalar(select(Run.id).where(
                Run.conversation_id == conversation.id,
                Run.status.in_(['queued', 'running']),
            ).limit(1))
            if active:
                continue
            ids = list((await db.scalars(select(Run.id).where(
                Run.conversation_id == conversation.id,
            ))).all())
            run_ids.extend(ids)
            application = await db.get(Application, conversation.application_id)
            if application:
                sessions.append((conversation.workspace_id, conversation.application_id,
                                 conversation.id, application.kind))
            await db.execute(delete(Run).where(Run.conversation_id == conversation.id))
            await db.delete(conversation)
        if rows:
            await db.commit()
    if run_ids:
        await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
    for workspace_id, app_id, conversation_id, app_kind in sessions:
        await asyncio.to_thread(
            remove_app_session, workspace_id, app_id, conversation_id, app_kind,
        )
    if run_ids:
        logging.info('removed %d expired external conversation runs', len(run_ids))


async def cleanup_expired_application_conversations() -> None:
    """Delete inactive authenticated conversation history after retention."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=max(1, int(settings.conversation_retention_days)),
    )
    run_ids: list[str] = []
    sessions: list[tuple[int, int, str, str]] = []
    async with SessionLocal() as db:
        rows = (await db.scalars(select(ApplicationConversation).where(
            ApplicationConversation.updated_at < cutoff,
        ))).all()
        for conversation in rows:
            active = await db.scalar(select(Run.id).where(
                Run.conversation_id == conversation.id,
                Run.status.in_(['queued', 'running']),
            ).limit(1))
            if active:
                continue
            ids = list((await db.scalars(select(Run.id).where(
                Run.conversation_id == conversation.id,
            ))).all())
            run_ids.extend(ids)
            application = await db.get(Application, conversation.application_id)
            if application:
                sessions.append((conversation.workspace_id, conversation.application_id,
                                 conversation.id, application.kind))
            await db.execute(delete(Run).where(Run.conversation_id == conversation.id))
            await db.delete(conversation)
        if rows:
            await db.commit()
    if run_ids:
        await redis.delete(*(f'run:{run_id}' for run_id in run_ids))
        logging.info('removed %d expired authenticated conversation runs', len(run_ids))
    for workspace_id, app_id, conversation_id, app_kind in sessions:
        await asyncio.to_thread(
            remove_app_session, workspace_id, app_id, conversation_id, app_kind,
        )


async def heartbeat(run_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        async with SessionLocal() as db:
            updated = await db.execute(update(Run).where(
                Run.id == run_id, Run.status == 'running', Run.worker_id == WORKER_ID,
            ).values(heartbeat_at=datetime.now(UTC).replace(tzinfo=None)))
            await db.commit()
        if updated.rowcount != 1:
            return


async def persist_runtime_event(run_id: str, event: dict) -> None:
    """Persist every state transition before the next node may start."""
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
        if not run or run.status != 'running' or run.worker_id != WORKER_ID:
            return
        events = list(run.events or [])
        merged = unique_events([*events, event])
        if len(merged) == len(events):
            return
        run.events = merged
        checkpoint = event.get('checkpoint')
        if isinstance(checkpoint, dict):
            state = dict(run.approval_payload or {})
            state['checkpoint'] = checkpoint
            state['outputs'] = dict(checkpoint.get('outputs') or state.get('outputs') or {})
            state['pending_node'] = checkpoint.get('current_node')
            state['waiting_input'] = checkpoint.get('waiting_input')
            run.approval_payload = state
        await db.commit()


async def _process(run_id: str) -> None:
    async with SessionLocal() as db:
        claim = await db.execute(update(Run).where(Run.id == run_id, Run.status == 'queued').values(
            status='running', worker_id=WORKER_ID, heartbeat_at=datetime.now(UTC).replace(tzinfo=None),
        ))
        if claim.rowcount != 1:
            await db.rollback()
            return
        await db.commit()
        run = await db.get(Run, run_id)
        if not run:
            return
        app = await db.get(Application, run.application_id)
        runtime_app = copy.copy(app)
        state = dict(run.approval_payload or {})
        profiles = (await db.scalars(select(ModelProfile).where(ModelProfile.workspace_id == app.workspace_id))).all()
        definition = (
            await read_application(db, app)
            if state.get('preview')
            else await read_published_application(db, app)
        )
        runtime_app.kind = definition.get('kind', app.kind)
        skills = (await db.scalars(select(Skill).where(Skill.workspace_id == app.workspace_id))).all()
        plugins = (await db.scalars(select(Plugin).where(Plugin.workspace_id == app.workspace_id))).all()
        knowledge_bases = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.workspace_id == app.workspace_id))).all()
        profile_map = {item.id: item for item in profiles}
        resources = {
            'skills': {str(item.id): {'id': str(item.id), **(item.content or {})} for item in skills},
            'plugins': {str(item.id): {'id': str(item.id), 'name': item.name, 'kind': item.kind,
                                      **runtime_plugin_configuration(item.configuration)} for item in plugins},
            'knowledge': {str(item.id): {'id': str(item.id), 'name': item.name,
                                        'embedding': embedding_config(profile_map[item.embedding_model_id])}
                          for item in knowledge_bases if item.embedding_model_id in profile_map and item.status == 'ready'},
        }
        state['execution_id'] = run_id
        files = state.get('files', [])
        events = list(run.events or [])
        events.append({
            'type': 'run.started', 'application': app.name,
            'at': datetime.now(UTC).isoformat(),
        })
        run.events = events
        await db.commit()
    await redis.hset(f'run:{run_id}', mapping={'status': 'running'})
    heartbeat_task = asyncio.create_task(heartbeat(run_id))
    try:
        loop = asyncio.get_running_loop()
        def on_runtime_event(event: dict) -> None:
            future = asyncio.run_coroutine_threadsafe(persist_runtime_event(run_id, event), loop)
            future.result(timeout=20)
        mapped = {}
        for item in profiles:
            # Application definitions persist profile references as strings,
            # while SQLAlchemy returns integer primary keys. Keep both forms
            # so a selected (and freshly updated) profile is never mistaken
            # for the workspace default merely because of key type.
            config = {
                'provider': item.provider,
                'model': item.model,
                'base_url': item.base_url,
                'api_key': decrypt_secret(item.api_key_encrypted),
                'temperature': item.temperature,
                'max_tokens': item.max_tokens,
                'timeout': item.timeout_seconds,
                'max_retries': item.max_retries,
                'thinking_mode': item.thinking_mode,
                'thinking_effort': item.thinking_effort,
            }
            mapped[item.id] = config
            mapped[str(item.id)] = config
        default = next((item for item in profiles if item.model_type == 'chat' and item.is_default), None)
        if default:
            mapped['default'] = mapped[default.id]
        result = await asyncio.to_thread(execute_application, runtime_app, run.input_text, files, state,
                                         mapped, definition, resources, on_runtime_event)
        async with SessionLocal() as db:
            run = await db.get(Run, run_id)
            events = list(run.events or [])
            execution_scope = str(state.get('execution_scope') or '')
            root = (app_session_dir(runtime_app.workspace_id, runtime_app.id, execution_scope, runtime_app.kind)
                    if execution_scope else app_dir(runtime_app.workspace_id, runtime_app.id, runtime_app.kind))
            snapshot = state.get('snapshot', {})
            current = app_file_manifest(root)
            receipt_artifacts = {
                str(name)
                for receipt in result.get('skill_execution_receipts', [])
                for name in receipt.get('files', [])
                if str(name) in current and str(name) not in files
            }
            changed_artifacts = [name for name, modified in current.items()
                                 if name not in files and snapshot.get(name) != modified]
            changed_artifacts = sorted({*changed_artifacts, *receipt_artifacts})
            artifacts = sorted({
                *changed_artifacts,
                *(name for name in state.get('artifacts', []) if name in current and name not in files),
            })
            events = unique_events([*events, *result['events']])
            if changed_artifacts:
                await ensure_bucket()
                for name in changed_artifacts:
                    if execution_scope:
                        sync_existing_session_file(
                            runtime_app.workspace_id, runtime_app.id, execution_scope, name, runtime_app.kind,
                        )
                    else:
                        sync_existing_app_file(runtime_app.workspace_id, runtime_app.id, name, runtime_app.kind)
                events.append({
                    'type': 'files.ready', 'files': changed_artifacts,
                    'at': datetime.now(UTC).isoformat(),
                })
            terminal_event = {
                'type': f"run.{result['status']}", 'output': result['output'],
                'at': datetime.now(UTC).isoformat(),
            }
            if not any(item.get('type') == terminal_event['type'] for item in events):
                events.append(terminal_event)
            run.status = result['status']
            run.output = result['output']
            run.events = events
            run.worker_id = None
            run.heartbeat_at = None
            state.update({'outputs': result['outputs'], 'files': files, 'snapshot': snapshot,
                          'artifacts': artifacts, 'pending_node': result.get('pending_node'),
                          'waiting_input': result.get('waiting_input'),
                          'node_artifacts': result.get('node_artifacts', {}),
                          'checkpoint': result.get('checkpoint', {}),
                          'skill_execution_receipts': result.get('skill_execution_receipts', []),})
            if not result.get('waiting_input'):
                state.pop('waiting_input', None)
            if result['status'] == 'completed':
                state.pop('decision', None)
            run.approval_payload = state
            if run.conversation_id:
                conversation = await db.get(ApplicationConversation, run.conversation_id)
                if conversation is None:
                    conversation = await db.get(ExternalConversation, run.conversation_id)
                if conversation:
                    conversation_state = dict(conversation.state or {})
                    conversation_state['status'] = (
                        'waiting_input' if result['status'] == 'waiting_input'
                        else ('completed' if result['status'] == 'completed' else 'ready')
                    )
                    if result['status'] == 'waiting_input' and definition.get('interaction_mode') == 'multi_turn':
                        conversation_state['pending_node'] = result.get('pending_node')
                        conversation_state['last_output'] = result.get('output', '')
                        conversation_state['runtime_resume'] = {
                            'outputs': result.get('outputs', {}),
                            'files': state.get('files', []),
                            'snapshot': state.get('snapshot', {}),
                            'artifacts': state.get('artifacts', []),
                            'node_artifacts': result.get('node_artifacts', {}),
                            'pending_node': result.get('pending_node'),
                            'waiting_input': result.get('waiting_input'),
                            'checkpoint': result.get('checkpoint', {}),
                            'skill_execution_receipts': result.get('skill_execution_receipts', []),
                        }
                    else:
                        conversation_state.pop('runtime_resume', None)
                        conversation_state.pop('pending_node', None)
                    conversation.state = conversation_state
            await db.commit()
        await redis.hset(f'run:{run_id}', mapping={'status': result['status'], 'output': result['output']})
    except Exception as exc:
        logging.exception('run %s failed', run_id)
        async with SessionLocal() as db:
            run = await db.get(Run, run_id)
            if run and is_transient_error(exc) and run.retry_count < settings.run_max_retries:
                attempt = run.retry_count + 1
                events = list(run.events or [])
                events.append({
                    'type': 'run.retrying',
                    'attempt': attempt,
                    'message': f'模型连接暂时中断，正在进行第 {attempt} 次恢复',
                })
                run.status = 'queued'
                run.retry_count = attempt
                run.worker_id = None
                run.heartbeat_at = None
                run.events = events
                await db.commit()
                await redis.hset(f'run:{run_id}', mapping={'status': 'queued'})
                delay = min(settings.run_retry_base_seconds * (2 ** (attempt - 1)), 30)
                await asyncio.sleep(delay)
                await redis.lpush(RUN_QUEUE, run_id)
                return
            if not run:
                return
            checkpoint = RuntimeCheckpoint.from_resume(state)
            checkpoint.fail(str(exc))
            state['checkpoint'] = checkpoint.dump()
            try:
                execution_scope = str(state.get('execution_scope') or '')
                root = (app_session_dir(runtime_app.workspace_id, runtime_app.id, execution_scope, runtime_app.kind)
                        if execution_scope else app_dir(runtime_app.workspace_id, runtime_app.id, runtime_app.kind))
                snapshot = state.get('snapshot', {})
                current = app_file_manifest(root)
                changed_artifacts = [name for name, modified in current.items()
                                     if name not in files and snapshot.get(name) != modified]
                artifacts = sorted({
                    *changed_artifacts,
                    *(name for name in state.get('artifacts', []) if name in current and name not in files),
                })
                if changed_artifacts:
                    await ensure_bucket()
                    for name in changed_artifacts:
                        if execution_scope:
                            sync_existing_session_file(
                                runtime_app.workspace_id, runtime_app.id, execution_scope, name, runtime_app.kind,
                            )
                        else:
                            sync_existing_app_file(runtime_app.workspace_id, runtime_app.id, name, runtime_app.kind)
                    events = list(run.events or [])
                    events.append({'type': 'files.ready', 'files': changed_artifacts})
                    run.events = events
                state['artifacts'] = artifacts
                run.approval_payload = state
            except Exception:
                logging.exception('run %s failed while publishing partial artifacts', run_id)
            run.status = 'failed'
            run.output = str(exc)
            run.worker_id = None
            run.heartbeat_at = None
            events = list(run.events or [])
            events.append({'type': 'run.failed', 'error': str(exc)})
            run.events = events
            if run.conversation_id:
                conversation = await db.get(ApplicationConversation, run.conversation_id)
                if conversation is None:
                    conversation = await db.get(ExternalConversation, run.conversation_id)
                if conversation:
                    conversation_state = dict(conversation.state or {})
                    conversation_state['status'] = 'ready'
                    conversation.state = conversation_state
            await db.commit()
        await redis.hset(f'run:{run_id}', mapping={'status': 'failed', 'output': str(exc)})
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def process(run_id: str) -> None:
    """Serialize runs belonging to one conversation while retaining global concurrency."""
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
        conversation_id = str(run.conversation_id or '') if run else ''
    if not conversation_id:
        await _process(run_id)
        return
    lock_key = f'xuanshu:conversation-lock:{conversation_id}'
    lock_token = uuid.uuid4().hex
    acquired = await redis.set(lock_key, lock_token, nx=True, ex=RUN_CONVERSATION_LOCK_TTL)
    if not acquired:
        await redis.lpush(RUN_QUEUE, run_id)
        await asyncio.sleep(.2)
        return
    try:
        await _process(run_id)
    finally:
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1, lock_key, lock_token,
        )


async def workflow_consumer(index: int) -> None:
    """Consume independent runs concurrently; DB claim prevents duplicates."""
    while True:
        try:
            run_id = await redis.rpop(RUN_QUEUE)
            if run_id:
                await process(run_id)
            else:
                await asyncio.sleep(.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('workflow consumer %s failed; retrying', index)
            await asyncio.sleep(2)


async def knowledge_consumer(index: int) -> None:
    while True:
        try:
            knowledge_file_id = await redis.rpop(KNOWLEDGE_QUEUE)
            if knowledge_file_id:
                await process_knowledge_file(int(knowledge_file_id))
            else:
                await asyncio.sleep(.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('knowledge consumer %s failed; retrying', index)
            await asyncio.sleep(2)


async def maintenance_loop() -> None:
    last_recovery = asyncio.get_running_loop().time()
    last_cleanup = last_recovery
    last_session_cleanup = last_recovery
    last_application_session_cleanup = last_recovery
    while True:
        try:
            await asyncio.sleep(1)
            now = asyncio.get_running_loop().time()
            if now - last_recovery >= HEARTBEAT_SECONDS:
                await recover_interrupted_runs()
                last_recovery = now
            if now - last_cleanup >= 3600:
                await cleanup_expired_uploads()
                last_cleanup = now
            if now - last_session_cleanup >= 3600:
                await cleanup_expired_external_sessions()
                last_session_cleanup = now
            if now - last_application_session_cleanup >= 3600:
                await cleanup_expired_application_conversations()
                last_application_session_cleanup = now
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('worker maintenance loop failed; retrying')


async def main() -> None:
    await recover_interrupted_runs()
    await recover_queued_knowledge_files()
    await cleanup_expired_uploads()
    await cleanup_expired_external_sessions()
    await cleanup_expired_application_conversations()
    logging.info(
        'starting %s workflow consumers and %s knowledge consumers',
        WORKFLOW_CONCURRENCY, KNOWLEDGE_CONCURRENCY,
    )
    await asyncio.gather(
        maintenance_loop(),
        *(workflow_consumer(index) for index in range(WORKFLOW_CONCURRENCY)),
        *(knowledge_consumer(index) for index in range(KNOWLEDGE_CONCURRENCY)),
    )


if __name__ == '__main__':
    asyncio.run(main())
