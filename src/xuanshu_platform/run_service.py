"""Application runs and conversations: documents, input merging, enqueueing and event responses.
"""
import json
import secrets
from .artifacts import (
    artifact_documents,
)
from .auth_deps import (
    workspace_member,
)
from .config import (
    settings,
)
from .contracts import (
    executable_contract_errors,
    variable_contract_errors,
)
from .conversation import (
    budget_conversation_history,
    request_fingerprint,
)
from .db import (
    Application,
    ApplicationConversation,
    ModelProfile,
    Run,
    SessionLocal,
    User,
)
from .model_runtime import (
    kickoff_structured,
    parse_structured_output,
    profile_llm,
)
from .run_events import (
    encode_run_event,
    stream_run_frames,
)
from .services import (
    RUN_QUEUE,
    app_dir,
    app_file_manifest,
    app_session_dir,
    delete_session_file,
    redis,
    resolve_app_file,
    safe_name,
    safe_relative_path,
    sync_session_file,
)
from .studio_proposals import (
    obvious_conversation,
)
from crewai import (
    Agent,
)
from fastapi import (
    HTTPException,
)
from fastapi.responses import (
    StreamingResponse,
)
from pathlib import (
    Path,
)
from pydantic import (
    BaseModel,
)
from sqlalchemy import (
    select,
)
from sqlalchemy.exc import (
    IntegrityError,
)


class RuntimeIntent(BaseModel):
    needs_workflow: bool
    reply: str = ''


def should_route_runtime_turn(message: str, *, has_attachments: bool,
                              resuming: bool, workflow_bound: bool) -> bool:
    """Route only before a conversation has committed to its workflow."""
    if not str(message or '').strip() or has_attachments or resuming or workflow_bound:
        return False
    return True


UPLOAD_ONLY_CHAT_MESSAGES = frozenset({
    '请处理我上传的内容。',
    '请判断并处理我上传的文件。',
    '请处理上传的文件。',
    '请结合我上传的附件继续。',
})


def is_upload_only_message(message: str, has_attachments: bool = False) -> bool:
    return bool(has_attachments and str(message or '').strip() in UPLOAD_ONLY_CHAT_MESSAGES)


def route_runtime_message(
    message: str,
    definition: dict,
    model: dict,
    conversation_history: list[dict] | None = None,
) -> RuntimeIntent:
    direct = obvious_conversation(message)
    if direct:
        return RuntimeIntent(needs_workflow=False, reply=direct)
    agent = Agent(
        role='应用入口路由器', goal='只判断本轮消息是否需要运行完整业务工作流',
        backstory='你负责低成本入口分流。问候、闲聊、致谢和与应用业务无关的问题直接回答；只有明确要求应用执行其业务能力时才进入工作流。',
        llm=profile_llm({**model, 'temperature': 0}),
        max_iter=1, reasoning=False, allow_delegation=False, verbose=False,
    )
    prompt = ({'role': 'user', 'content': json.dumps({
        'application': {'name': definition.get('name', ''), 'description': definition.get('description', ''),
                        'tasks': [item.get('name', '') for item in definition.get('tasks', [])]},
        'message': message,
        'recent_conversation': (
            [item for item in (conversation_history or []) if item.get('summary')]
            + [item for item in (conversation_history or []) if not item.get('summary')][-6:]
        ),
        'rule': 'needs_workflow=false 时必须直接给出简短自然回复；true 时 reply 留空。',
    }, ensure_ascii=False)})
    output = kickoff_structured(agent, [prompt], RuntimeIntent, model)
    return parse_structured_output(output, RuntimeIntent)


