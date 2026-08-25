"""Relational persistence for published application graphs.

The API uses a workflow document at its boundary while PostgreSQL stores every
graph concern in its own relation. JSONB is limited to extensible node options.
"""
from sqlalchemy import delete, select

from .db import (
    Application, ApplicationAgent, ApplicationAgentResource, ApplicationInput,
    ApplicationTask, ApplicationTaskDependency,
)


def _dict(value):
    return dict(value) if isinstance(value, dict) else {}


async def read_application(db, row: Application) -> dict:
    agents = (await db.scalars(select(ApplicationAgent).where(ApplicationAgent.application_id == row.id).order_by(ApplicationAgent.id))).all()
    tasks = (await db.scalars(select(ApplicationTask).where(ApplicationTask.application_id == row.id).order_by(ApplicationTask.id))).all()
    inputs = (await db.scalars(select(ApplicationInput).where(ApplicationInput.application_id == row.id).order_by(ApplicationInput.position, ApplicationInput.id))).all()
    dependencies = (await db.scalars(select(ApplicationTaskDependency).where(ApplicationTaskDependency.application_id == row.id))).all()
    resources = (await db.scalars(select(ApplicationAgentResource).where(ApplicationAgentResource.application_id == row.id))).all()
    app_config = _dict(row.config)

    dependency_map = {}
    for item in dependencies:
        dependency_map.setdefault(item.task_key, []).append(item.depends_on_key)
    resource_map = {}
    for item in resources:
        resource_map.setdefault(item.agent_key, {}).setdefault(item.resource_type, []).append(str(item.resource_id))
    agent_docs = []
    for item in agents:
        config = _dict(item.config)
        goal = item.goal or '完成分配任务'
        role = item.role or '任务专家'
        config.update({
            'id': item.agent_key, 'role': role, 'goal': goal,
            'backstory': item.backstory or f'你是一名{role}，围绕“{goal}”工作，遵循输入约束并交付可验证结果。',
            'memory': item.memory, 'reasoning': item.reasoning,
            'skills': resource_map.get(item.agent_key, {}).get('skill', config.get('skills', [])),
            'plugins': resource_map.get(item.agent_key, {}).get('plugin', config.get('plugins', [])),
            'knowledge_base_ids': resource_map.get(item.agent_key, {}).get('knowledge', config.get('knowledge_base_ids', [])),
            'position': {'x': item.position_x, 'y': item.position_y},
        })
        agent_docs.append(config)
    task_docs = []
    for item in tasks:
        config = _dict(item.config)
        # CrewAI's stdin-based Task(human_input=True) is intentionally not a
        # platform feature.  Keep old persisted documents compatible without
        # exposing or forwarding this unsupported option.
        config.pop('human_input', None)
        for nested in config.get('crew_tasks', []) or []:
            if isinstance(nested, dict):
                nested.pop('human_input', None)
        config.update({
            'id': item.task_key, 'name': item.name, 'description': item.description,
            'expected_output': item.expected_output, 'agent_id': item.agent_key,
            'node_type': item.node_type, 'depends_on': dependency_map.get(item.task_key, []),
            'position': {'x': item.position_x, 'y': item.position_y},
        })
        task_docs.append(config)
    description = str(row.description or app_config.get('description') or '').strip()
    if not description:
        task_subject = next((str(item.name or '').strip() for item in tasks if str(item.name or '').strip()), '用户需求')
        description = f'面向{row.name or task_subject}提供可运行的 CrewAI 智能应用，按已确认输入完成处理并交付可验证结果。'
    return {
        **app_config,
        'id': str(row.id), 'name': row.name, 'description': description,
        'kind': row.kind, 'process': row.process, 'memory': row.memory,
        'planning': row.planning, 'agents': agent_docs, 'tasks': task_docs,
        'memory_policy': app_config.get('memory_policy', {
            'conversation_history': True, 'runtime_checkpoint': True,
            'long_term_semantic': bool(row.memory),
        }),
        'inputs': [{
            'name': x.name, 'label': x.label, 'input_type': x.input_type,
            'required': x.required, 'multiple': x.multiple, 'description': x.description,
        } for x in inputs],
    }


