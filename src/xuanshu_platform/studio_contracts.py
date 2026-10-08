"""Studio proposal normalization, locking, and executable contract checks."""

from __future__ import annotations

import json
import re

from . import confirmations
from .composer import normalize_runtime_inputs
from .input_contracts import conversational_inputs
from .contracts import architecture_contract_errors, ensure_fixed_output_contracts, executable_contract_errors, force_object_code_contract, generation_code_node_errors, variable_contract_errors, normalize_deterministic_dependencies
from .naming import normalize_definition_names


LEGACY_STUDIO_VARIABLE_ALIASES = {
    'dialogue_message': 'message',
}


def resolve_collection_task(proposal: dict, *, require_interactive: bool = True) -> dict | None:
    """Resolve the task that owns the platform's conversational pause.

    Older Studio documents implicitly used the first task.  Newer documents may
    persist ``interaction.collection_task_id`` so an ordinary Agent task later
    in a top-level graph can own ``ask_user``.  Keeping this resolution in one
    place prevents the validator and workflow materializer from silently
    choosing different nodes.
    """
    tasks = [item for item in (proposal.get('tasks') or []) if isinstance(item, dict)]
    if not tasks:
        configured_id = str((proposal.get('interaction') or {}).get('collection_task_id') or '').strip()
        if configured_id:
            raise RuntimeError(f'交互收集节点不存在：{configured_id}')
        return None
    agents = {
        str(item.get('id')) for item in (proposal.get('agents') or [])
        if isinstance(item, dict) and item.get('id') and item.get('user_interaction')
    }
    interaction = proposal.get('interaction')
    interaction = interaction if isinstance(interaction, dict) else {}
    configured_id = str(interaction.get('collection_task_id') or '').strip()
    def owns_interactive_agent(item: dict) -> bool:
        node_type = str(item.get('node_type') or 'task')
        if node_type in {'task', 'agent'}:
            return str(item.get('agent_id') or '') in agents
        if node_type == 'crew':
            members = {str(value) for value in item.get('crew_agent_ids', []) or []}
            return bool(members & agents)
        return False
    if configured_id:
        selected = next((item for item in tasks if str(item.get('id') or '') == configured_id), None)
        if selected is None:
            raise RuntimeError(f'交互收集节点不存在：{configured_id}')
    else:
        selected = next(
            (item for item in tasks if owns_interactive_agent(item)),
            tasks[0],
        )
    if selected is None:
        selected = next((item for item in tasks if owns_interactive_agent(item)), None)
        if selected is None:
            raise RuntimeError(f'交互收集节点不存在：{configured_id}')
    node_type = str(selected.get('node_type') or 'task')
    selected_agents = (
        {str(selected.get('agent_id') or '')}
        if node_type in {'task', 'agent'}
        else {str(value) for value in selected.get('crew_agent_ids', []) or []}
        if node_type == 'crew' else set()
    )
    if node_type not in {'task', 'agent', 'crew'}:
        raise RuntimeError('多轮应用的信息收集节点必须是 Agent 或 Crew 节点')
    if require_interactive and not (selected_agents & agents):
        raise RuntimeError('多轮应用的信息收集节点必须绑定启用了 ask_user 的 Agent')
    return selected


