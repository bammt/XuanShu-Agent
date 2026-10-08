"""Application draft documents and persistence.

Builds, validates and stores application workflow documents. Moved out of
``api.py`` so that module keeps only HTTP entry points.
"""
import json
import re
from .contracts import (
    ensure_fixed_output_contracts,
    ensure_variable_contract,
    execution_graph,
)
from .db import (
    Application,
    DesignSession,
    KnowledgeBase,
    ModelProfile,
    Plugin,
    Skill,
)
from .persistence import (
    read_application,
    write_application,
)
from .resources import (
    public_plugin_configuration,
    runtime_plugin_configuration,
)
from .schemas import (
    ApplicationDefinition,
)
from .services import (
    materialize_application_resources,
    relocate_app_root,
    safe_name,
)
from .studio_contracts import (
    ensure_message_task_reference,
    normalize_studio_definition,
    normalize_studio_input_contract,
)
from .studio_proposals import (
    canonicalize_studio_proposal,
    draft_sync_document,
)
from datetime import (
    UTC,
    datetime,
)
from fastapi import (
    HTTPException,
)
from sqlalchemy import (
    select,
    text,
)


def skill_document(row: Skill) -> dict:
    data = row.content if isinstance(row.content, dict) else {}
    if data.get('instructions') is not None:
        return {
            'id': str(row.id),
            'revision': int(getattr(row, 'revision', 1) or 1),
            'updated_at': (
                getattr(row, 'updated_at', None)
                or datetime.now(UTC).replace(tzinfo=None)
            ).isoformat(),
            **data,
        }
    return {'id': str(row.id), 'name': row.name, 'slug': safe_name(row.name).lower(), 'description': row.description,
            'instructions': '', 'version': '1.0.0', 'author': 'local',
            'source': 'local', 'registry_ref': '', 'files': [], 'enabled': True, 'status': 'published',
            'revision': int(getattr(row, 'revision', 1) or 1),
            'updated_at': (getattr(row, 'updated_at', None) or datetime.now(UTC).replace(tzinfo=None)).isoformat()}


def plugin_document(row: Plugin, *, runtime: bool = False) -> dict:
    configuration = (runtime_plugin_configuration if runtime else public_plugin_configuration)(row.configuration)
    configuration.pop('category', None)
    return {'id': str(row.id), 'name': row.name, 'kind': row.kind, **configuration}


async def sync_design_sessions_for_application(
    db, application_id: int, definition: dict, manual_changes: list[dict] | None = None,
) -> None:
    """Refresh every chat session attached to an application draft."""
    rows = (await db.scalars(select(DesignSession).where(
        DesignSession.application_id == application_id,
    ))).all()
    if not rows:
        return
    sync = draft_sync_document(definition, manual_changes)
    projection = sync['workflow']
    for row in rows:
        proposal = canonicalize_studio_proposal(row.proposal)
        proposal.update(json.loads(json.dumps(projection, ensure_ascii=False)))
        proposal['draft_sync'] = json.loads(json.dumps(sync, ensure_ascii=False))
        proposal['structure_confirmed'] = True
        proposal['stage'] = 'generation'
        proposal['confirmed_stages'] = list(dict.fromkeys([
            *proposal.get('confirmed_stages', []), 'inputs', 'architecture',
        ]))
        row.proposal = canonicalize_studio_proposal(proposal)
        row.stage = 'generation'
        row.status = 'generated'
        if definition.get('kind') in {'crew', 'flow'}:
            row.kind = definition['kind']
        if str(definition.get('name') or '').strip():
            row.title = str(definition['name']).strip()[:200]
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)