def run_document(run: Run, app_row: Application | None = None, *, include_details: bool = True) -> dict:
    state = run.approval_payload or {}
    events = run.events or [] if include_details else []
    timeline_events = [
        item for item in events
        if str(item.get('type') or '') not in {'llm.delta', 'llm.received'}
    ]
    status = run.status
    pending = {}
    if include_details and status == 'waiting_approval':
        required = next((x for x in reversed(events) if x.get('type') == 'approval.required'), {})
        pending = {'step_id': required.get('node_id', state.get('pending_node')), 'step_name': required.get('node_name', '人工审核'),
                   'message': required.get('message', '请审核当前节点输出'), 'output': required.get('output', run.output),
                   'outcomes': required.get('outcomes') or ['approved', 'revise'],
                   'default_outcome': required.get('default_outcome')}
    waiting_input = state.get('waiting_input') if include_details and status == 'waiting_input' else None
    runtime_mode = str(state.get('runtime_mode') or ('preview' if state.get('preview') else 'application'))
    checkpoint_nodes = (state.get('checkpoint') or {}).get('nodes') or {}
    final_checkpoint_at = max((
        str(node.get('completed_at') or node.get('started_at') or '')
        for node in checkpoint_nodes.values() if isinstance(node, dict)
    ), default='')

    def event_time(item: dict) -> str:
        if item.get('at'):
            return str(item['at'])
        node_id = str(item.get('node_id') or '')
        node = ((item.get('checkpoint') or {}).get('nodes') or {}).get(node_id) or {}
        if item.get('type') == 'node.completed' and node.get('completed_at'):
            return str(node['completed_at'])
        if node.get('started_at'):
            return str(node['started_at'])
        if str(item.get('type') or '').startswith(('run.', 'files.')) and final_checkpoint_at:
            return final_checkpoint_at
        return run.created_at.isoformat()

    return {'id': run.id, 'workflow_id': str(run.application_id), 'workflow_name': app_row.name if app_row else '应用',
            'idempotency_key': getattr(run, 'idempotency_key', '') or '',
            'status': status, 'inputs': state.get('inputs', {}), 'attachments': state.get('attachment_names', {}),
            'runtime_mode': runtime_mode,
            'conversation_id': run.conversation_id or state.get('conversation_id', ''),
            'user_message': run.input_text if include_details else '',
            'output': run.output if include_details else '',
            'error': run.output if include_details and run.status == 'failed' else '',
            'model': 'workspace_default', 'flow_id': '',
            'metrics': {
                'event_count': len(events),
                **({'runtime_type': state.get('runtime_type')} if state.get('runtime_type') else {}),
            },
            'checkpoint': state.get('checkpoint', {}) if include_details else {},
            'files': artifact_documents(run, app_row, f'/api/runs/{run.id}/files') if include_details else [],
            'pending_feedback': pending, 'waiting_input': waiting_input, 'events': [
                {'at': event_time(item), 'type': item.get('type', 'event'),
                 'event_index': index,
                 'node_id': item.get('node_id') or item.get('step_id') or item.get('task_id') or '',
                 'agent_id': item.get('agent_id') or '',
                 'agent_role': item.get('agent_role') or '',
                 'title': (f"{item.get('tool_name', '工具')} 调用失败"
                           if item.get('type') == 'tool.failed'
                           else item.get('node_name') or item.get('application') or item.get('type', '事件')),
                 'detail': str(item.get('error') or item.get('message') or item.get('output') or '')[:300],
                 **({'tool_name': item.get('tool_name'), 'arguments': item.get('arguments', {})}
                    if item.get('type') == 'tool.failed' else {})}
                for index, item in enumerate(timeline_events)], 'created_at': run.created_at.isoformat()}


def is_workflow_run(run: Run) -> bool:
    """Conversation-router replies are chat turns, not workflow execution."""
    return str((run.approval_payload or {}).get('runtime_type') or '') != 'conversation_router'


def conversation_trace_document(row: ApplicationConversation, runs: list[Run],
                                app_row: Application | None = None, *, include_details: bool = True) -> dict:
    """Present all turns in one conversation as one observable trace."""
    ordered = sorted(runs, key=lambda item: item.created_at)
    documents = [run_document(item, app_row, include_details=include_details) for item in ordered]
    latest = documents[-1] if documents else {
        'status': 'draft', 'output': '', 'error': '', 'events': [], 'files': [],
        'pending_feedback': {}, 'waiting_input': None, 'metrics': {},
    }
    events = []
    files = []
    seen_files = set()
    for turn, (run, document) in enumerate(zip(ordered, documents), start=1):
        for item in document.get('events', []):
            events.append({**item, 'run_id': run.id, 'turn': turn})
        for item in document.get('files', []):
            identity = (run.id, item.get('name'))
            if identity not in seen_files:
                seen_files.add(identity)
                files.append(item)
    workflow_runs = sum(is_workflow_run(item) for item in ordered)
    metrics = {
        **(latest.get('metrics') or {}),
        'turns': len(ordered),
        'workflow_runs': workflow_runs,
        'runtime_modes': list(dict.fromkeys(
            document.get('runtime_mode', 'application')
            for run, document in zip(ordered, documents)
            if is_workflow_run(run)
        )),
    }
    return {
        **latest,
        'id': row.id,
        'trace_id': row.id,
        'conversation_id': row.id,
        'flow_id': row.id,
        'latest_run_id': ordered[-1].id if ordered else '',
        'run_ids': [item.id for item in ordered],
        'run_count': len(ordered),
        'workflow_run_count': workflow_runs,
        'workflow_id': str(row.application_id),
        'workflow_name': app_row.name if app_row else latest.get('workflow_name', '应用'),
        'events': events if include_details else [],
        'files': files if include_details else [],
        'metrics': metrics,
        'created_at': row.created_at.isoformat(),
        'updated_at': row.updated_at.isoformat(),
        'runs': documents,
    }


