"""Workspace, application and resource records: documents, usage counts and deletion.
"""
import ast
import base64
import binascii
import json
import logging
from .config import (
    settings,
)
from .crypto import (
    decrypt_secret,
)
from .db import (
    ApiKey,
    Application,
    ApplicationAgent,
    ApplicationAgentResource,
    ApplicationConversation,
    ApplicationInput,
    ApplicationTask,
    ApplicationTaskDependency,
    DesignSession,
    ExternalConversation,
    KnowledgeBase,
    KnowledgeFile,
    ModelProfile,
    Plugin,
    Run,
    Skill,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from .knowledge import (
    delete_collection as delete_knowledge_collection,
)
from .persistence import (
    read_application,
)
from .services import (
    parse_skill_manifest,
    redis,
    remove_app_dir,
    remove_minio_prefix,
    remove_object_prefix,
    remove_workspace_dir,
    safe_relative_path,
)
from .studio_proposals import (
    _compact_studio_message,
    canonicalize_studio_proposal,
    is_conversation_only_proposal,
    is_legacy_conversation_only_session,
)
from fastapi import (
    HTTPException,
)
from sqlalchemy import (
    delete,
    select,
)
from urllib.parse import (
    urlparse,
)


ALLOWED_PLUGIN_KINDS = {'http', 'python', 'mcp_http', 'mcp_sse', 'app'}


def validate_plugin_document(body: dict) -> None:
    kind = str(body.get('kind') or 'http')
    if kind == 'mcp_stdio':
        raise HTTPException(422, '本地命令 MCP 会绕过统一隔离执行器，玄枢仅支持远程 HTTP/SSE MCP')
    if kind not in ALLOWED_PLUGIN_KINDS:
        raise HTTPException(422, '不支持的工具类型')
    if not str(body.get('name', '')).strip() or not str(body.get('description', '')).strip():
        raise HTTPException(422, '工具名称和说明不能为空')
    endpoint = str(body.get('endpoint') or body.get('server_url') or '')
    if kind in {'http', 'mcp_http', 'mcp_sse'} and urlparse(endpoint).scheme not in {'http', 'https'}:
        raise HTTPException(422, '服务地址必须是有效的 HTTP 或 HTTPS URL')
    if kind == 'http' and str(body.get('method', 'POST')).upper() not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
        raise HTTPException(422, '不支持的 HTTP method')
    if kind == 'python':
        source = str(body.get('source_code') or '')
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise HTTPException(422, f'Python Tool 语法错误：{exc.msg}') from exc
        if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {'run', 'main'} for node in tree.body):
            raise HTTPException(422, 'Python Tool 必须定义 run(**kwargs) 或 main(**kwargs)')
    if kind == 'app':
        if not str(body.get('app_slug') or '').strip():
            raise HTTPException(422, 'Connected App 名称不能为空')
        if not settings.crewai_platform_integration_token:
            raise HTTPException(409, '服务端尚未配置 CREWAI_PLATFORM_INTEGRATION_TOKEN')


async def delete_application_records(db, row: Application, *, commit: bool = True) -> tuple[int, int, list[str]]:
    workspace_id, application_id = row.workspace_id, row.id
    run_ids = list((await db.scalars(select(Run.id).where(Run.application_id == application_id))).all())
    # A design session belongs to the application it created.  Detaching it
    # leaves stale cards in the Studio "recent projects" list and can later
    # overwrite a freshly saved application draft when that app is opened.
    # Remove the sessions with the application in the same transaction.
    await db.execute(delete(DesignSession).where(DesignSession.application_id == application_id))
    await db.execute(delete(ApiKey).where(ApiKey.application_id == application_id))
    await db.execute(delete(Run).where(Run.application_id == application_id))
    await db.execute(delete(ApplicationConversation).where(
        ApplicationConversation.application_id == application_id))
    # Older deployments have NO ACTION foreign keys for several application
    # relations.  Clean up every owned row explicitly before deleting the
    # parent so old conversations or draft graph rows cannot block deletion.
    await db.execute(delete(ExternalConversation).where(
        ExternalConversation.application_id == application_id))
    await db.execute(delete(ApplicationAgentResource).where(
        ApplicationAgentResource.application_id == application_id))
    await db.execute(delete(ApplicationTaskDependency).where(
        ApplicationTaskDependency.application_id == application_id))
    await db.execute(delete(ApplicationTask).where(
        ApplicationTask.application_id == application_id))
    await db.execute(delete(ApplicationAgent).where(
        ApplicationAgent.application_id == application_id))
    await db.execute(delete(ApplicationInput).where(
        ApplicationInput.application_id == application_id))
    await db.delete(row)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return workspace_id, application_id, run_ids