def workflow_document(row: Application, definition: dict | None = None) -> dict:
    definition = definition or {}
    document = {
        'id': str(row.id),
        'name': definition.get('name') or row.name,
        'description': definition.get('description', ''),
        'kind': definition.get('kind') or row.kind,
        'process': definition.get('process', 'sequential'),
        'planning': definition.get('planning', False),
        'planning_model_profile_id': definition.get('planning_model_profile_id'),
        'memory': definition.get('memory', False),
        'memory_policy': definition.get('memory_policy', {
            'conversation_history': True, 'runtime_checkpoint': True,
            'long_term_semantic': definition.get('memory', False),
        }),
        'cache': definition.get('cache', True),
        'verbose': definition.get('verbose', False),
        'output_log_file': definition.get('output_log_file', ''),
        'manager_agent_id': definition.get('manager_agent_id'),
        'manager_model_profile_id': definition.get('manager_model_profile_id'),
        'max_method_calls': definition.get('max_method_calls', 100),
        'model': definition.get('model', 'workspace_default'),
        'model_profile_id': definition.get('model_profile_id'),
        'status': 'published' if row.published else 'draft',
        'published': bool(row.published),
        'draft_revision': int(getattr(row, 'draft_revision', 1) or 1),
        'public_token': row.public_token,
        'agents': definition.get('agents', []),
        'tasks': definition.get('tasks', []),
        'inputs': normalize_studio_input_contract(
            definition.get('inputs', []), definition.get('interaction_mode'),
        ),
        'tags': definition.get('tags', []),
        'chat_history': definition.get('chat_history', []),
        'interaction_mode': definition.get('interaction_mode', 'single_run'),
        'interaction': definition.get('interaction', {}),
        # Keep the confirmed application-level resource plan in the document
        # returned to the builder. Without these fields a reload loses the
        # capability-card selection even though the relational bindings exist.
        'capability_requirements': definition.get('capability_requirements', []),
        'tools': definition.get('tools', []),
        'draft_sync': definition.get('draft_sync', {}),
        'structure_confirmed': definition.get('structure_confirmed', True),
        'created_at': (row.created_at or datetime.now(UTC).replace(tzinfo=None)).isoformat(),
        'updated_at': (row.updated_at or datetime.now(UTC).replace(tzinfo=None)).isoformat(),
    }
    document['execution_graph'] = execution_graph(document)
    return document


def workflow_definition(document: dict) -> dict:
    # execution_graph is a derived view. Persist task dependencies only, then
    # regenerate the graph so canvas, code generation and runtime cannot drift.
    excluded = {
        'id', 'name', 'kind', 'status', 'published', 'created_at', 'updated_at',
        'execution_graph', 'draft_revision', 'studio_session', 'chat_history',
        '_manual_changes', '_base_revision',
    }
    return {key: value for key, value in document.items() if key not in excluded}


def workflow_name(document: dict) -> str:
    name = str(document.get('name') or '').strip()
    if name and name not in {'新智能体', '未命名智能体', 'Untitled automation', 'Untitled agent'}:
        return name[:160]
    candidates = [
        document.get('description'),
        next((item.get('content') for item in document.get('chat_history', []) if item.get('role') == 'user'), ''),
        next((item.get('name') for item in document.get('tasks', []) if item.get('name')), ''),
        next((item.get('role') for item in document.get('agents', []) if item.get('role')), ''),
    ]
    for value in candidates:
        text = str(value or '').strip()
        if not text:
            continue
        first_line = text.splitlines()[0][:18].rstrip('，。；：,. ')
        if first_line:
            return first_line
    return '新智能体'


def validate_flow_tool_document(task, plugin) -> None:
    """Validate one Flow tool node against the selected registry schema."""
    if plugin is None or plugin.kind not in {'http', 'python'}:
        raise HTTPException(422, f'工具节点“{task.name}”必须选择当前工作空间中可执行的 HTTP 或 Python 工具')
    schema = (plugin.configuration or {}).get('input_schema') or {}
    properties = schema.get('properties') if isinstance(schema, dict) else {}
    properties = properties if isinstance(properties, dict) else {}
    declared = set(properties)
    bound = set(task.input_bindings)
    unknown = sorted(bound - declared)
    if unknown:
        raise HTTPException(422, f'工具节点“{task.name}”包含工具未声明的输入：{", ".join(unknown)}')
    required = set(schema.get('required') or []) if isinstance(schema, dict) else set()
    missing = sorted(required - bound)
    if missing:
        raise HTTPException(422, f'工具节点“{task.name}”缺少工具必填输入：{", ".join(missing)}')