def resolve_interactive_task_ids(proposal: dict) -> list[str]:
    """Return all ordinary tasks that may call ``ask_user``.

    ``collection_task_id`` was a single-node UI concept. Keep it as a
    backwards-compatible alias when reading old documents. The executable
    contract now records every node that may request input; an individual run
    still pauses at the first request it reaches and resumes from that checkpoint.
    """
    tasks = [item for item in (proposal.get('tasks') or []) if isinstance(item, dict)]
    agents = {
        str(item.get('id')) for item in (proposal.get('agents') or [])
        if isinstance(item, dict) and item.get('id') and item.get('user_interaction')
    }
    interaction = proposal.get('interaction')
    interaction = interaction if isinstance(interaction, dict) else {}
    configured = interaction.get('interactive_task_ids')
    if configured is None:
        legacy = str(interaction.get('collection_task_id') or '').strip()
        configured = [legacy] if legacy else []
    if not isinstance(configured, list):
        raise RuntimeError('interactive_task_ids 必须是数组')
    result = []
    for raw_id in configured:
        task_id = str(raw_id or '').strip()
        if not task_id or task_id in result:
            continue
        task = next((item for item in tasks if str(item.get('id') or '') == task_id), None)
        if task is None:
            raise RuntimeError(f'交互节点不存在：{task_id}')
        node_type = str(task.get('node_type') or 'task')
        owners = ({str(task.get('agent_id') or '')} if node_type in {'task', 'agent'}
                  else {str(value) for value in task.get('crew_agent_ids', []) or []} if node_type == 'crew'
                  else set())
        if not owners & agents:
            raise RuntimeError(f'交互节点配置无效：{task_id}')
        result.append(task_id)
    if not result:
        result = [
            str(item.get('id')) for item in tasks
            if (
                str(item.get('agent_id') or '') in agents
                or bool({str(value) for value in item.get('crew_agent_ids', []) or []} & agents)
            )
        ]
    return result


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
    file_inputs = [item for item in normalized if item.get('type') in {'file', 'image'}]
    if file_inputs:
        normalized = [item for item in normalized if item.get('type') not in {'file', 'image'}]
        normalized.append({
            'name': '本轮文件',
            'variable': 'files',
            'type': 'file',
            'required': any(bool(item.get('required')) for item in file_inputs),
            'multiple': True,
            'description': '用户每轮对话上传的文件列表，后续轮次追加，不覆盖已有文件。',
        })
    if interaction_mode == 'multi_turn':
        normalized = conversational_inputs(normalized)
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
    return [message_input, *additional]


def _file_input_aliases(values: list[dict] | None) -> set[str]:
    """Return historical names that represented an uploaded-file input."""
    return {
        str(item.get('name') or item.get('variable') or '').strip()
        for item in (values or [])
        if isinstance(item, dict)
        and item.get('input_type', item.get('type')) in {'file', 'image'}
        and str(item.get('name') or item.get('variable') or '').strip()
        and str(item.get('name') or item.get('variable') or '').strip() != 'files'
    }


