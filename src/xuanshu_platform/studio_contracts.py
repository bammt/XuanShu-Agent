"""Studio proposal normalization, locking, and executable contract checks."""

from __future__ import annotations

import json

from .composer import normalize_runtime_inputs
from .contracts import ensure_file_output_contract, variable_contract_errors


LEGACY_STUDIO_VARIABLE_ALIASES = {
    'dialogue_message': 'message',
}


def canonical_studio_variable_name(value) -> str | None:
    raw = str(value or '').strip()
    return LEGACY_STUDIO_VARIABLE_ALIASES.get(raw, raw) or None


def normalize_studio_input_contract(values: list[dict] | None,
                                    interaction_mode: str | None = None) -> list[dict]:
    """Normalize inputs and enforce the platform's primary message contract."""
    raw_values = []
    message_seen = False
    for item in values or []:
        variable = canonical_studio_variable_name(item.get('name') or item.get('variable'))
        if variable == 'message':
            if message_seen:
                continue
            message_seen = True
        raw_values.append({
            'name': item.get('label') or item.get('name') or '输入',
            'variable': variable,
            'type': item.get('input_type') or item.get('type') or 'text',
            'required': item.get('required', False),
            'multiple': item.get('multiple', False),
            'description': item.get('description', ''),
        })
    normalized = normalize_runtime_inputs(raw_values)
    contract = [
        {
            'name': item['variable'],
            'label': item['name'],
            'input_type': item['type'],
            'required': item['required'],
            'multiple': item['multiple'],
            'description': item['description'],
        }
        for item in normalized
    ]
    existing_message = next((item for item in contract if item['name'] == 'message'), None)
    message_input = {
        'name': 'message',
        'label': (existing_message or {}).get('label') or '用户需求',
        'input_type': 'long_text',
        'required': True,
        'multiple': False,
        'description': ((existing_message or {}).get('description')
                        or '用户本轮对智能体的需求描述，通过 {message} 传入执行流程。'),
    }
    additional = [item for item in contract if item['name'] != 'message']
    if interaction_mode == 'multi_turn':
        additional = [item for item in additional if item['input_type'] in {'file', 'image'}]
    return [message_input, *additional]


def normalize_legacy_studio_references(definition: dict) -> dict:
    """Migrate known historical prompt aliases before strict validation."""
    for task in definition.get('tasks', []) or []:
        if not isinstance(task, dict):
            continue
        for node in [task, *(task.get('crew_tasks', []) or [])]:
            if not isinstance(node, dict):
                continue
            for field in ('description', 'objective', 'expected_output', 'code_snippet'):
                value = node.get(field)
                if not isinstance(value, str):
                    continue
                for old, new in LEGACY_STUDIO_VARIABLE_ALIASES.items():
                    value = value.replace('{' + old + '}', '{' + new + '}')
                node[field] = value
    return ensure_file_output_contract(definition)


def ensure_message_task_reference(definition: dict) -> dict:
    """Make the platform message input an actual dependency of the workflow."""
    tasks = definition.get('tasks', []) or []
    if not tasks or any(
        '{message}' in str(task.get('description') or task.get('objective') or '')
        for task in tasks
    ):
        return definition
    first = tasks[0]
    key = 'description' if 'description' in first or 'objective' not in first else 'objective'
    first[key] = '根据用户本轮需求 {message} 完成以下工作：\n' + str(first.get(key) or '')
    return definition


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _merge_confirmed_graph(reviewed: dict, current: dict) -> dict:
    """Keep confirmed graph identity while accepting generation details and repairs."""
    result = _copy(reviewed)

    current_agents = current.get('agents') or []
    reviewed_agents = {
        str(item.get('id')): item for item in result.get('agents') or [] if item.get('id')
    }
    if current_agents:
        merged_agents = []
        for confirmed in current_agents:
            generated = reviewed_agents.get(str(confirmed.get('id')))
            if not generated:
                merged_agents.append(_copy(confirmed))
                continue
            merged = {**_copy(confirmed), **_copy(generated)}
            for key in ('id', 'role', 'purpose', 'goal', 'backstory', 'responsibilities'):
                if key in confirmed:
                    merged[key] = _copy(confirmed[key])
            merged_agents.append(merged)
        result['agents'] = merged_agents

    current_tasks = current.get('tasks') or []
    reviewed_tasks = {
        str(item.get('id')): item for item in result.get('tasks') or [] if item.get('id')
    }
    if current_tasks:
        merged_tasks = []
        for confirmed in current_tasks:
            generated = reviewed_tasks.get(str(confirmed.get('id')))
            if not generated:
                merged_tasks.append(_copy(confirmed))
                continue
            merged = {**_copy(confirmed), **_copy(generated)}
            # The card confirms graph identity and topology. Generation still
            # owns detailed prompts and variable contracts so review can repair
            # those fields without silently changing the selected architecture.
            for key in (
                'id', 'name', 'agent_id', 'agent_role', 'depends_on',
                'node_type', 'crew_agent_ids', 'crew_process',
            ):
                if key in confirmed:
                    merged[key] = _copy(confirmed[key])
            merged_tasks.append(merged)
        result['tasks'] = merged_tasks
    return result