async def validate_application_resources(db, workspace_id: int, definition: ApplicationDefinition) -> None:
    model_ids = {
        value for value in [definition.model_profile_id, definition.manager_model_profile_id, definition.planning_model_profile_id]
        if value and str(value).isdigit()
    }
    skill_ids: set[int] = set()
    plugin_ids: set[int] = set()
    flow_tool_ids: set[int] = set()
    knowledge_ids: set[int] = set()
    for agent in definition.agents:
        for value in (agent.model_profile_id, agent.function_calling_model_profile_id):
            if value and str(value).isdigit(): model_ids.add(str(value))
        skill_ids.update(int(value) for value in agent.skills if str(value).isdigit())
        plugin_ids.update(int(value) for value in agent.plugins if str(value).isdigit())
        knowledge_ids.update(int(value) for value in agent.knowledge_base_ids if str(value).isdigit())
    for task in definition.tasks:
        for value in (
            task.crew_manager_model_profile_id,
            task.crew_planning_model_profile_id,
        ):
            if value and str(value).isdigit():
                model_ids.add(str(value))
        if task.node_type == 'tool' and task.tool_id and str(task.tool_id).isdigit():
            flow_tool_ids.add(int(task.tool_id))
    if model_ids:
        found = set((await db.scalars(select(ModelProfile.id).where(
            ModelProfile.workspace_id == workspace_id, ModelProfile.id.in_([int(value) for value in model_ids]),
        ))).all())
        if found != {int(value) for value in model_ids}:
            raise HTTPException(422, '应用引用了当前工作空间不存在的模型')
    if skill_ids:
        found = set((await db.scalars(select(Skill.id).where(
            Skill.workspace_id == workspace_id, Skill.id.in_(skill_ids),
        ))).all())
        if found != skill_ids:
            raise HTTPException(422, '应用引用了当前工作空间不存在的 Skill')
    if flow_tool_ids:
        plugin_ids.update(flow_tool_ids)
    if plugin_ids:
        found = set((await db.scalars(select(Plugin.id).where(
            Plugin.workspace_id == workspace_id, Plugin.id.in_(plugin_ids),
        ))).all())
        if found != plugin_ids:
            raise HTTPException(422, '应用引用了当前工作空间不存在的工具')
    if flow_tool_ids:
        rows = (await db.scalars(select(Plugin).where(
            Plugin.workspace_id == workspace_id, Plugin.id.in_(flow_tool_ids),
        ))).all()
        by_id = {int(row.id): row for row in rows}
        for task in definition.tasks:
            if task.node_type != 'tool' or not task.tool_id or not str(task.tool_id).isdigit():
                continue
            validate_flow_tool_document(task, by_id.get(int(task.tool_id)))
    if knowledge_ids:
        found = set((await db.scalars(select(KnowledgeBase.id).where(
            KnowledgeBase.workspace_id == workspace_id, KnowledgeBase.id.in_(knowledge_ids),
            KnowledgeBase.status == 'ready',
        ))).all())
        if found != knowledge_ids:
            raise HTTPException(422, '应用引用了不存在或尚未完成解析的知识库')


def _resource_lookup(rows) -> dict[str, str]:
    """Build a stable id lookup from both ids and human-facing resource names."""
    lookup: dict[str, str] = {}
    for row in rows:
        resource_id = str(row.id)
        lookup[resource_id] = resource_id
        for value in (getattr(row, 'name', ''), getattr(row, 'slug', '')):
            normalized = re.sub(r'\s+', '', str(value or '').strip()).casefold()
            if normalized:
                lookup[normalized] = resource_id
    return lookup