def _rewrite_file_references(definition: dict, aliases: set[str]) -> None:
    """Rewrite file-input aliases in every executable nested node."""
    aliases = {str(alias).strip() for alias in aliases if str(alias).strip()}
    if not aliases:
        return

    def rewrite(node: dict) -> None:
        for field in ('description', 'objective', 'expected_output', 'code_snippet'):
            value = node.get(field)
            if isinstance(value, str):
                for alias in aliases:
                    value = value.replace('{' + alias + '}', '{files}')
                node[field] = value
        bindings = node.get('input_bindings') or {}
        if isinstance(bindings, dict):
            for binding in bindings.values():
                if (isinstance(binding, dict) and binding.get('source') == 'input'
                        and str(binding.get('variable') or '') in aliases):
                    binding['variable'] = 'files'
        for rule in node.get('router_rules') or []:
            expression = rule.get('expression') if isinstance(rule, dict) else None
            if isinstance(expression, dict) and expression.get('source') == 'input':
                if str(expression.get('variable') or '') in aliases:
                    expression['variable'] = 'files'
        for nested in node.get('crew_tasks') or []:
            if isinstance(nested, dict):
                rewrite(nested)

    for task in definition.get('tasks', []) or []:
        if isinstance(task, dict):
            rewrite(task)

    def rewrite_metadata(value):
        if isinstance(value, str):
            for alias in aliases:
                value = value.replace('{' + alias + '}', '{files}')
                if value == alias:
                    value = 'files'
            return value
        if isinstance(value, list):
            return [rewrite_metadata(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite_metadata(item) for key, item in value.items()}
        return value

    for key in ('interaction', 'stage_summaries', 'draft_sync'):
        if key in definition:
            definition[key] = rewrite_metadata(definition[key])


def _infer_file_aliases(definition: dict) -> set[str]:
    """Recover file aliases from drafts normalized before their task text."""
    if not any(
        str(item.get('name') or item.get('variable') or '') == 'files'
        for item in (definition.get('inputs') or []) if isinstance(item, dict)
    ):
        return set()
    aliases = set()
    pattern = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
    file_hint = re.compile(r'(?:file|files|attachment|document|reference|material|template|upload|excel|csv|pdf)', re.I)

    def inspect(node: dict) -> None:
        for field in ('description', 'objective', 'expected_output', 'code_snippet'):
            value = node.get(field)
            if isinstance(value, str):
                aliases.update(name for name in pattern.findall(value) if file_hint.search(name))
        for nested in node.get('crew_tasks') or []:
            if isinstance(nested, dict):
                inspect(nested)

    for task in definition.get('tasks', []) or []:
        if isinstance(task, dict):
            inspect(task)
    return aliases


def normalize_legacy_studio_references(definition: dict, *, normalize_names: bool = True) -> dict:
    """Migrate known historical prompt aliases before strict validation."""
    # Shared Crew was an early Studio-only switch. It has no runtime contract
    # and must not be re-enabled by old drafts or direct API payloads.
    definition.pop('share_crew', None)
    definition.pop('max_rpm', None)
    # Flow has one executable process mode. Normalize any stale persisted
    # value without exposing legacy process names to the editor or model.
    if str(definition.get('kind') or '') == 'flow':
        if definition.get('process') not in {None, 'sequential'}:
            definition['process'] = 'sequential'
        if definition.get('recommended_process') not in {None, 'sequential'}:
            definition['recommended_process'] = 'sequential'
    inputs = list(definition.get('inputs') or [])
    old_names = _file_input_aliases(inputs)
    named_file_inputs = [
        item for item in inputs
        if isinstance(item, dict)
        and re.search(
            r'(?:file|files|attachment|reference|material|template|upload|excel|csv|pdf)',
            str(item.get('name') or item.get('variable') or ''), re.I,
        )
        and str(item.get('name') or item.get('variable') or '') != 'files'
    ]
    old_names.update(
        str(item.get('name') or item.get('variable') or '').strip()
        for item in named_file_inputs
        if str(item.get('name') or item.get('variable') or '').strip()
    )
    file_inputs = [item for item in inputs if isinstance(item, dict) and item.get('input_type', item.get('type')) in {'file', 'image'}]
    file_inputs.extend(item for item in named_file_inputs if item not in file_inputs)
    if file_inputs:
        definition['inputs'] = [item for item in inputs if item not in file_inputs]
        if not any(str(item.get('name') or item.get('variable') or '') == 'files' for item in definition['inputs']):
            definition['inputs'].append({
                'name': 'files', 'label': '本轮文件', 'input_type': 'file',
                'required': any(bool(item.get('required')) for item in file_inputs),
                'multiple': True,
                'description': '用户每轮对话上传的文件列表，后续轮次追加，不覆盖已有文件。',
            })
    _rewrite_file_references(definition, old_names | _infer_file_aliases(definition))
    is_flow = str(definition.get('kind') or '') == 'flow'
    for task in definition.get('tasks', []) or []:
        if not isinstance(task, dict):
            continue
        task.pop('crew_share_crew', None)
        task.pop('crew_max_rpm', None)
        for nested in task.get('crew_tasks', []) or []:
            if isinstance(nested, dict):
                nested.pop('crew_share_crew', None)
        # In a Flow, Agent ownership is represented by an Agent→node edge.
        # Crew membership stays in ``crew_agent_ids`` and is likewise derived
        # from Agent→Crew edges; never retain a hidden/default ``agent_id`` on
        # deterministic or Crew nodes.
        if is_flow and str(task.get('node_type') or '') in {'router', 'code', 'tool', 'crew'}:
            task['agent_id'] = None
        if str(task.get('node_type') or '') in {'code', 'tool'}:
            # Deterministic nodes execute their explicit contract directly;
            # they are not CrewAI Tasks and therefore do not need an LLM
            # description or expected-output prompt.
            task['description'] = ''
            task['expected_output'] = ''
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
    if normalize_names:
        normalize_definition_names(definition)
    normalize_crew_execution_contract(definition)
    normalize_flow_crew_execution_contract(definition)
    for task in definition.get('tasks', []) or []:
        if not isinstance(task, dict):
            continue
        if str(task.get('node_type') or '') in {'code', 'tool'}:
            task['description'] = ''
            task['expected_output'] = ''
    ensure_fixed_output_contracts(definition)
    _repair_interaction_task_ids(definition, definition)
    return normalize_deterministic_dependencies(definition)


def normalize_studio_definition(definition: dict, *, normalize_names: bool = True) -> dict:
    """Normalize a document that is already in the current storage format.

    Legacy aliases and removed fields are handled only by the bootstrap
    migration above. Runtime/API code uses this narrower function so old
    shapes cannot silently re-enter the application contract.
    """
    if not isinstance(definition, dict):
        return definition
    if normalize_names:
        normalize_definition_names(definition)
    normalize_crew_execution_contract(definition)
    normalize_flow_crew_execution_contract(definition)
    for task in definition.get('tasks', []) or []:
        if isinstance(task, dict) and str(task.get('node_type') or '') in {'code', 'tool'}:
            task['description'] = ''
            task['expected_output'] = ''
    _repair_interaction_task_ids(definition, definition)
    ensure_fixed_output_contracts(definition)
    return normalize_deterministic_dependencies(definition)


def ensure_message_task_reference(definition: dict) -> dict:
    """Make the platform message input an actual dependency of the workflow."""
    tasks = definition.get('tasks', []) or []
    if not tasks or any(
        '{message}' in str(task.get('description') or task.get('objective') or '')
        for task in tasks
    ):
        return definition
    interaction = definition.get('interaction')
    interaction = interaction if isinstance(interaction, dict) else {}
    selected_id = str(interaction.get('collection_task_id') or '').strip()
    first = next((task for task in tasks if str(task.get('id') or '') == selected_id), None)
    if first is None or str(first.get('node_type') or 'task') not in {'task', 'agent'}:
        first = next(
            (task for task in tasks
             if str(task.get('node_type') or 'task') in {'task', 'agent'}
             and task.get('agent_id')),
            None,
        )
    # A Flow beginning with code/tool must bind ``message`` through its
    # deterministic input contract; it must not receive a synthetic LLM
    # description that is never executed.
    if first is None:
        return definition
    key = 'description' if 'description' in first or 'objective' not in first else 'objective'
    first[key] = '根据用户本轮需求 {message} 完成以下工作：\n' + str(first.get(key) or '')
    return definition


def normalize_crew_execution_contract(definition: dict) -> dict:
    """Make Crew process semantics consistent across cards and workflows."""
    if not isinstance(definition, dict) or definition.get('kind') != 'crew':
        return definition
    agents = [item for item in definition.get('agents', []) or [] if isinstance(item, dict)]
    tasks = [item for item in definition.get('tasks', []) or [] if isinstance(item, dict)]
    process = str(definition.get('process') or definition.get('recommended_process') or 'sequential')
    process = process if process in {'sequential', 'hierarchical'} else 'sequential'
    definition['process'] = process
    definition['recommended_process'] = process
    agent_ids = [str(item.get('id')) for item in agents if item.get('id')]

    def add_manager(prefix: str, *, role: str = 'Crew 管理者') -> str:
        base = re.sub(r'[^A-Za-z0-9_]+', '_', prefix).strip('_') or 'crew'
        manager_id = f'{base}_manager'
        used = set(agent_ids)
        suffix = 2
        while manager_id in used:
            manager_id = f'{base}_manager_{suffix}'
            suffix += 1
        agents.append({
            'id': manager_id, 'role': role,
            'goal': '统筹 Crew 内部任务，分配工作并汇总最终结果。',
            'backstory': '你是独立的 Crew 管理者，只负责任务分派、上下文协调和结果验收，不直接执行任何业务任务。',
            'responsibilities': ['理解整体目标', '动态分配内部任务', '汇总并验收执行结果'],
            'skills': [], 'plugins': [], 'tools': [], 'knowledge_base_ids': [],
            'allow_delegation': True, 'user_interaction': False,
        })
        agent_ids.append(manager_id)
        return manager_id

    definition['agents'] = agents
    definition['tasks'] = tasks
    if process == 'hierarchical':
        # Only explicit markers select the manager; role names are not a
        # contract.  A model may also refer to a manager ID it forgot to
        # include, so a dangling reference is never persisted.
        explicit_manager_id = str(
            definition.get('manager_agent_id')
            or next((item.get('id') for item in agents if item.get('allow_delegation')), '')
        )
        assigned_ids = {
            str(task.get('agent_id')) for task in tasks if task.get('agent_id')
        }
        # An Agent left without work is a plausible dedicated manager only
        # when the other Agents are visibly assigned. Once every assignment
        # has been cleared, "unassigned" would just mean "listed first" and
        # would strip a real worker's tools.
        unassigned_manager = next(
            (agent_id for agent_id in agent_ids if agent_id not in assigned_ids), ''
        ) if assigned_ids else ''
        manager_id = (
            explicit_manager_id
            if explicit_manager_id in agent_ids and explicit_manager_id not in assigned_ids
            else unassigned_manager or (add_manager('crew') if agent_ids else '')
        )
        if manager_id:
            definition['manager_agent_id'] = manager_id
        for agent in agents:
            is_manager = str(agent.get('id') or '') == manager_id
            agent['allow_delegation'] = is_manager
            if is_manager:
                # CrewAI's hierarchical manager is the coordinator, not a
                # worker. It must not carry worker Skills, Tools, or knowledge
                # bindings that could be invoked as delegated capabilities.
                for key in ('skills', 'plugins', 'tools', 'knowledge_base_ids'):
                    if key in agent:
                        agent[key] = []
        for task in tasks:
            if str(task.get('node_type') or 'task') in {'task', 'agent'}:
                task['agent_id'] = None
    else:
        definition['manager_agent_id'] = None
        for agent in agents:
            agent['allow_delegation'] = False
        for index, task in enumerate(tasks):
            if str(task.get('node_type') or 'task') in {'task', 'agent'}:
                task['agent_id'] = task.get('agent_id') or (
                    agent_ids[min(index, len(agent_ids) - 1)] if agent_ids else None
                )
    return definition


def normalize_flow_crew_execution_contract(definition: dict) -> dict:
    """Normalize embedded hierarchical Crews with a dedicated manager Agent."""
    if not isinstance(definition, dict) or definition.get('kind') != 'flow':
        return definition
    agents = [item for item in definition.get('agents', []) or [] if isinstance(item, dict)]
    tasks = [item for item in definition.get('tasks', []) or [] if isinstance(item, dict)]
    agent_ids = {str(item.get('id')) for item in agents if item.get('id')}

    def add_manager(task_id: str) -> str:
        base = re.sub(r'[^A-Za-z0-9_]+', '_', str(task_id or 'crew')).strip('_') or 'crew'
        manager_id = f'{base}_manager'
        suffix = 2
        while manager_id in agent_ids:
            manager_id = f'{base}_manager_{suffix}'
            suffix += 1
        agents.append({
            'id': manager_id, 'role': 'Crew 管理者',
            'goal': '统筹 Crew 内部任务，动态分配工作并汇总最终结果。',
            'backstory': '你是独立的 Crew 管理者，只负责任务分派、上下文协调和结果验收，不直接执行任何业务任务。',
            'responsibilities': ['理解整体目标', '动态分配内部任务', '汇总并验收执行结果'],
            'skills': [], 'plugins': [], 'tools': [], 'knowledge_base_ids': [],
            'allow_delegation': True, 'user_interaction': False,
        })
        agent_ids.add(manager_id)
        return manager_id

    for node in tasks:
        if str(node.get('node_type') or '') != 'crew':
            continue
        if node.get('crew_process') == 'sequential':
            manager_id = str(node.get('crew_manager_agent_id') or '')
            members = [str(value) for value in node.get('crew_agent_ids', []) or []
                       if str(value) in agent_ids and str(value) != manager_id]
            node['crew_manager_agent_id'] = None
            node['crew_agent_ids'] = list(dict.fromkeys(members))
            for index, nested in enumerate(node.get('crew_tasks', []) or []):
                if isinstance(nested, dict) and not nested.get('agent_id') and members:
                    nested['agent_id'] = members[index % len(members)]
            continue
        if node.get('crew_process') != 'hierarchical':
            continue
        members = [str(value) for value in node.get('crew_agent_ids', []) or [] if str(value) in agent_ids]
        nested = [item for item in node.get('crew_tasks', []) or [] if isinstance(item, dict)]
        assigned = {str(item.get('agent_id')) for item in nested if item.get('agent_id')}
        requested = str(node.get('crew_manager_agent_id') or '')
        role_manager = next(
            (str(agent.get('id')) for agent in agents
             if str(agent.get('id')) in members
             and ('管理' in str(agent.get('role') or '')
                  or 'manager' in str(agent.get('role') or '').casefold())),
            '',
        )
        manager_id = (
            requested if requested in members and requested not in assigned
            else role_manager if role_manager and role_manager not in assigned else ''
        )
        if not manager_id:
            manager_id = next((member for member in members if member not in assigned), '')
        if not manager_id:
            manager_id = add_manager(str(node.get('id') or 'crew'))
        if manager_id not in members:
            members.append(manager_id)
        node['crew_agent_ids'] = list(dict.fromkeys(members))
        node['crew_manager_agent_id'] = manager_id
        for item in nested:
            item['agent_id'] = None
        for agent in agents:
            if str(agent.get('id')) == manager_id:
                agent['allow_delegation'] = True
                for key in ('skills', 'plugins', 'tools', 'knowledge_base_ids'):
                    if key in agent:
                        agent[key] = []
            elif str(agent.get('id')) in members:
                agent['allow_delegation'] = False
    definition['agents'] = agents
    definition['tasks'] = tasks
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
            # Resource and capability choices belong to the confirmed
            # discovery/architecture contract. A generation response often
            # omits them or returns empty lists; those omissions must not
            # detach a Skill/Tool or disable an execution capability.
            for key in ('skills', 'plugins', 'knowledge_base_ids', 'tools'):
                if confirmed.get(key) and not generated.get(key):
                    merged[key] = _copy(confirmed[key])
            # The confirmed card is the latest decision for both switches.
            if 'allow_code_execution' in confirmed:
                merged['allow_code_execution'] = bool(confirmed['allow_code_execution'])
            # The confirmed card is the latest decision for this switch.
            # Keep it both ways instead of only ever re-enabling it.
            if 'user_interaction' in confirmed:
                merged['user_interaction'] = bool(confirmed['user_interaction'])
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
                'crew_memory', 'crew_planning', 'crew_cache',
                'crew_output_log_file', 'crew_manager_agent_id',
                'crew_manager_model_profile_id',
                'crew_planning_model_profile_id', 'crew_verbose',
                'tool_id',
            ):
                if key in confirmed:
                    merged[key] = _copy(confirmed[key])
            # Code and bindings are implementation details owned by
            # generation, so a repaired main()/input_bindings must win.  Keep
            # the confirmed value only when generation left the field empty.
            for key in ('code_snippet', 'input_bindings', 'execution_contract'):
                if key in confirmed and not generated.get(key):
                    merged[key] = _copy(confirmed[key])
            merged_tasks.append(merged)
        result['tasks'] = merged_tasks
    return result