async def read_published_application(db, row: Application) -> dict:
    """Return the last explicitly published definition for runtime callers.

    Older rows predate ``published_config``. They fall back to their current
    normalized graph until the next explicit publish, preserving compatibility
    while new edits are isolated once a snapshot exists.
    """
    snapshot = _dict(getattr(row, 'published_config', {}))
    if not snapshot:
        return await read_application(db, row)
    return {
        **snapshot,
        'id': str(row.id),
        # Keep draft edits out of the live definition.  The snapshot contains
        # the name/kind/description that were explicitly published; the row is
        # allowed to move on while the draft is being edited.
        'name': snapshot.get('name') or row.name,
        'status': 'published',
        'published': True,
        'kind': snapshot.get('kind', row.kind),
    }


async def write_application(db, row: Application, document: dict) -> None:
    row.name = str(document.get('name') or row.name)
    row.kind = document.get('kind', row.kind)
    row.description = document.get('description', '')
    row.process = document.get('process', 'sequential')
    row.memory = bool(document.get('memory', False))
    row.planning = bool(document.get('planning', False))
    excluded = {'id', 'name', 'description', 'kind', 'process', 'memory', 'planning', 'agents', 'tasks', 'inputs', 'status'}
    row.config = {key: value for key, value in document.items() if key not in excluded}
    await db.execute(delete(ApplicationInput).where(ApplicationInput.application_id == row.id))
    await db.execute(delete(ApplicationAgentResource).where(ApplicationAgentResource.application_id == row.id))
    await db.execute(delete(ApplicationTaskDependency).where(ApplicationTaskDependency.application_id == row.id))
    await db.execute(delete(ApplicationTask).where(ApplicationTask.application_id == row.id))
    await db.execute(delete(ApplicationAgent).where(ApplicationAgent.application_id == row.id))

    for position, item in enumerate(document.get('inputs', []) or []):
        db.add(ApplicationInput(application_id=row.id, name=item.get('name', f'input_{position + 1}'),
                                label=item.get('label', item.get('name', '输入')), input_type=item.get('input_type', item.get('type', 'text')),
                                required=bool(item.get('required', False)), multiple=bool(item.get('multiple', False)),
                                description=item.get('description', ''), position=position))
    for position, item in enumerate(document.get('agents', []) or []):
        key = item.get('id', f'agent_{position + 1}')
        pos = item.get('position') or {}
        known = {'id', 'role', 'goal', 'backstory', 'memory', 'reasoning', 'skills', 'plugins', 'knowledge_base_ids', 'position'}
        db.add(ApplicationAgent(application_id=row.id, agent_key=key, role=item.get('role', '任务专家'),
                                goal=item.get('goal', ''), backstory=item.get('backstory', ''), memory=bool(item.get('memory', False)),
                                reasoning=bool(item.get('reasoning', False)), config={k: v for k, v in item.items() if k not in known},
                                position_x=int(pos.get('x', 80)), position_y=int(pos.get('y', 80 + position * 190))))
        for resource_type, values in [('skill', item.get('skills', [])), ('plugin', item.get('plugins', [])),
                                      ('knowledge', item.get('knowledge_base_ids', []))]:
            for resource_id in values or []:
                try:
                    db.add(ApplicationAgentResource(application_id=row.id, agent_key=key, resource_type=resource_type, resource_id=int(resource_id)))
                except (TypeError, ValueError):
                    continue
    for position, item in enumerate(document.get('tasks', []) or []):
        key = item.get('id', f'task_{position + 1}')
        pos = item.get('position') or {}
        known = {'id', 'name', 'description', 'expected_output', 'agent_id', 'node_type', 'depends_on', 'position'}
        db.add(ApplicationTask(application_id=row.id, task_key=key, name=item.get('name', f'执行步骤 {position + 1}'),
                                description=item.get('description', ''), expected_output=item.get('expected_output', ''),
                                agent_key=item.get('agent_id'), node_type=item.get('node_type', 'task'),
                                config={k: v for k, v in item.items() if k not in known}, position_x=int(pos.get('x', 430)),
                                position_y=int(pos.get('y', 80 + position * 210))))
        for dependency in item.get('depends_on', []) or []:
            db.add(ApplicationTaskDependency(application_id=row.id, task_key=key, depends_on_key=dependency))