def preserve_confirmed_proposal(reviewed: dict, current: dict,
                                *, generation_confirmed: bool = False) -> dict:
    """Prevent a later stage from rewriting choices the user already confirmed."""
    result = _copy(reviewed or {})
    locked = set(current.get('confirmed_stages', []))
    resolved = current.get('resolved_clarifications', {}) or {}
    if current.get('interaction_mode_preselected') or resolved.get('interaction_mode'):
        locked_mode = resolved.get('interaction_mode')
        if locked_mode not in {'single_run', 'multi_turn'}:
            locked_mode = current.get('interaction_mode')
        if locked_mode in {'single_run', 'multi_turn'}:
            result['interaction_mode'] = locked_mode
            result['interaction_mode_preselected'] = True
            result['inputs'] = normalize_studio_input_contract(
                result.get('inputs', current.get('inputs', [])), locked_mode,
            )
    if 'inputs' in locked:
        result['inputs'] = normalize_studio_input_contract(
            current.get('inputs', []), current.get('interaction_mode'),
        )
    if 'architecture' in locked:
        for key in (
            'kind', 'recommended_kind', 'process', 'recommended_process',
            'interaction_mode',
        ):
            if key in current:
                result[key] = _copy(current[key])
        result = _merge_confirmed_graph(result, current)
    if generation_confirmed:
        for key in ('tools', 'capability_requirements'):
            if key in current:
                result[key] = _copy(current[key])
    return result


def ensure_executable_design(proposal: dict, stage: str) -> None:
    """Reject a successful-looking plan that contains no runnable graph."""
    if stage not in {'architecture', 'generation'} or proposal.get('intent') != 'design':
        return
    agents = proposal.get('agents', []) or []
    tasks = proposal.get('tasks', []) or []
    if not tasks:
        raise RuntimeError('生成方案未包含任何 Task，无法形成可运行编排')
    if not agents:
        raise RuntimeError('生成方案未包含任何 Agent，无法形成可运行编排')
    agent_ids = {str(item.get('id')) for item in agents if item.get('id')}
    if proposal.get('interaction_mode') == 'multi_turn':
        interactive_ids = {
            str(item.get('id')) for item in agents
            if item.get('id') and item.get('user_interaction')
        }
        first = tasks[0]
        if not interactive_ids:
            raise RuntimeError('生成的多轮方案必须有一个显式启用 ask_user 的信息收集 Agent')
        if str(first.get('node_type') or 'task') == 'crew':
            raise RuntimeError('生成的多轮方案首节点必须是普通 Agent，不能是 Crew 节点')
        if str(first.get('agent_id') or '') not in interactive_ids:
            raise RuntimeError('生成的多轮方案首节点必须绑定显式启用 ask_user 的 Agent')
    for task in tasks:
        node_type = str(task.get('node_type') or 'task')
        if node_type in {'task', 'agent'} and str(task.get('agent_id') or '') not in agent_ids:
            raise RuntimeError(
                f'生成方案的 Task“{task.get("name") or task.get("id") or "未命名"}”未绑定有效 Agent'
            )
        if node_type == 'crew':
            members = {str(value) for value in task.get('crew_agent_ids', []) or []}
            if not members or not members <= agent_ids:
                raise RuntimeError(
                    f'生成方案的 Crew 节点“{task.get("name") or task.get("id") or "未命名"}”未绑定有效 Agent'
                )


def ensure_stage_variable_contract(proposal: dict, stage: str) -> None:
    """Validate stage variables before showing or publishing the graph."""
    if stage not in {'architecture', 'generation'} or proposal.get('intent') != 'design':
        return
    normalize_legacy_studio_references(proposal)
    ensure_message_task_reference(proposal)
    errors = variable_contract_errors(proposal)
    if errors:
        raise RuntimeError(f'{stage} 阶段变量契约无效：' + '；'.join(errors))