def _repair_interaction_task_ids(result: dict, current: dict | None = None) -> None:
    """Migrate stale multi-turn task IDs after a model regenerates a graph."""
    interaction = result.get('interaction')
    if not isinstance(interaction, dict):
        return
    if str(result.get('interaction_mode') or interaction.get('mode') or '') != 'multi_turn':
        interaction.pop('interactive_task_ids', None)
        interaction.pop('collection_task_id', None)
        return
    tasks = [item for item in result.get('tasks', []) or [] if isinstance(item, dict)]
    task_ids = [str(item.get('id') or '') for item in tasks if str(item.get('id') or '')]
    if not task_ids:
        return
    source = current if isinstance(current, dict) else result
    old_interaction = source.get('interaction') if isinstance(source.get('interaction'), dict) else {}
    configured = old_interaction.get('interactive_task_ids')
    if configured is None:
        legacy = str(old_interaction.get('collection_task_id') or '').strip()
        configured = [legacy] if legacy else []
    configured = [str(value).strip() for value in configured or [] if str(value).strip()]
    if not configured:
        configured = [str(value).strip() for value in interaction.get('interactive_task_ids') or [] if str(value).strip()]
    if not configured:
        return
    valid = [value for value in configured if value in task_ids]
    stale = [value for value in configured if value not in task_ids]
    # Old model IDs normally follow the same task order as the regenerated
    # graph. Map stale entries by position, then fall back to the first
    # language task. This keeps ask_user attached to the intended Agent.
    for stale_index, old_id in enumerate(stale):
        old_tasks = [item for item in (source.get('tasks') or []) if isinstance(item, dict)]
        old_index = next((index for index, item in enumerate(old_tasks)
                          if str(item.get('id') or '') == old_id), None)
        candidate_index = old_index if old_index is not None else stale_index
        candidate = task_ids[candidate_index] if candidate_index < len(task_ids) else None
        if candidate and candidate not in valid:
            valid.append(candidate)
    if not valid:
        valid = [str(item.get('id')) for item in tasks
                 if str(item.get('node_type') or 'task') in {'task', 'agent', 'crew'}][:1]
    if not valid:
        interaction.pop('interactive_task_ids', None)
        interaction.pop('collection_task_id', None)
        return
    interactive_agents = {
        str(item.get('id')) for item in result.get('agents', []) or []
        if isinstance(item, dict) and item.get('user_interaction')
    }
    task_by_id = {str(item.get('id')): item for item in tasks}
    valid = [
        task_id for task_id in valid
        if (
            str(task_by_id[task_id].get('node_type') or 'task') in {'task', 'agent'}
            and str(task_by_id[task_id].get('agent_id') or '') in interactive_agents
        ) or (
            str(task_by_id[task_id].get('node_type') or '') == 'crew'
            and bool(set(str(value) for value in task_by_id[task_id].get('crew_agent_ids', []) or []) & interactive_agents)
        )
    ]
    if not valid:
        valid = [
            task_id for task_id, task in task_by_id.items()
            if str(task.get('node_type') or 'task') in {'task', 'agent'}
            and str(task.get('agent_id') or '') in interactive_agents
        ]
    if not valid:
        interaction.pop('interactive_task_ids', None)
        interaction.pop('collection_task_id', None)
        return
    interaction['interactive_task_ids'] = valid
    interaction['collection_task_id'] = valid[0]