async def mark_design_sessions_deleted(session_ids: list[str]) -> None:
    """Stop queued Studio workers from recreating sessions deleted with an app."""
    if not session_ids:
        return
    try:
        for session_id in session_ids:
            await redis.set(f'xuanshu:studio:deleted:{session_id}', '1', ex=86400)
        await redis.delete(*(f'xuanshu:composer-flow:{session_id}' for session_id in session_ids))
    except Exception:
        logging.exception('failed to clear deleted Studio sessions')


async def delete_workspace_records(db, workspace: Workspace) -> list[str]:
    applications = (await db.scalars(select(Application).where(Application.workspace_id == workspace.id))).all()
    knowledge_bases = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.workspace_id == workspace.id))).all()
    run_ids: list[str] = []
    for application in applications:
        remove_minio_prefix(workspace.id, application.id)
        remove_app_dir(workspace.id, application.id, application.kind)
        session_ids = list((await db.scalars(select(DesignSession.id).where(
            DesignSession.application_id == application.id,
        ))).all())
        await mark_design_sessions_deleted(session_ids)
        _, _, application_runs = await delete_application_records(db, application, commit=False)
        run_ids.extend(application_runs)
    for knowledge_base in knowledge_bases:
        delete_knowledge_collection(workspace.id, knowledge_base.id)
    remove_object_prefix(f'workspaces/{workspace.id}/')
    remove_workspace_dir(workspace.id)
    await db.execute(delete(KnowledgeFile).where(KnowledgeFile.workspace_id == workspace.id))
    await db.execute(delete(KnowledgeBase).where(KnowledgeBase.workspace_id == workspace.id))
    await db.execute(delete(ApiKey).where(ApiKey.workspace_id == workspace.id))
    await db.execute(delete(DesignSession).where(DesignSession.workspace_id == workspace.id))
    await db.execute(delete(ModelProfile).where(ModelProfile.workspace_id == workspace.id))
    await db.execute(delete(Skill).where(Skill.workspace_id == workspace.id))
    await db.execute(delete(Plugin).where(Plugin.workspace_id == workspace.id))
    await db.execute(delete(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == workspace.id))
    await db.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace.id))
    await db.delete(workspace)
    await db.flush()
    return run_ids


async def model_usage_count(db, workspace_id: int, model_id: int) -> int:
    """Count applications that reference a model in application or agent settings."""
    applications = (await db.scalars(select(Application).where(Application.workspace_id == workspace_id))).all()
    used_by = 0
    for application in applications:
        document = await read_application(db, application)
        references = {
            document.get('model_profile_id'),
            document.get('manager_model_profile_id'),
            document.get('planning_model_profile_id'),
        }
        for agent in document.get('agents', []):
            references.update({agent.get('model_profile_id'), agent.get('function_calling_model_profile_id')})
        if str(model_id) in {str(value) for value in references if value not in {None, ''}}:
            used_by += 1
    return used_by


async def resource_usage_count(db, workspace_id: int, resource_type: str, resource_id: int) -> int:
    application_ids = set((await db.scalars(select(Application.id).where(
        Application.workspace_id == workspace_id,
    ))).all())
    if not application_ids:
        return 0
    referenced = set((await db.scalars(select(ApplicationAgentResource.application_id).where(
        ApplicationAgentResource.application_id.in_(application_ids),
        ApplicationAgentResource.resource_type == resource_type,
        ApplicationAgentResource.resource_id == resource_id,
    ))).all())
    return len(referenced)