async def normalize_application_resources(db, workspace_id: int, definition: dict) -> dict:
    """Resolve model-emitted resource labels before relational materialization.

    Composer output is allowed to be human-readable, but the runtime relation
    tables require numeric resource ids. Unknown labels are discarded rather
    than causing an unhandled ``int()`` exception. Selected capability-card
    resources are application-level defaults; explicit Agent bindings are kept.
    """
    skills = (await db.scalars(select(Skill).where(Skill.workspace_id == workspace_id))).all()
    plugins = (await db.scalars(select(Plugin).where(Plugin.workspace_id == workspace_id))).all()
    knowledge = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.workspace_id == workspace_id))).all()
    lookups = {
        'skills': _resource_lookup(skills),
        'plugins': _resource_lookup(plugins),
        'knowledge_base_ids': _resource_lookup(knowledge),
    }
    result = json.loads(json.dumps(definition or {}, ensure_ascii=False))
    agents = result.get('agents', []) or []
    for agent in agents:
        for key, lookup in lookups.items():
            values = agent.get(key, []) or []
            resolved = []
            for value in values:
                raw = str(value or '').strip()
                if not raw:
                    continue
                resource_id = lookup.get(raw)
                if resource_id is None:
                    resource_id = lookup.get(re.sub(r'\s+', '', raw).casefold())
                if resource_id and resource_id not in resolved:
                    resolved.append(resource_id)
            agent[key] = resolved

    selected_by_type = {'skills': [], 'plugins': [], 'knowledge_base_ids': []}
    type_keys = {'skill': 'skills', 'tool': 'plugins', 'knowledge': 'knowledge_base_ids'}
    for requirement in result.get('capability_requirements', []) or []:
        key = type_keys.get(requirement.get('resource_type'))
        if not key:
            continue
        for value in requirement.get('selected_ids', []) or []:
            resource_id = lookups[key].get(str(value).strip())
            if resource_id and resource_id not in selected_by_type[key]:
                selected_by_type[key].append(resource_id)
    # Capability-card selections are requirements, not implicit Agent
    # bindings. Preserve only the explicit bindings already present in the
    # document; generation review is responsible for asking the model to bind
    # every required resource to the Agent that actually uses it.
    result['agents'] = agents
    flow_tool_ids = [
        str(task.get('tool_id')) for task in result.get('tasks', []) or []
        if isinstance(task, dict) and task.get('node_type') == 'tool' and task.get('tool_id')
    ]
    result['tools'] = list(dict.fromkeys([
        *(value for value in result.get('tools', []) or []
          if str(value).strip() in set(selected_by_type['plugins'])),
        *(lookups['plugins'].get(value, value) for value in flow_tool_ids),
    ]))
    return result