def preserve_confirmed_proposal(reviewed: dict, current: dict,
                                *, generation_confirmed: bool = False) -> dict:
    """Prevent a later stage from rewriting choices the user already confirmed."""
    result = _copy(reviewed or {})
    # The original request is a session-level invariant. Stage-local schemas
    # do not expose it, so an empty generation/input response must never erase
    # the summary that later revision routing relies on.
    if current.get('original_request') and not result.get('original_request'):
        result['original_request'] = _copy(current['original_request'])
    if current.get('stage_summaries') and not result.get('stage_summaries'):
        result['stage_summaries'] = _copy(current['stage_summaries'])
    # Capture aliases before the confirmed input card is normalized to the
    # canonical ``files`` field. The graph merge below may restore task text
    # from an older draft after that normalization.
    confirmed_file_aliases = _file_input_aliases(current.get('inputs') or [])
    locked = set(current.get('confirmed_stages', []))
    current_interaction = current.get('interaction')
    if current.get('interaction_mode') == 'multi_turn' and isinstance(current_interaction, dict):
        # Stage-local Composer schemas intentionally do not expose the full
        # interaction envelope. Preserve a user-selected later collector while
        # merging a newly generated Agent/Task graph.
        result_interaction = result.get('interaction')
        result_interaction = (
            _copy(result_interaction) if isinstance(result_interaction, dict) else {}
        )
        interactive_ids = current_interaction.get('interactive_task_ids')
        if isinstance(interactive_ids, list):
            result_interaction['interactive_task_ids'] = [
                str(item).strip() for item in interactive_ids if str(item).strip()
            ]
        legacy_id = str(current_interaction.get('collection_task_id') or '').strip()
        if legacy_id:
            result_interaction['collection_task_id'] = legacy_id
        result['interaction'] = result_interaction
    if confirmations.interaction_confirmed(current):
        locked_mode = confirmations.locked_interaction_mode(current)
        if locked_mode:
            result['interaction_mode'] = locked_mode
            result['interaction_mode_preselected'] = True
            result['inputs'] = normalize_studio_input_contract(
                result.get('inputs', current.get('inputs', [])), locked_mode,
            )
    if 'inputs' in locked:
        result['inputs'] = normalize_studio_input_contract(
            current.get('inputs', []), current.get('interaction_mode'),
        )
    if 'architecture' in locked or str(current.get('stage') or '') == 'generation':
        for key in (
            'kind', 'recommended_kind', 'process', 'recommended_process',
            'interaction_mode',
        ):
            if key in current:
                result[key] = _copy(current[key])
        result = _merge_confirmed_graph(result, current)
        # Resource selection is confirmed before generation. Preserve its
        # selected IDs even when the generation-stage schema defaults the
        # field to an empty list.
        if current.get('capability_requirements') and not result.get('capability_requirements'):
            result['capability_requirements'] = _copy(current['capability_requirements'])
        if current.get('tools'):
            result['tools'] = list(dict.fromkeys([
                *(str(value) for value in current.get('tools', []) or []),
                *(str(value) for value in result.get('tools', []) or []),
            ]))
    if generation_confirmed:
        for key in ('tools', 'capability_requirements'):
            if key in current:
                result[key] = _copy(current[key])
    _repair_interaction_task_ids(result, current)
    _rewrite_file_references(result, confirmed_file_aliases)
    return result