def conversation_document(row: ApplicationConversation, runs: list[Run] | None = None,
                          app_row: Application | None = None) -> dict:
    return {
        'id': row.id,
        'workflow_id': str(row.application_id),
        'title': row.title,
        'created_at': row.created_at.isoformat(),
        'updated_at': row.updated_at.isoformat(),
        'history_tokens': getattr(row, 'history_tokens', 0) or 0,
        'history_summary': getattr(row, 'history_summary', '') or '',
        'state': getattr(row, 'state', None) or {},
        'runs': [run_document(item, app_row) for item in runs or []],
    }


async def authenticated_run_for_user(db, run_id: str, user: User) -> tuple[Run, Application]:
    """Resolve a run without crossing an authenticated user's conversation."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, '运行不存在')
    app_row = await db.get(Application, run.application_id)
    if not app_row:
        raise HTTPException(404, '应用不存在')
    await workspace_member(db, app_row.workspace_id, user.id)
    if run.conversation_id:
        conversation = await db.get(ApplicationConversation, run.conversation_id)
        if conversation is not None and conversation.user_id != user.id:
            raise HTTPException(404, '运行不存在')
    return run, app_row


async def require_latest_conversation_run(db, run: Run) -> None:
    """Reject retries that could replay an older snapshot over newer turns."""
    if not run.conversation_id:
        return
    latest = await db.scalar(select(Run).where(
        Run.application_id == run.application_id,
        Run.conversation_id == run.conversation_id,
    ).order_by(Run.created_at.desc(), Run.id.desc()).limit(1))
    if not latest or latest.id != run.id:
        raise HTTPException(409, '只能重试当前对话最新一轮运行，请刷新会话后重试')


async def budgeted_run_history(db, app_id: int, conversation, statuses: tuple[str, ...] = ('completed', 'waiting_input')) -> list[dict]:
    """Load one conversation with a deterministic summary and a hard context budget."""
    rows = (await db.scalars(select(Run).where(
        Run.application_id == app_id,
        Run.conversation_id == conversation.id,
        Run.status.in_(statuses),
    ).order_by(Run.created_at))).all()
    full = [
        {'user': item.input_text, 'assistant': item.output}
        for item in rows if item.input_text or item.output
    ]
    kept, summary, tokens = budget_conversation_history(full)
    conversation.history_summary = summary
    conversation.history_tokens = tokens
    if summary:
        return [{'summary': summary, 'user': '', 'assistant': ''}, *kept]
    return kept


async def conversation_workflow_bound(db, app_id: int, conversation) -> bool:
    """Once a conversation enters the workflow, keep all later turns in it."""
    state = dict(getattr(conversation, 'state', None) or {})
    if state.get('workflow_started') or state.get('routing_mode') == 'workflow':
        return True
    prior = (await db.scalars(select(Run).where(
        Run.application_id == app_id,
        Run.conversation_id == conversation.id,
    ))).all()
    return any(is_workflow_run(item) for item in prior)


def update_conversation_state(definition: dict, current_state: dict, inputs: dict,
                              attachments: dict[str, list[str]]) -> tuple[dict, dict, dict, list[dict]]:
    """Merge one multi-turn input patch and compute readiness without an LLM."""
    state = json.loads(json.dumps(current_state or {}, ensure_ascii=False))
    # A completed run is still part of the same conversation.  Keep its
    # collected fields and attachment references until the caller explicitly
    # creates/clears a conversation; only the transient runtime resume state is
    # removed by the worker when the run completes.
    collected = dict(state.get('collected_fields') or {})
    stored_attachments = {
        str(key): list(value or [])
        for key, value in (state.get('attachment_ids') or {}).items()
    }
    stored_paths = {
        str(key): list(value or [])
        for key, value in (state.get('attachment_paths') or {}).items()
    }
    configured = {str(item.get('name')): item for item in definition.get('inputs', [])}
    for name, value in (inputs or {}).items():
        # File names shown by the browser are not durable input values.  The
        # attachment IDs are consumed by enqueue_application_run and replaced
        # with application-relative paths after the run is created.
        if (name in configured
                and configured[name].get('input_type') not in {'file', 'image'}
                and input_is_supplied(value)):
            collected[name] = value
    for name, values in (attachments or {}).items():
        if name in configured and values:
            if configured[name].get('input_type') in {'file', 'image'} or configured[name].get('multiple'):
                stored_attachments[name] = list(dict.fromkeys([
                    *stored_attachments.get(name, []),
                    *list(values),
                ]))
            else:
                stored_attachments[name] = list(values)
                stored_paths.pop(name, None)

    missing = []
    for name, item in configured.items():
        if item.get('required') is False:
            continue
        supplied = (bool(stored_attachments.get(name)) or bool(stored_paths.get(name))
                    or input_is_supplied(collected.get(name))
                    if item.get('input_type') in {'file', 'image'}
                    else input_is_supplied(collected.get(name)))
        if not supplied:
            missing.append({'name': name, 'label': item.get('label') or name})
    state.update({
        'status': 'collecting' if missing else 'ready',
        'collected_fields': collected,
        'missing_fields': [item['name'] for item in missing],
        'attachment_ids': stored_attachments,
        'attachment_paths': stored_paths,
    })
    return state, collected, stored_attachments, missing


def durable_attachment_payload(definition: dict, state: dict) -> dict[str, list[dict]]:
    """Turn persisted app-relative file paths into safe enqueue references."""
    result: dict[str, list[dict]] = {}
    configured = {str(item.get('name')): item for item in definition.get('inputs', []) or []}
    for name, paths in (state.get('attachment_paths') or {}).items():
        item = configured.get(str(name))
        if not item or item.get('input_type') not in {'file', 'image'}:
            continue
        entries = []
        for path in paths or []:
            relative = safe_relative_path(str(path)).as_posix()
            entries.append({'name': Path(relative).name, 'existing_path': relative})
        if entries:
            result[str(name)] = entries
    return result


def merge_run_inputs_into_conversation(state: dict, run: Run, definition: dict) -> dict:
    """Persist consumed inputs without retaining one-shot upload IDs.

    Upload IDs live in Redis only until enqueue_application_run copies the
    bytes into the application workspace.  Conversation state must retain the
    resulting relative paths, otherwise the next ask_user reply would try to
    consume an expired upload again or report the required file as missing.
    """
    result = json.loads(json.dumps(state or {}, ensure_ascii=False))
    collected = dict(result.get('collected_fields') or {})
    runtime_inputs = dict((run.approval_payload or {}).get('inputs') or {})
    attachment_refs = {
        str(key): list(value or [])
        for key, value in (result.get('attachment_paths') or {}).items()
    }
    for item in definition.get('inputs', []) or []:
        name = str(item.get('name') or '')
        if name and input_is_supplied(runtime_inputs.get(name)):
            value = runtime_inputs[name]
            if item.get('input_type') in {'file', 'image'}:
                refs = list(value) if isinstance(value, list) else [value]
                attachment_refs[name] = [str(path) for path in refs if input_is_supplied(path)]
            else:
                collected[name] = value
    result['collected_fields'] = collected
    # After enqueue, file values are application-relative paths, not one-shot
    # Redis upload IDs. Keep those paths so a later chat turn can reuse the
    # first-turn files without asking the user to upload them again.
    result['attachment_ids'] = {}
    result['attachment_paths'] = attachment_refs
    return result


async def owned_conversation(db, conversation_id: str, app_row: Application,
                             user_id: int) -> ApplicationConversation:
    row = await db.get(ApplicationConversation, conversation_id)
    if (not row or row.application_id != app_row.id or row.workspace_id != app_row.workspace_id
            or row.user_id != user_id):
        raise HTTPException(404, '对话不存在')
    return row


def input_is_supplied(value) -> bool:
    if value is None or value == '':
        return False
    if isinstance(value, (list, dict)) and not value:
        return False
    return True


def nonempty_input_patch(definition: dict, inputs: dict | None) -> dict:
    """Keep only durable, supplied input values from one chat turn.

    Chat clients commonly serialize every configured control on every request,
    including empty untouched fields and browser file names. Treating that
    serialization as a replacement would erase values collected earlier.
    Files/images always travel through the attachment channel.
    """
    configured = {
        str(item.get('name')): item for item in (definition or {}).get('inputs', [])
        if item.get('name')
    }
    result = {}
    for name, value in (inputs or {}).items():
        item = configured.get(str(name))
        if not item:
            # Preserve supplied unknown names so the normal run-contract
            # validator can return its explicit 422 instead of silently
            # accepting a misspelled variable.
            if input_is_supplied(value):
                result[str(name)] = value
            continue
        if item.get('input_type') in {'file', 'image'}:
            continue
        if input_is_supplied(value):
            result[str(name)] = value
    return result


def apply_waiting_chat_message(
    inputs: dict,
    message: str,
    waiting_input: dict | None,
    primary_name: str = '',
    *,
    has_attachments: bool = False,
) -> dict:
    """Apply a chat reply without replacing the original conversation prompt.

    Web and public clients commonly send the current chat text twice: once as
    ``message`` and once as the configured primary text input. While
    ``ask_user`` is waiting for another field, that duplicate is not a new
    value for the primary field. File attachments are handled separately by
    ``update_conversation_state`` and therefore remain untouched here.
    """
    result = dict(inputs or {})
    text = str(message or '').strip()
    if not text:
        return result
    target = str((waiting_input or {}).get('input_name') or '').strip()
    # A generic ask_user question is answered through the current chat
    # message. It has no declared field and must not be forced into the first
    # text input or persisted as an unknown contract variable.
    generic_target = target == '__ask_user_reply'
    if generic_target:
        target = ''
    primary = str(primary_name or '').strip()
    if is_upload_only_message(text, has_attachments):
        # Older clients may still mirror the upload placeholder into the
        # primary text field. Drop only that supplied mirror; an empty control
        # remains harmless, and attachment IDs are merged separately.
        if primary and str(result.get(primary) or '').strip() == text:
            result.pop(primary, None)
        return result
    if target and target != primary:
        # The browser mirrors the current message into the primary input for
        # every turn. While another field is pending, that mirror is not a
        # patch to the original request, even when its text differs.
        result.pop(primary, None)
    # Upload-only replies use a UI placeholder as ``message``.  It is not a
    # value for a pending file/image field and must never become a path or a
    # textual answer for that field; attachments are merged separately.
    waiting_type = str((waiting_input or {}).get('input_type') or '').strip()
    if target and waiting_type in {'file', 'image'}:
        return result
    if target:
        result[target] = text
    elif not generic_target and primary and not input_is_supplied(result.get(primary)):
        result[primary] = text
    return result


def validate_run_contract(definition: dict, inputs: dict, attachments: dict[str, list],
                          *, allow_missing_required: bool = False) -> None:
    contract_errors = executable_contract_errors(definition)
    if contract_errors:
        raise HTTPException(422, '运行输入契约无效：' + '；'.join(contract_errors))
    configured = {item.get('name'): item for item in definition.get('inputs', [])}
    unknown = (set(inputs) | set(attachments)) - set(configured)
    if unknown:
        raise HTTPException(422, f"包含未定义的运行变量：{', '.join(sorted(unknown))}")
    missing = []
    for item in configured.values():
        if not item.get('required'):
            continue
        name = item.get('name')
        supplied = bool(attachments.get(name)) if item.get('input_type') in {'file', 'image'} else input_is_supplied(inputs.get(name))
        if not supplied:
            missing.append(item.get('label') or name)
    if missing and not allow_missing_required:
        raise HTTPException(422, f"请提供必填输入：{'、'.join(missing)}")
    for name, items in attachments.items():
        field = configured.get(name, {})
        if field.get('input_type') not in {'file', 'image'}:
            raise HTTPException(422, f'{field.get("label") or name} 不是文件输入')


async def require_model_for_definition(db, workspace_id: int, definition: dict) -> None:
    needs_model = bool(definition.get('agents')) or any(
        item.get('node_type', 'task') in {'task', 'agent', 'crew'} for item in definition.get('tasks', [])
    )
    if not needs_model:
        return
    selected = str(definition.get('model_profile_id') or '')
    if selected.isdigit() and await db.scalar(select(ModelProfile).where(
        ModelProfile.workspace_id == workspace_id, ModelProfile.id == int(selected),
    )):
        return
    if not await db.scalar(select(ModelProfile).where(
        ModelProfile.workspace_id == workspace_id,
        ModelProfile.model_type == 'chat',
        ModelProfile.is_default == True,
    )):
        raise HTTPException(409, '请先设置工作空间默认模型，或为应用选择一个模型')


async def enqueue_application_run(
    app_row: Application,
    definition: dict,
    *,
    message: str,
    inputs: dict,
    attachments: dict[str, list[dict]],
    conversation_id: str = '',
    conversation_history: list[dict] | None = None,
    runtime_resume: dict | None = None,
    external_user_id: str = '',
    idempotency_key: str = '',
    preview: bool = False,
    runtime_mode: str = 'application',
) -> Run:
    app_kind = str(definition.get('kind') or app_row.kind)
    validate_run_contract(
        definition, inputs, attachments,
        allow_missing_required=definition.get('interaction_mode') == 'multi_turn',
    )
    effective_key = str(idempotency_key or secrets.token_urlsafe(18))[:240]
    fingerprint = request_fingerprint({
        'message': message,
        'inputs': inputs,
        'attachments': {
            key: [
                {
                    'name': item.get('name'),
                    'size': len(item.get('data') or b''),
                    'path': item.get('path', ''),
                    'existing_path': item.get('existing_path', ''),
                }
                for item in values
            ]
            for key, values in attachments.items()
        },
        'conversation_id': conversation_id,
        'preview': bool(preview),
        'runtime_mode': runtime_mode,
    })
    async with SessionLocal() as db:
        existing = await db.scalar(select(Run).where(
            Run.application_id == app_row.id,
            Run.idempotency_key == effective_key,
        ))
        if existing:
            existing_fingerprint = str((existing.approval_payload or {}).get('request_fingerprint') or '')
            if existing_fingerprint and existing_fingerprint != fingerprint:
                raise HTTPException(409, '同一幂等键不能用于不同请求')
            return existing
    run_id = secrets.token_urlsafe(12)
    execution_scope = str((runtime_resume or {}).get('execution_scope') or conversation_id or f'run-{run_id}')
    copied: list[str] = []
    existing: list[str] = []
    attachment_names: dict[str, list[str]] = {}
    runtime_inputs = dict(inputs)
    total = 0
    for variable, items in attachments.items():
        paths = []
        attachment_names[variable] = []
        for index, item in enumerate(items):
            existing_path = str(item.get('existing_path') or '').strip()
            if existing_path:
                try:
                    relative = safe_relative_path(existing_path).as_posix()
                    session_root = app_session_dir(
                        app_row.workspace_id, app_row.id, execution_scope, app_kind,
                    )
                    existing_target = resolve_app_file(session_root, relative)
                    if not existing_target.is_file():
                        legacy_target = resolve_app_file(
                            app_dir(app_row.workspace_id, app_row.id, app_kind), relative,
                        )
                        if legacy_target.is_file():
                            existing_target = sync_session_file(
                                app_row.workspace_id, app_row.id, execution_scope,
                                relative, legacy_target.read_bytes(), app_kind,
                            )
                    if not existing_target.is_file():
                        raise ValueError('文件不存在')
                except (ValueError, OSError) as exc:
                    raise HTTPException(422, f'应用内附件路径无效：{existing_path}') from exc
                paths.append(relative)
                existing.append(relative)
                attachment_names[variable].append(str(item.get('name') or Path(relative).name))
                continue
            data = item.get('data')
            if data is None and item.get('path'):
                data = Path(item['path']).read_bytes()
            data = data or b''
            total += len(data)
            if total > settings.max_upload_mb * 1024 * 1024:
                raise HTTPException(413, f'本次上传总大小不能超过 {settings.max_upload_mb} MB')
            original = str(item.get('name') or f'file-{index + 1}')
            filename = safe_name(original)
            relative = f'uploads/{run_id}/{index + 1}-{filename}'
            sync_session_file(
                app_row.workspace_id, app_row.id, execution_scope, relative, data, app_kind,
            )
            copied.append(relative); paths.append(relative); attachment_names[variable].append(original)
        field = next((x for x in definition.get('inputs', []) if x.get('name') == variable), {})
        if field.get('input_type') in {'file', 'image'} or field.get('multiple'):
            previous = runtime_inputs.get(variable)
            previous_paths = list(previous) if isinstance(previous, list) else ([previous] if previous else [])
            runtime_inputs[variable] = previous_paths + [path for path in paths if path not in previous_paths]
        else:
            runtime_inputs[variable] = paths[0] if paths else runtime_inputs.get(variable, '')
    root = app_session_dir(app_row.workspace_id, app_row.id, execution_scope, app_kind)
    display_message = message or str(runtime_inputs.get('message', '')) or ('请处理上传的文件。' if copied else '运行应用。')
    persisted_files = list(dict.fromkeys([
        *((runtime_resume or {}).get('files', []) or []),
        *copied,
        *existing,
    ]))
    async with SessionLocal() as db:
        state = {**(runtime_resume or {}),
                 'files': persisted_files, 'snapshot': app_file_manifest(root),
                 'artifacts': list((runtime_resume or {}).get('artifacts') or []), 'inputs': runtime_inputs,
                 'attachment_names': attachment_names, 'conversation_id': conversation_id,
                 'execution_scope': execution_scope,
                 'conversation_history': conversation_history or [],
                 'request_fingerprint': fingerprint,
                 'preview': bool(preview),
                 'runtime_mode': runtime_mode,
                 **({'user_id': external_user_id} if external_user_id else {})}
        run = Run(id=run_id, application_id=app_row.id, conversation_id=conversation_id or None,
                  idempotency_key=effective_key,
                  status='queued', input_text=display_message,
                  events=[{'type': 'run.queued', 'application': app_row.name,
                           'idempotency_key': effective_key}],
                  approval_payload=state)
        db.add(run)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            for relative in copied:
                delete_session_file(
                    app_row.workspace_id, app_row.id, execution_scope, relative, app_kind,
                )
            existing = await db.scalar(select(Run).where(
                Run.application_id == app_row.id,
                Run.idempotency_key == effective_key,
            ))
            if not existing:
                raise
            existing_fingerprint = str((existing.approval_payload or {}).get('request_fingerprint') or '')
            if existing_fingerprint and existing_fingerprint != fingerprint:
                raise HTTPException(409, '同一幂等键不能用于不同请求')
            return existing
        await db.refresh(run)
    await redis.hset(f'run:{run_id}', mapping={'status': 'queued', 'application': app_row.name})
    await redis.lpush(RUN_QUEUE, run_id)
    return run


def reset_run_for_manual_retry(run: Run) -> None:
    """Start a fresh execution attempt while preserving user inputs/files.

    A normal resume keeps completed checkpoints. A user-requested retry must
    clear those checkpoints, otherwise Flow skips every node and Crew tool
    idempotency keys can replay the previous attempt without doing work.
    """
    state = dict(run.approval_payload or {})
    retry_count = int(run.retry_count or 0) + 1
    state['outputs'] = {}
    state['pending_node'] = None
    state['waiting_input'] = None
    state['decision'] = None
    state['node_artifacts'] = {}
    state['artifacts'] = []
    state['skill_execution_receipts'] = []
    state['execution_id'] = f'{run.id}:manual-retry:{retry_count}:{secrets.token_hex(4)}'
    state['run_attempt'] = retry_count
    state['checkpoint'] = {
        'schema_version': 1,
        'status': 'ready',
        'current_node': None,
        'outputs': {},
        'nodes': {},
        'transition_version': 0,
        'waiting_input': None,
        'waiting_approval': None,
        'history_summary': state.get('history_summary', ''),
        'history_tokens': int(state.get('history_tokens') or 0),
    }
    run.approval_payload = state
    run.status = 'queued'
    run.retry_count = retry_count
    run.output = ''
    run.events = list(run.events or []) + [
        {'type': 'run.manual_retry', 'message': '用户请求重试', 'run_attempt': retry_count}
    ]


async def run_event_response(run_id: str, after_event: int = 0, *, plan=None, completed_files=None, completed_document=None):
    async def load_run():
        async with SessionLocal() as db:
            return await db.get(Run, run_id)

    async def stream():
        initial = {'type': 'plan', 'run_id': run_id, 'steps': plan} if plan is not None else None
        async for frame in stream_run_frames(
            load_run, after_event=after_event, initial_frame=initial,
            completed_files=completed_files, completed_document=completed_document,
        ):
            yield encode_run_event(frame) if frame is not None else ': keep-alive\n\n'

    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
    })