async def persist_application_draft(
    db,
    workspace_id: int,
    document: dict,
    *,
    application: Application | None = None,
    session: DesignSession | None = None,
    manual_changes: list[dict] | None = None,
    expected_revision: int | None = None,
) -> tuple[Application, dict]:
    """Validate and persist one authoritative application draft.

    Both canvas saves and Composer generation use this function so a generated
    graph cannot exist only in ``DesignSession.active_job``. The caller owns
    the transaction and decides when to commit.
    """
    name = workflow_name(document)
    kind = str(document.get('kind') or 'crew')
    if kind not in {'crew', 'flow'}:
        raise HTTPException(422, '编排类型必须是 crew 或 flow')
    definition = workflow_definition(document)
    # The relational definition omits the duplicated application kind; expose
    # it while normalizing Flow assignment fields before validation.  Flow
    # Agent/Crew ownership is represented by canvas edges, never a hidden
    # default ``agent_id``.
    definition['kind'] = kind
    normalize_studio_definition(definition, normalize_names=False)
    # A tool node is itself a resource binding. Keep its selected tool in the
    # application resource plan so reload, publish and runtime all agree even
    # when the user added the node directly on the canvas.
    selected_flow_tools = [
        str(task.tool_id) for task in definition.get('tasks', []) or []
        if isinstance(task, dict) and task.get('node_type') == 'tool' and task.get('tool_id')
    ]
    if selected_flow_tools:
        definition['tools'] = list(dict.fromkeys([
            *(str(value) for value in definition.get('tools', []) or []),
            *selected_flow_tools,
        ]))
    definition['inputs'] = normalize_studio_input_contract(
        definition.get('inputs', []), definition.get('interaction_mode'),
    )
    if not definition.get('tasks'):
        raise HTTPException(422, '应用至少需要一个可执行 Task')
    if kind == 'crew' and not definition.get('agents'):
        raise HTTPException(422, 'Crew 应用至少需要一个 Agent')
    ensure_message_task_reference(definition)
    ensure_fixed_output_contracts(definition)
    ensure_variable_contract(definition)
    definition = await normalize_application_resources(db, workspace_id, definition)
    try:
        validated = ApplicationDefinition.model_validate(definition)
    except Exception as exc:
        raise HTTPException(422, f'应用编排无效：{exc}') from exc
    await validate_application_resources(db, workspace_id, validated)
    # ``kind`` is an application-level field and is not stored in the task
    # relations, but it must remain present through validation so Flow-only
    # constraints (including the ban on embedded Crew nodes) are enforced.
    definition.pop('kind', None)

    row = application
    if row and row.workspace_id != workspace_id:
        raise HTTPException(403, '应用不属于当前工作空间')
    if row and expected_revision is not None:
        current_revision = int(getattr(row, 'draft_revision', 1) or 1)
        if current_revision != expected_revision:
            raise HTTPException(
                409,
                '应用草稿已被其他操作更新，请刷新后在最新画布上继续修改',
            )
    old_kind = row.kind if row else kind
    if not row:
        row = Application(
            workspace_id=workspace_id,
            name=name,
            kind=kind,
            draft_revision=1,
        )
        db.add(row)
        await db.flush()
    else:
        row.draft_revision = int(getattr(row, 'draft_revision', 1) or 1) + 1
    if row.published and not getattr(row, 'published_config', None):
        row.published_config = await read_application(db, row)

    sync_definition = {
        **definition,
        'name': name,
        'kind': kind,
        'description': document.get('description', ''),
    }
    definition['draft_sync'] = draft_sync_document(sync_definition, manual_changes)
    row.name = name
    row.kind = kind
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await write_application(db, row, definition)

    app_root = relocate_app_root(workspace_id, row.id, old_kind, kind)
    selected_skill_ids = {
        str(skill_id) for agent in validated.agents for skill_id in agent.skills
        if str(skill_id).isdigit()
    }
    skill_rows = (await db.scalars(select(Skill).where(
        Skill.workspace_id == workspace_id,
        Skill.id.in_([int(skill_id) for skill_id in selected_skill_ids])
        if selected_skill_ids else text('false'),
    ))).all()
    materialize_application_resources(
        app_root,
        {str(item.id): skill_document(item) for item in skill_rows},
        selected_skill_ids,
        include_code=any(agent.allow_code_execution for agent in validated.agents),
        refresh=True,
    )

    if session:
        bound = await db.scalar(select(DesignSession).where(
            DesignSession.application_id == row.id,
            DesignSession.id != session.id,
        ))
        if bound:
            raise HTTPException(409, '该应用已经绑定另一条编排会话')
        session.application_id = row.id
        session.status = 'generated'
    await sync_design_sessions_for_application(
        db, row.id, sync_definition, manual_changes,
    )
    await db.flush()
    return row, workflow_document(row, {
        **definition,
        'name': name,
        'kind': kind,
    })