def ensure_executable_design(proposal: dict, stage: str) -> None:
    """Reject a successful-looking plan that contains no runnable graph."""
    if stage not in {'architecture', 'generation'} or proposal.get('intent') != 'design':
        return
    agents = proposal.get('agents', []) or []
    tasks = proposal.get('tasks', []) or []
    if not tasks:
        raise RuntimeError('生成方案未包含任何 Task，无法形成可运行编排')
    requires_agent = any(
        str(item.get('node_type') or 'task') in {'task', 'agent', 'crew'}
        for item in tasks if isinstance(item, dict)
    )
    if not agents and (proposal.get('kind') != 'flow' or requires_agent):
        raise RuntimeError('生成方案未包含任何 Agent，无法形成可运行编排')
    agent_ids = {str(item.get('id')) for item in agents if item.get('id')}
    if proposal.get('interaction_mode') == 'multi_turn':
        interactive_ids = {
            str(item.get('id')) for item in agents
            if item.get('id') and item.get('user_interaction')
        }
        if not interactive_ids:
            raise RuntimeError('生成的多轮方案必须有一个显式启用 ask_user 的信息收集 Agent')
        try:
            resolve_collection_task(proposal)
        except RuntimeError as exc:
            raise RuntimeError(f'生成的多轮方案交互节点无效：{exc}') from exc
    hierarchical_crew = (
        proposal.get('kind') == 'crew'
        and str(proposal.get('process') or proposal.get('recommended_process') or '') == 'hierarchical'
    )
    for task in tasks:
        node_type = str(task.get('node_type') or 'task')
        if (node_type in {'task', 'agent'} and not hierarchical_crew
                and str(task.get('agent_id') or '') not in agent_ids):
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
    normalize_studio_definition(proposal)
    ensure_message_task_reference(proposal)
    # Generation produces the graph that will be saved and run, so it must
    # also pass the code/tool/router binding gate used by publish and run.
    if stage == 'generation':
        force_object_code_contract(proposal)
        errors = list(dict.fromkeys([
            *executable_contract_errors(proposal),
            *generation_code_node_errors(proposal),
        ]))
    else:
        errors = architecture_contract_errors(proposal)
    if errors:
        raise RuntimeError(f'{stage} 阶段变量契约无效：' + '；'.join(errors))