def overview_run_summary(item, workflow_names):
    state = item.approval_payload or {}
    return {
        'id': item.id, 'conversation_id': str(item.conversation_id or ''),
        'workflow_id': str(item.application_id),
        'workflow_name': workflow_names.get(item.application_id, '应用'),
        'status': item.status,
        'runtime_mode': state.get('runtime_mode') or ('preview' if state.get('preview') else 'application'),
        'metrics': {'runtime_type': state.get('runtime_type')} if state.get('runtime_type') else {},
        'created_at': item.created_at.isoformat(), 'updated_at': item.created_at.isoformat(),
    }


def design_session_document(row: DesignSession, *, detail: bool = False,
                            message_limit: int = 30) -> dict:
    first_message = next(
        (item.get('content', '') for item in (row.messages or []) if item.get('role') == 'user'),
        '',
    )
    active_job = dict(row.active_job or {})
    active_job.pop('request', None)
    if row.application_id:
        result_state = dict(active_job.get('result') or {})
        result_state.pop('workflow', None)
        result_state.pop('proposal', None)
        active_job['result'] = result_state
    conversation_only = (
        is_conversation_only_proposal(row.proposal)
        or is_legacy_conversation_only_session(row)
    )
    if conversation_only and isinstance(active_job.get('result'), dict):
        active_result = dict(active_job['result'])
        active_result.pop('proposal', None)
        active_result.pop('workflow', None)
        active_job['result'] = active_result
    result = {
        'id': row.id,
        'name': row.title,
        'kind': row.kind,
        'stage': row.stage,
        'status': row.status,
        'application_id': str(row.application_id) if row.application_id else None,
        'created_at': row.created_at.isoformat(),
        'updated_at': row.updated_at.isoformat(),
        'description': first_message or row.title,
        'active_job': active_job,
        'history_summary': getattr(row, 'history_summary', '') or '',
        'history_tokens': getattr(row, 'history_tokens', 0) or 0,
    }
    if detail:
        messages = [_compact_studio_message(item) for item in list(row.messages or [])[-max(1, int(message_limit)):]]
        if conversation_only:
            messages = [
                {**item, 'proposal': None, 'clarification': None}
                if isinstance(item, dict) else item
                for item in messages
            ]
        result.update({
            'messages': messages,
            'proposal': {} if conversation_only or row.application_id else canonicalize_studio_proposal(row.proposal),
        })
    return result


async def resolve_design_session(db, identifier: str | int) -> DesignSession | None:
    """Resolve a real session id, or the unique session bound to an app id."""
    value = str(identifier)
    row = await db.get(DesignSession, value)
    if not row and value.isdigit():
        row = await db.scalar(select(DesignSession).where(
            DesignSession.application_id == int(value),
        ))
    return row


def user_document(row: User, workspace_id: int | None = None) -> dict:
    result = {
        'id': row.id, 'username': row.username, 'is_admin': row.is_admin,
        'created_at': row.created_at.isoformat(),
    }
    if workspace_id is not None:
        result['workspace_id'] = workspace_id
    return result


def public_model(row: ModelProfile) -> dict:
    secret = decrypt_secret(row.api_key_encrypted)
    return {'id': str(row.id), 'name': row.name, 'provider': row.provider, 'model': row.model, 'model_type': row.model_type,
            'supports_vision': bool(getattr(row, 'supports_vision', False)),
            'base_url': row.base_url, 'is_default': row.is_default, 'has_api_key': bool(secret),
            'key_hint': (f'{secret[:3]}…{secret[-3:]}' if len(secret) > 7 else ('已配置' if secret else '')),
            'temperature': row.temperature, 'max_tokens': row.max_tokens,
            'timeout': row.timeout_seconds, 'max_retries': row.max_retries,
            'thinking_mode': getattr(row, 'thinking_mode', 'auto') or 'auto',
            'thinking_effort': getattr(row, 'thinking_effort', None)}


def knowledge_document(row: KnowledgeBase, files: list[KnowledgeFile] | None = None) -> dict:
    return {'id': str(row.id), 'name': row.name, 'description': row.description,
            'embedding_model_id': str(row.embedding_model_id), 'parsing_strategy': row.parsing_strategy,
            'chunk_size': row.chunk_size, 'chunk_overlap': row.chunk_overlap, 'status': row.status,
            'created_at': row.created_at.isoformat(), 'updated_at': row.updated_at.isoformat(),
            'files': [{'id': str(item.id), 'name': item.name, 'content_type': item.content_type,
                       'size': item.size, 'chunk_count': item.chunk_count, 'status': item.status,
                       'error': item.error, 'created_at': item.created_at.isoformat()} for item in files or []]}


def normalize_skill_package(body: dict) -> dict:
    """Normalize the editable package and reject paths that materialize differently."""
    result = json.loads(json.dumps(body or {}, ensure_ascii=False))
    result.pop('category', None)
    name = str(result.get('name') or '').strip()
    description = str(result.get('description') or '').strip()
    instructions = str(result.get('instructions') or '').strip()
    slug = str(result.get('slug') or '').strip()
    if not name or not description or not instructions:
        raise ValueError('Skill 名称、触发说明和指令不能为空')
    manifest = (
        f'---\nname: {slug}\n'
        f'description: {json.dumps(description, ensure_ascii=False)}\n'
        f'---\n\n{instructions}'
    )
    parse_skill_manifest(manifest)

    def package_path(value, label: str) -> str:
        raw = str(value or '').replace('\\', '/').strip('/')
        try:
            normalized = safe_relative_path(raw).as_posix()
        except ValueError as exc:
            raise ValueError(f'{label}路径不安全：{raw}') from exc
        if normalized != raw or raw == 'SKILL.md':
            raise ValueError(f'{label}路径无效：{raw}')
        return raw

    directories: set[str] = set()
    for value in result.get('directories', []) or []:
        path = package_path(value, '目录')
        parts = path.split('/')
        directories.update('/'.join(parts[:index]) for index in range(1, len(parts) + 1))

    files = []
    paths: set[str] = set()
    for item in result.get('files', []) or []:
        if not isinstance(item, dict):
            raise ValueError('Skill 文件定义必须是对象')
        path = package_path(item.get('path'), '文件')
        if path in paths:
            raise ValueError(f'Skill 中存在重复文件路径：{path}')
        paths.add(path)
        parent_parts = path.split('/')[:-1]
        directories.update(
            '/'.join(parent_parts[:index])
            for index in range(1, len(parent_parts) + 1)
        )
        encoding = str(item.get('encoding') or 'utf8')
        content = item.get('content', '')
        if encoding not in {'utf8', 'base64'}:
            raise ValueError(f'文件 {path} 的编码只能是 utf8 或 base64')
        if not isinstance(content, str):
            raise ValueError(f'文件 {path} 的内容必须是字符串')
        if encoding == 'base64':
            try:
                base64.b64decode(content, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError(f'文件 {path} 的 Base64 内容无效') from exc
        kind = (
            'script' if path == 'scripts' or path.startswith('scripts/')
            else 'asset' if path == 'assets' or path.startswith('assets/')
            else 'reference'
        )
        files.append({
            **item,
            'path': path,
            'kind': kind,
            'content': content,
            'encoding': encoding,
            'executable': bool(item.get('executable', kind == 'script')),
        })
    collision = paths & directories
    if collision:
        raise ValueError(f'路径同时被用作文件和目录：{sorted(collision)[0]}')
    result.update({
        'name': name,
        'slug': slug,
        'description': description,
        'instructions': instructions,
        'files': files,
        'directories': sorted(directories),
    })
    return result
