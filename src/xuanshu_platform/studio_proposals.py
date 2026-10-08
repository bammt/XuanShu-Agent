"""Studio proposal, card and workflow shaping.

Pure functions only: no database, Redis, object storage or model calls.
Moved out of ``api.py`` so that module keeps only HTTP entry points.
"""
import json
import re
from .capability_policy import apply_interaction_policy
from .contracts import summary_looks_like_change_note
from . import (confirmations)
from .capability_policy import (apply_capability_policy)
from .composer import (normalize_runtime_inputs)
from .contracts import (execution_graph)
from .conversation import (budget_chat_messages)
from .db import (DesignSession)
from .naming import (normalize_definition_names)
from .studio_contracts import (
    ensure_message_task_reference,
    normalize_crew_execution_contract,
    normalize_flow_crew_execution_contract,
    normalize_legacy_studio_references,
    normalize_studio_definition,
    normalize_studio_input_contract,
)
from datetime import (UTC, datetime)
from fastapi import (HTTPException)
from pydantic import (BaseModel, field_validator)


class StudioChatIn(BaseModel):
    message: str
    orchestration_id: str
    kind: str = 'auto'
    model_profile_id: str | None = None
    history: list[dict] = []
    attachment_ids: list[str] = []
    current_workflow: dict | None = None
    confirmed: bool = False
    confirmation_stage: str | None = None
    clarification_id: str = ''
    clarification_value: str = ''
    input_contract: list[dict] = []
    removed_input_names: list[str] = []
    proposal: dict | None = None
    kind_preselected: bool = False
    action: str = 'message'
    architecture_changed: bool = False
    previous_kind: str = ''
    # Compact canvas edits are carried alongside the authoritative workflow
    # document so a later Composer turn can distinguish confirmed manual
    # changes from the older conversational proposal.
    manual_changes: list[dict] = []

    @field_validator('orchestration_id', mode='before')
    @classmethod
    def normalize_orchestration_id(cls, value):
        # Application ids are numeric in PostgreSQL, while unbound Studio
        # sessions use opaque string ids. Keep one transport type and accept
        # requests from browser tabs loaded before that frontend fix.
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value


def obvious_conversation(message: str) -> str:
    normalized = re.sub(r'[\s!！?？。,.，]+', '', message).lower()
    replies = {
        '你好': '你好！有什么我可以帮你的？', '您好': '您好！有什么我可以帮您的？',
        '嗨': '嗨！有什么我可以帮你的？', 'hello': 'Hello！有什么我可以帮你的？',
        'hi': 'Hi！有什么我可以帮你的？', 'nihao': '你好！有什么我可以帮你的？',
        '在吗': '在的，有什么我可以帮你的？',
        '谢谢': '不客气。', '感谢': '不客气。',
    }
    return replies.get(normalized, '')


def _retry_original_request(proposal: dict, history: list[dict] | None) -> str:
    """Recover the original design request for a failed Studio retry."""
    proposal = proposal or {}
    for key in ('original_request', 'request_summary', 'summary', 'request'):
        value = str(proposal.get(key) or '').strip()
        if value and value not in _STUDIO_RETRY_MESSAGES:
            return value
    ignored = _STUDIO_RETRY_MESSAGES | {
        '确认运行输入。', '确认运行输入', '确认编排架构。', '确认编排架构',
        '确认能力配置。', '确认能力配置', '确认生成清单。', '确认生成清单',
    }
    for item in history or []:
        if not isinstance(item, dict) or item.get('role') != 'user':
            continue
        value = str(item.get('content') or '').strip()
        if value and value.casefold() not in {str(x).casefold() for x in ignored}:
            return value
    return ''


def studio_proposal(raw: dict, stage: str, confirmed: list[str] | None = None,
                    kind_preselected: bool | None = None) -> dict:
    interaction_mode = raw.get('interaction_mode', 'single_run')
    if interaction_mode not in {'single_run', 'multi_turn'}:
        interaction_mode = 'single_run'
    inputs = normalize_studio_input_contract([
        {
            'name': item.get('variable', item.get('name', 'input')),
            'label': item.get('name', item.get('label', '输入')),
            'input_type': item.get('type', item.get('input_type', 'text')),
            'required': item.get('required', False),
            'multiple': item.get('multiple', False),
            'description': item.get('description', ''),
        }
        for item in normalize_runtime_inputs(raw.get('inputs', []))
    ], interaction_mode)
    agents = [{'id': x.get('id'), 'role': x.get('role', '任务专家'), 'purpose': x.get('goal', x.get('purpose', '')),
               'goal': x.get('goal', x.get('purpose', '')), 'backstory': x.get('backstory') or (
                   f"你是一名{x.get('role', '专业执行智能体')}，围绕“{x.get('goal', x.get('purpose', '完成分配任务'))}”工作，"
                   '遵循输入约束并交付可验证结果。'),
               'responsibilities': x.get('responsibilities') or [x.get('goal', x.get('purpose', '完成分配任务'))], 'tools': x.get('tools', []),
               'skills': x.get('skills', []), 'plugins': x.get('plugins', []), 'knowledge_base_ids': x.get('knowledge_base_ids', []),
               'memory': x.get('memory', False), 'reasoning': x.get('reasoning', False),
               'allow_code_execution': x.get('allow_code_execution', False),
               'user_interaction': x.get('user_interaction', False)}
              for x in raw.get('agents', [])]
    tasks = [{'id': x.get('id'), 'name': x.get('name', '执行任务'), 'objective': x.get('description', x.get('objective', '')),
              'agent_id': x.get('agent_id'),
              'agent_role': next((a.get('role', '') for a in raw.get('agents', []) if a.get('id') == x.get('agent_id')), ''),
              'depends_on': x.get('depends_on', []), 'expected_output': x.get('expected_output', ''),
              'output_mode': x.get('output_mode', 'text'),
              'code_snippet': x.get('code_snippet', ''),
              'input_bindings': x.get('input_bindings', {}),
              'execution_contract': x.get('execution_contract', 'legacy'),
              'tool_id': x.get('tool_id'),
              'node_type': x.get('node_type', 'task'),
              'router_rules': x.get('router_rules', []),
              'routes': x.get('routes', {}),
              'condition': x.get('condition', ''),
              'run_if': x.get('run_if', ''),
              'crew_agent_ids': x.get('crew_agent_ids', []),
              'crew_tasks': x.get('crew_tasks', []),
              'crew_process': x.get('crew_process', 'sequential'),
              'crew_memory': x.get('crew_memory', False),
              'crew_planning': x.get('crew_planning', False),
              'crew_cache': x.get('crew_cache', True),
              'crew_output_log_file': x.get('crew_output_log_file', ''),
              'crew_manager_agent_id': x.get('crew_manager_agent_id'),
              'crew_manager_model_profile_id': x.get('crew_manager_model_profile_id'),
              'crew_planning_model_profile_id': x.get('crew_planning_model_profile_id'),
              'crew_verbose': bool(x.get('crew_verbose', False)),
              'output_variables': x.get('output_variables', []),
              'dependency_variables': x.get('dependency_variables', {}),
              'human_feedback': x.get('human_feedback', False),
              'feedback_message': x.get('feedback_message', '请审核当前结果'),
              'feedback_outcomes': x.get('feedback_outcomes') or ['approved', 'revise'],
              'feedback_default_outcome': x.get('feedback_default_outcome')} for x in raw.get('tasks', [])]
    kind = raw.get('kind', 'crew')
    process = raw.get('process', 'sequential')
    if stage == 'architecture':
        # The architecture card confirms nodes and dependencies only.  Code
        # and input_bindings are implementation details written by the
        # generation stage; keeping a draft here would lock a wrong version.
        for task in tasks:
            if str(task.get('node_type') or '') in {'code', 'tool'}:
                task['code_snippet'] = ''
                task['input_bindings'] = {}
    apply_interaction_policy({
        'interaction_mode': interaction_mode, 'kind': kind, 'process': process,
        'agents': agents, 'tasks': tasks,
    })
    state_fields = [item['name'] for item in inputs]
    required_fields = [item['name'] for item in inputs if item.get('required')]
    interaction = {
        'mode': interaction_mode,
        'state_schema': {
            'status': {'type': 'string'},
            'collected_fields': {'type': 'object', 'fields': state_fields},
            'missing_fields': {'type': 'array', 'items': required_fields},
        },
        'states': (
            ['collecting', 'awaiting_confirmation', 'ready', 'running', 'completed']
            if interaction_mode == 'multi_turn'
            else ['ready', 'running', 'completed']
        ),
        'transitions': (
            [
                {'from': 'collecting', 'to': 'awaiting_confirmation', 'when': '需求档案完整'},
                {'from': 'awaiting_confirmation', 'to': 'ready', 'when': '用户明确确认执行'},
                {'from': 'ready', 'to': 'running', 'when': 'execution starts'},
                {'from': 'running', 'to': 'completed', 'when': 'delivery succeeds'},
            ]
            if interaction_mode == 'multi_turn'
            else [
                {'from': 'ready', 'to': 'running', 'when': 'execution starts'},
                {'from': 'running', 'to': 'completed', 'when': 'delivery succeeds'},
            ]
        ),
    }
    if interaction_mode == 'multi_turn' and tasks:
        # The current contract permits several interactive tasks. Keep the
        # legacy collection_task_id alias in the response only for old clients;
        # execution uses interactive_task_ids and never restricts to one node.
        raw_interaction = raw.get('interaction')
        raw_interaction = raw_interaction if isinstance(raw_interaction, dict) else {}
        configured_id = str(raw_interaction.get('collection_task_id') or '').strip()
        interactive_ids = {
            str(agent.get('id')) for agent in agents
            if agent.get('id') and agent.get('user_interaction')
        }
        interactive_tasks = [
            task for task in tasks
            if (
                str(task.get('node_type') or 'task') in {'task', 'agent'}
                and str(task.get('agent_id') or '') in interactive_ids
            ) or (
                str(task.get('node_type') or 'task') == 'crew'
                and bool({str(value) for value in task.get('crew_agent_ids', []) or []} & interactive_ids)
            )
        ]
        # The generation model may omit the already-confirmed ask_user flag.
        # Preserve the stage proposal and let preserve_confirmed_proposal map
        # the old task IDs onto the regenerated graph. Final executable
        # validation still rejects a multi-turn graph with no interactive node.
        interaction['interactive_task_ids'] = [str(task.get('id')) for task in interactive_tasks]
        # Legacy clients render one collector. Keep the first interactive task
        # as a read-only compatibility alias while the canonical contract is
        # the complete list above.
        if configured_id:
            interaction['collection_task_id'] = configured_id
        elif interaction['interactive_task_ids']:
            interaction['collection_task_id'] = interaction['interactive_task_ids'][0]
    summary = str(raw.get('summary') or '').strip()
    if summary_looks_like_change_note(summary):
        summary = ''
    if not summary:
        title = str(raw.get('title') or '').strip()
        task_names = [str(item.get('name') or '').strip() for item in tasks if item.get('name')]
        subject = title or (task_names[0] if task_names else '用户需求')
        summary = f'面向{subject}提供可运行的 CrewAI 智能应用，按已确认输入完成处理并交付可验证结果。'
    result = {
        **raw, 'summary': summary, 'inputs': inputs, 'recommended_kind': kind,
        'original_request': str(
            raw.get('original_request') or raw.get('request_summary') or raw.get('request') or ''
        ).strip(),
        'recommended_process': 'sequential' if kind == 'flow' else process,
        'process_reason': ('任务存在状态、分支或审批，适合使用 Flow 节点表达。' if kind == 'flow'
                           else ('需要管理者动态委派复杂任务。' if process == 'hierarchical' else '任务依赖明确，建议按顺序传递上下文。')),
        'architecture_reason': raw.get('summary', ''), 'agents': agents, 'tasks': tasks,
        'tools': raw.get('tools', []), 'planning': raw.get('planning', False),
        'capability_requirements': raw.get('capability_requirements', []),
        'interaction_mode': interaction_mode, 'interaction': interaction,
        'stage': stage, 'confirmed_stages': confirmed or [],
        'kind_confirmed': 'architecture' in (confirmed or []),
        'kind_preselected': bool(raw.get('kind_preselected') or kind_preselected),
        'confirmation_prompt': ({'discovery': '请先确认运行交互方式、资源配置和编排类型。',
                                 'inputs': '请确认发布后用户需要提供的输入。',
                                 'architecture': '请确认编排形式、子智能体分工和任务关系。',
                                 'generation': '正在生成并更新可运行画布。'}[stage]),
        'clarification': raw.get('clarification'), 'resolved_clarifications': raw.get('resolved_clarifications', {}),
        'capability_card': bool(raw.get('capability_card')),
        'resource_selection_confirmed': bool(raw.get('resource_selection_confirmed')),
    }
    if not result.get('original_request'):
        result.pop('original_request', None)
    normalize_crew_execution_contract(result)
    normalize_flow_crew_execution_contract(result)
    normalize_definition_names(result)
    normalize_flow_crew_execution_contract(result)
    return result


def stage_after_input_confirmation(kind_preselected: bool, locked_kind: str | None) -> str:
    """Input approval always hands off to a separately reviewable architecture stage.

    A preselected Crew/Flow only removes the type question from discovery. It
    must not remove the architecture card or cause generation to run before
    the user has reviewed the Agent/Task graph.
    """
    return 'architecture'


def has_meaningful_studio_proposal(proposal: dict | None) -> bool:
    """Ignore normalization-only fields when deciding whether discovery already started."""
    proposal = proposal or {}
    scalar_markers = (
        'request', 'clarification', 'capability_card', 'preflight',
        'resource_selection_confirmed', 'interaction_mode_preselected',
        'kind_preselected', 'kind_confirmed', 'orchestration_intent_confirmed',
        'application_purpose_known',
    )
    collection_markers = (
        'resolved_clarifications', 'confirmed_stages', 'inputs', 'agents', 'tasks',
    )
    return any(bool(proposal.get(key)) for key in scalar_markers + collection_markers)


def is_conversation_only_proposal(proposal: dict | None) -> bool:
    """Return whether this session has chat history but no design state yet."""
    return bool(isinstance(proposal, dict) and proposal.get('intent') == 'conversation')


_LEGACY_ORCHESTRATION_REQUEST = re.compile(
    r'(?:做|创建|生成|编排|设计|需要|想要|修改).*(?:智能体|智能应用|应用|助手)'
    r'|(?:智能体|智能应用|应用|助手).*(?:做|创建|生成|编排|设计|修改)',
    re.IGNORECASE,
)


_LEGACY_GENERIC_ORCHESTRATION = re.compile(
    r'^(?:(?:请|麻烦)(?:你)?)?(?:(?:能不能|可以))?'
    r'(?:(?:帮我|给我|我想|我要|想要))?'
    r'(?:做|创建|生成|编排|设计)(?:一个|个|一下)?'
    r'(?:智能体|智能应用|智能体应用|应用|助手)(?:吧|吗|呢|啊|呀|哈)?$',
    re.IGNORECASE,
)


def _legacy_generic_orchestration_request(message: str) -> bool:
    normalized = re.sub(r'[\s!！?？。,.，]+', '', str(message or ''))
    return bool(_LEGACY_GENERIC_ORCHESTRATION.fullmatch(normalized))


def is_legacy_conversation_only_session(row: DesignSession | None) -> bool:
    """Hide cards created by older builds before conversation intent was persisted.

    New turns use the explicit ``intent=conversation`` marker. This conservative
    compatibility path inspects every user turn, rather than assuming the first
    message was a greeting.
    """
    if (row is None or not row.proposal
            or is_conversation_only_proposal(row.proposal) or row.application_id
            or row.proposal.get('orchestration_intent_confirmed')
            or row.proposal.get('application_purpose_known')):
        return False
    user_messages = [
        str(item.get('content') or '').strip()
        for item in (row.messages or [])
        if isinstance(item, dict) and item.get('role') == 'user'
    ]
    if not user_messages:
        return False
    requests = [
        (index, message) for index, message in enumerate(user_messages)
        if _LEGACY_ORCHESTRATION_REQUEST.search(message)
    ]
    if not has_meaningful_studio_proposal(row.proposal):
        return False
    if not requests:
        return True
    request_index, request_message = requests[-1]
    if not _legacy_generic_orchestration_request(request_message):
        return False
    later_context = user_messages[request_index + 1:]
    return not any(
        message and not message.startswith('确认') and not obvious_conversation(message)
        for message in later_context
    )


def mark_studio_conversation_only(row: DesignSession) -> None:
    """Persist chat mode so refresh cannot revive a transient proposal card."""
    row.proposal = {'intent': 'conversation'}
    row.stage = 'discovery'
    row.status = 'draft'


def studio_composer_history(history: list[dict] | None, message: str,
                            initial_request: str = '') -> list[dict]:
    """Return prior conversation turns without duplicating the current user message."""
    result = [
        {'role': str(item.get('role') or 'user'), 'content': str(item.get('content') or '')}
        for item in (history or [])
        if isinstance(item, dict) and str(item.get('content') or '').strip()
    ]
    if (result and result[-1].get('role') == 'user'
            and result[-1].get('content') == message):
        result.pop()
    if not result and initial_request and initial_request != message:
        result.append({'role': 'user', 'content': initial_request})
    return result


def studio_discovery_history(history: list[dict] | None, message: str) -> list[dict]:
    """Carry bounded chat context only while conversation is entering design."""
    prior = studio_composer_history(history, message)
    # Server-owned Studio history is already budgeted and may start with a
    # compact summary. Re-budgeting it as chat turns would drop that summary.
    if any(item.get('role') == 'system' for item in prior):
        return prior
    kept, _summary, _tokens = budget_chat_messages(prior, token_budget=1800)
    return kept


def compact_stage_history(proposal: dict | None, message: str) -> list[dict]:
    """Keep only the current turn for stage Agents.

    Earlier turns remain available to the Studio transcript, but the Composer
    receives confirmed structured stage summaries through ``existing``. This
    prevents every stage from paying for the full chat transcript again.
    """
    text = str(message or '').strip()
    if not text:
        return []
    # A retry/correction message is intentionally short, but architecture
    # retries must carry the user's last explicit structural constraint. The
    # confirmed graph is also sent separately in ComposerState; this compact
    # context prevents a bare “重试” from being interpreted as permission to
    # redesign Agent/Crew membership.
    return [{'role': 'user', 'content': text}]


def collect_architecture_constraints(history: list[dict] | None) -> list[str]:
    """Retain explicit topology requests as structured-stage context."""
    structural_terms = (
        'agent', '智能体', 'crew', 'flow', '成员', '几个', '多少个', '只需要',
        '放在', '放到', '合并', '拆分', '独立', '节点',
    )
    ignored = {'确认编排架构。', '确认编排架构', '重试', '重试一下', '再试一次', '继续'}
    result = []
    for item in history or []:
        if not isinstance(item, dict) or item.get('role') != 'user':
            continue
        text_value = str(item.get('content') or '').strip()
        if not text_value or text_value in ignored:
            continue
        if any(term in text_value.casefold() for term in structural_terms):
            if text_value not in result:
                result.append(text_value)
    return result[-12:]


_STUDIO_RETRY_MESSAGES = {
    '重试', '重新试', '再试', '再试一次', '重试一下', '再重试一次',
    '继续重试', 'retry', 'try again',
}


def is_studio_retry_message(message: str) -> bool:
    return re.sub(r'[\s。.!！?？]+', '', str(message or '')).casefold() in {
        re.sub(r'[\s。.!！?？]+', '', value).casefold()
        for value in _STUDIO_RETRY_MESSAGES
    }


def architecture_was_confirmed_in_history(history: list[dict] | None) -> bool:
    """Find an explicit architecture confirmation after the latest card."""
    latest_architecture_card = -1
    latest_architecture_confirmation = -1
    for index, item in enumerate(history or []):
        if not isinstance(item, dict):
            continue
        proposal = item.get('proposal')
        if item.get('role') == 'assistant' and isinstance(proposal, dict):
            if proposal.get('stage') == 'architecture':
                latest_architecture_card = index
        if item.get('role') == 'user' and str(item.get('content') or '').strip() in {
            '确认编排架构。', '确认编排架构',
        }:
            latest_architecture_confirmation = index
    return latest_architecture_confirmation > latest_architecture_card >= 0


def latest_confirmed_crew_contract(history: list[dict] | None) -> dict:
    """Recover the last persisted Crew mode when an input edit reopened design."""
    for item in reversed(history or []):
        if not isinstance(item, dict) or item.get('role') != 'assistant':
            continue
        proposal = item.get('proposal')
        if not isinstance(proposal, dict) or proposal.get('kind') != 'crew':
            continue
        process = proposal.get('process') or proposal.get('recommended_process')
        if process not in {'sequential', 'hierarchical'}:
            continue
        return {
            'kind': 'crew',
            'recommended_kind': 'crew',
            'process': process,
            'recommended_process': process,
            'manager_agent_id': proposal.get('manager_agent_id'),
        }
    return {}


def apply_requested_crew_process(proposal: dict, process: str | None,
                                 target_node_id: str | None = None) -> dict:
    """Apply a routed Crew mode to the intended top-level or Flow Crew node."""
    if process not in {'sequential', 'hierarchical'} or not isinstance(proposal, dict):
        return proposal
    if proposal.get('kind') == 'crew':
        proposal['process'] = process
        proposal['recommended_process'] = process
        return proposal
    if proposal.get('kind') != 'flow':
        return proposal
    crew_nodes = [
        task for task in proposal.get('tasks', []) or []
        if isinstance(task, dict) and task.get('node_type') == 'crew'
    ]
    target = next((task for task in crew_nodes
                   if str(task.get('id') or '') == str(target_node_id or '')), None)
    if target is None and len(crew_nodes) == 1:
        target = crew_nodes[0]
    if target is None:
        return proposal
    target['crew_process'] = process
    if process == 'hierarchical':
        target['requested_process'] = process
        return proposal
    manager_id = str(target.get('crew_manager_agent_id') or '')
    target['crew_manager_agent_id'] = None
    members = [str(value) for value in target.get('crew_agent_ids', []) or []
               if str(value) and str(value) != manager_id]
    target['crew_agent_ids'] = list(dict.fromkeys(members))
    for index, nested in enumerate(target.get('crew_tasks', []) or []):
        if not isinstance(nested, dict):
            continue
        nested['agent_id'] = members[index % len(members)] if members else None
    return proposal


def recover_confirmed_architecture_after_constraint(history: list[dict] | None) -> dict | None:
    """Recover the confirmed graph after the latest topology request, if any."""
    messages = [item for item in (history or []) if isinstance(item, dict)]
    topology_terms = (
        '只需要', '只要', '三个', '两个', '一个', 'agent', '智能体', 'crew',
        '成员', '放在', '放到', '合并', '拆分', '独立', '顶层',
    )
    latest_constraint = max((index for index, item in enumerate(messages)
                             if item.get('role') == 'user'
                             and any(term in str(item.get('content') or '').casefold()
                                     for term in topology_terms)), default=-1)
    candidate = None
    for offset, item in enumerate(messages[latest_constraint + 1:], start=latest_constraint + 1):
        proposal = item.get('proposal') if item.get('role') == 'assistant' else None
        if isinstance(proposal, dict) and proposal.get('stage') == 'architecture' and proposal.get('tasks'):
            candidate = json.loads(json.dumps(proposal, ensure_ascii=False))
            continue
        if item.get('role') == 'user' and str(item.get('content') or '').strip() in {
            '确认编排架构。', '确认编排架构',
        } and candidate:
            # The first architecture card after the explicit structure request
            # is the one the user confirmed. Later retry cards may be malformed
            # and must not replace that decision.
            return json.loads(json.dumps(candidate, ensure_ascii=False))
    return None


def stage_summary(proposal: dict | None, stage: str) -> dict:
    """Create the immutable, minimal handoff produced by a completed stage."""
    proposal = proposal or {}
    if stage == 'discovery':
        return {
            'stage': stage,
            'original_request': proposal.get('original_request') or proposal.get('request_summary') or proposal.get('request', ''),
            'interaction_mode': proposal.get('interaction_mode'),
            'interaction_mode_confirmed': confirmations.interaction_confirmed(proposal),
            'resource_selection_confirmed': confirmations.resources_confirmed(proposal),
            'kind': proposal.get('recommended_kind') or proposal.get('kind'),
            'kind_confirmed': confirmations.kind_confirmed(proposal),
            'capability_requirements': proposal.get('capability_requirements', []),
        }
    if stage == 'inputs':
        return {
            'stage': stage,
            'original_request': proposal.get('original_request') or proposal.get('request', ''),
            'interaction_mode': proposal.get('interaction_mode'),
            'inputs': proposal.get('inputs', []),
        }
    if stage == 'architecture':
        agents = [
            {
                key: item.get(key)
                for key in (
                    'id', 'role', 'goal', 'backstory', 'responsibilities', 'skills',
                    'plugins', 'knowledge_base_ids', 'tools', 'user_interaction',
                )
                if item.get(key) not in (None, '', [], {})
            }
            for item in proposal.get('agents', []) or []
            if isinstance(item, dict)
        ]
        tasks = [
            {
                key: item.get(key)
                for key in (
                    'id', 'name', 'description', 'expected_output',
                    'agent_id', 'depends_on', 'node_type', 'crew_agent_ids',
                    'crew_tasks', 'crew_process', 'output_variables',
                    'dependency_variables', 'crew_memory', 'crew_planning',
                    'crew_cache', 'crew_output_log_file',
                    'crew_manager_agent_id', 'crew_manager_model_profile_id',
                    'crew_planning_model_profile_id', 'crew_verbose',
                    'execution_contract', 'tool_id',
                    'router_rules', 'routes', 'condition', 'run_if', 'output_mode',
                )
                if item.get(key) not in (None, '', [], {})
            }
            for item in proposal.get('tasks', []) or []
            if isinstance(item, dict)
        ]
        return {
            'stage': stage,
            'original_request': proposal.get('original_request') or proposal.get('request', ''),
            'kind': proposal.get('recommended_kind') or proposal.get('kind'),
            'process': proposal.get('recommended_process') or proposal.get('process', 'sequential'),
            'summary': proposal.get('summary', ''),
            'agents': agents,
            'tasks': tasks,
        }
    return {
        'stage': stage,
        'original_request': proposal.get('original_request') or proposal.get('request', ''),
        'summary': proposal.get('summary', ''),
        'kind': proposal.get('recommended_kind') or proposal.get('kind'),
        'process': proposal.get('recommended_process') or proposal.get('process', 'sequential'),
        'interaction_mode': proposal.get('interaction_mode'),
        'resource_selection_confirmed': confirmations.resources_confirmed(proposal),
        'capability_requirements': proposal.get('capability_requirements', []),
        'inputs': proposal.get('inputs', []),
        'agents': [
            {
                key: item.get(key)
                for key in ('id', 'role', 'goal', 'backstory', 'responsibilities', 'skills',
                            'plugins', 'knowledge_base_ids', 'tools', 'user_interaction')
                if item.get(key) not in (None, '', [], {})
            }
            for item in proposal.get('agents', []) or []
            if isinstance(item, dict)
        ],
        'tasks': [
            {
                key: item.get(key)
                for key in ('id', 'name', 'description', 'expected_output',
                            'agent_id', 'depends_on', 'node_type', 'crew_agent_ids',
                            'crew_tasks', 'crew_process', 'output_variables',
                            'dependency_variables', 'crew_memory', 'crew_planning',
                            'crew_cache', 'crew_output_log_file',
                            'crew_manager_agent_id', 'crew_manager_model_profile_id',
                            'crew_planning_model_profile_id', 'crew_verbose',
                            'code_snippet', 'input_bindings', 'execution_contract', 'tool_id',
                            'router_rules', 'routes', 'condition', 'run_if', 'output_mode')
                if item.get(key) not in (None, '', [], {})
            }
            for item in proposal.get('tasks', []) or []
            if isinstance(item, dict)
        ],
        'tools': list(proposal.get('tools', []) or []),
    }


def bound_session_proposal(proposal: dict, proposal_data: dict, current: dict) -> dict:
    """Persist the card of a session that already owns an application draft.

    The stage summary is the canonical, compact view of the current card, but
    a summary alone is not a complete proposal: the architecture summary, for
    example, has no ``inputs``.  The next confirmation reads this persisted
    card back as the authoritative proposal, so dropping the contract fields
    here made every post-generation architecture revision fail with
    "variable not available".  Keep the full card and let the summary win only
    on the fields it owns.
    """
    stage = str(proposal.get('stage') or proposal_data.get('stage') or 'generation')
    confirmed = list(dict.fromkeys([
        *(current.get('confirmed_stages') or []),
        *(proposal_data.get('confirmed_stages') or []),
    ]))
    if stage == 'generation':
        confirmed = list(dict.fromkeys([*confirmed, 'inputs', 'architecture']))
    elif stage in _STAGE_ORDER:
        # A card still waiting for confirmation must not be stored as
        # confirmed: the merge would then restore nodes the user deleted.
        confirmed = [item for item in confirmed
                     if item in _STAGE_ORDER and _STAGE_ORDER.index(item) < _STAGE_ORDER.index(stage)]

    # Keep the full proposal and merge stage-specific fields from stage_summary
    # without losing other important context (stage_summaries, etc.)
    base = json.loads(json.dumps(proposal, ensure_ascii=False))
    summary_fields = stage_summary(proposal, stage)

    # Preserve stage_summaries from base if not in summary
    if 'stage_summaries' in base and 'stage_summaries' not in summary_fields:
        summary_fields['stage_summaries'] = base['stage_summaries']

    return {
        **base,
        **summary_fields,
        'draft_sync': proposal_data.get('draft_sync') or current.get('draft_sync', {}),
        'structure_confirmed': True,
        'confirmed_stages': confirmed,
    }


def remember_stage_summary(proposal: dict | None, stage: str) -> dict:
    result = json.loads(json.dumps(proposal or {}, ensure_ascii=False))
    summaries = dict(result.get('stage_summaries') or {})
    summaries[stage] = stage_summary(result, stage)
    result['stage_summaries'] = summaries
    return result


_STAGE_ORDER = ('discovery', 'inputs', 'architecture', 'generation')


def rewind_proposal(proposal: dict | None, target_stage: str, message: str = '',
                    reset_fields: list[str] | None = None) -> dict:
    """Reopen one owning stage while keeping earlier confirmed decisions fixed."""
    result = json.loads(json.dumps(proposal or {}, ensure_ascii=False))
    if target_stage not in _STAGE_ORDER:
        return result
    target_index = _STAGE_ORDER.index(target_stage)
    result['stage'] = target_stage
    result['confirmed_stages'] = [
        item for item in result.get('confirmed_stages', [])
        if item in _STAGE_ORDER and _STAGE_ORDER.index(item) < target_index
    ]
    summaries = dict(result.get('stage_summaries') or {})
    for item in _STAGE_ORDER[target_index:]:
        summaries.pop(item, None)
    result['stage_summaries'] = summaries
    if target_stage == 'discovery':
        # The router names the preflight choices to ask again.  Keyword
        # matching on the raw message is not used: "工作流程" would miss
        # ``orchestration_kind`` and "保留工具" would wrongly reset resources.
        # Returning to discovery without naming any choice re-asks all three,
        # because an unchanged discovery would advance straight back out.
        confirmations.reset_preflight(result, reset_fields)
        resolved = result['resolved_clarifications']
        result.update({
            'capability_card': False,
            'capability_blocked': [],
            'preflight': True,
            'clarification': None,
            'resolved_clarifications': resolved,
            'agents': [], 'tasks': [],
        })
    elif target_stage == 'inputs':
        result.update({
            'capability_card': False,
            'capability_blocked': [],
            'clarification': None,
            'agents': [], 'tasks': [],
        })
    elif target_stage == 'architecture':
        result.update({
            'capability_card': False,
            'capability_blocked': [],
            'clarification': None,
        })
    return result


REVISION_LOG_LIMIT = 20


def append_revision_log(log: list | None, stage: str, text: str) -> list[dict]:
    """Append one user revision; the newest entry wins on conflict."""
    entries = [dict(item) for item in (log or []) if isinstance(item, dict) and item.get('text')]
    text = str(text or '').strip()
    if text and not (entries and entries[-1].get('text') == text and entries[-1].get('stage') == stage):
        entries.append({'stage': stage, 'text': text[:500]})
    return entries[-REVISION_LOG_LIMIT:]


def record_failed_turn(proposal: dict | None, error: str) -> dict:
    """Remember why the last turn failed and drop its one-turn requests.

    ``requested_process`` belongs to the turn that asked for it.  Leaving it on
    the proposal after a failure re-applies the same impossible change on every
    later turn.  The failure reason goes into ``revision_log`` so the router and
    stage Agents see what not to repeat; no extra model call is made.
    """
    result = dict(proposal or {})
    result.pop('requested_process', None)
    result.pop('requested_crew_node_id', None)
    result['revision_log'] = append_revision_log(
        result.get('revision_log'), 'failed', f'上一轮失败：{error}',
    )
    return result


def ready_reply(workflow: dict, proposal: dict | None = None, updated: bool = False) -> str:
    """Workflow-ready message shared by every generation branch."""
    name = workflow.get('name') or '可运行编排'
    return f"已生成{'并更新' if updated else ''}{name}，请在右侧画布检查后运行。"


def discovery_preflight_complete(proposal: dict | None) -> bool:
    """Advance only after all three discovery decisions are explicitly confirmed."""
    return confirmations.preflight_complete(proposal)


def canonicalize_studio_proposal(proposal: dict | None) -> dict:
    """Return a transport-safe copy of a persisted studio proposal."""
    result = json.loads(json.dumps(proposal or {}, ensure_ascii=False))
    if result.get('recommended_kind') in {'crew', 'flow'} and result.get('kind') not in {'crew', 'flow'}:
        result['kind'] = result['recommended_kind']
    # Session proposals may be submitted by an older Studio tab during the
    # one-time migration window. Canonicalize that transport payload before
    # writing it back; application/runtime documents use the current-only
    # normalizer instead.
    normalize_legacy_studio_references(result)
    locked_mode = confirmations.locked_interaction_mode(result)
    if locked_mode and result.get('interaction_mode_preselected'):
        result['interaction_mode'] = locked_mode
    if isinstance(result.get('inputs'), list):
        result['inputs'] = normalize_studio_input_contract(
            result['inputs'], result.get('interaction_mode'),
        )
    return result


def lock_confirmed_stage_messages(messages: list[dict], confirmed_stages: list[str] | None) -> list[dict]:
    """Keep earlier confirmation cards locked after the next stage is saved."""
    confirmed = {str(stage) for stage in (confirmed_stages or [])}
    if not confirmed:
        return messages
    locked_messages = []
    for message in messages:
        proposal = message.get('proposal') if isinstance(message, dict) else None
        stage = proposal.get('stage') if isinstance(proposal, dict) else None
        if stage not in confirmed:
            locked_messages.append(message)
            continue
        locked_proposal = json.loads(json.dumps(proposal, ensure_ascii=False))
        locked_proposal['confirmed_stages'] = list(dict.fromkeys([
            *(locked_proposal.get('confirmed_stages') or []), stage,
        ]))
        if stage == 'architecture':
            locked_proposal['kind_confirmed'] = True
        locked_messages.append({**message, 'proposal': locked_proposal})
    return locked_messages


DRAFT_SYNC_FIELDS = (
    'name', 'description', 'kind', 'process', 'planning', 'memory', 'cache',
    'output_log_file',
    'interaction_mode', 'interaction', 'inputs', 'agents', 'tasks', 'tools',
    'capability_requirements', 'manager_agent_id', 'structure_confirmed',
)


def normalize_manual_changes(changes: list[dict] | None) -> list[dict]:
    """Keep only bounded, JSON-safe canvas change summaries."""
    normalized = []
    for item in (changes or [])[-50:]:
        if not isinstance(item, dict):
            continue
        fields = []
        for field in item.get('fields', []) or []:
            if not isinstance(field, dict) or not field.get('name'):
                continue
            fields.append({
                'name': str(field.get('name'))[:80],
                'before': json.loads(json.dumps(field.get('before'), ensure_ascii=False)) if 'before' in field else None,
                'after': json.loads(json.dumps(field.get('after'), ensure_ascii=False)) if 'after' in field else None,
            })
        if not fields:
            continue
        normalized.append({
            'source': str(item.get('source') or 'canvas')[:30],
            'at': str(item.get('at') or datetime.now(UTC).replace(tzinfo=None).isoformat())[:80],
            'fields': fields,
        })
    return normalized


def draft_sync_document(definition: dict, manual_changes: list[dict] | None = None) -> dict:
    """Build the small authoritative-draft projection stored in both layers."""
    workflow = {
        key: json.loads(json.dumps(definition[key], ensure_ascii=False))
        for key in DRAFT_SYNC_FIELDS if key in definition
    }
    return {
        'source': 'canvas',
        'updated_at': datetime.now(UTC).replace(tzinfo=None).isoformat(),
        'manual_changes': normalize_manual_changes(manual_changes),
        'workflow': workflow,
    }


def input_contract_needs_model_completion(proposal: dict, request: str) -> bool:
    """Identify a substantive single-run contract with no required business field."""
    if proposal.get('interaction_mode') != 'single_run':
        return False
    inputs = proposal.get('inputs', []) or []
    # A single-run design can be under-specified even when the model emitted
    # only the platform field or only optional fields. Give the same
    # input-design Agent one completeness pass; it must preserve confirmed
    # details and add only independent values needed for the final deliverable.
    additional_required = [
        item for item in inputs
        if item.get('name') != 'message' and item.get('required', False)
    ]
    return not additional_required and len(str(request or '').strip()) >= 12


def _json_merge_patch(document: dict, patch: dict) -> dict:
    result = json.loads(json.dumps(document or {}, ensure_ascii=False))
    for key, value in (patch or {}).items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _json_merge_patch(result[key], value)
        else:
            result[key] = json.loads(json.dumps(value, ensure_ascii=False))
    return result


def apply_studio_structured_patch(current: dict, incoming: dict | None, body: StudioChatIn) -> tuple[dict, bool]:
    """Apply card actions without sending editable structured data through an LLM."""
    result = json.loads(json.dumps(current or incoming or {}, ensure_ascii=False))
    submitted = incoming or {}
    previous_kind = result.get('recommended_kind') or result.get('kind')
    stage = result.get('stage') or submitted.get('stage') or 'inputs'

    if body.action == 'confirm_stage':
        if body.confirmation_stage and body.confirmation_stage != stage:
            raise HTTPException(409, '当前确认卡片已经失效，请刷新后继续')
        if stage == 'inputs':
            submitted_inputs = body.input_contract or submitted.get('inputs', [])
            submitted_by_name = {
                str(item.get('name')): item for item in submitted_inputs if item.get('name')
            }
            removed_names = {str(value) for value in body.removed_input_names}
            merged_inputs = []
            consumed_names = set()
            for item in result.get('inputs', []):
                name = str(item.get('name') or '')
                if name in removed_names and name != 'message':
                    continue
                merged_inputs.append(submitted_by_name.get(name, item))
                consumed_names.add(name)
            merged_inputs.extend(
                item for item in submitted_inputs
                if str(item.get('name') or '') not in consumed_names
            )
            result['inputs'] = normalize_studio_input_contract(
                merged_inputs,
                result.get('interaction_mode') or submitted.get('interaction_mode'),
            )
        elif stage == 'architecture':
            kind = submitted.get('recommended_kind') or submitted.get('kind') or body.kind
            if kind in {'crew', 'flow'}:
                result['kind'] = kind
                result['recommended_kind'] = kind
            for key in ('process', 'recommended_process', 'process_reason', 'architecture_reason'):
                if key in submitted:
                    result[key] = submitted[key]
        elif stage == 'generation':
            for key in ('capability_requirements', 'capability_blocked', 'capability_card', 'tools'):
                if key in submitted:
                    result[key] = json.loads(json.dumps(submitted[key], ensure_ascii=False))
            submitted_agents = {str(item.get('id')): item for item in submitted.get('agents', [])}
            for agent in result.get('agents', []):
                source = submitted_agents.get(str(agent.get('id')))
                if not source:
                    continue
                for key in ('skills', 'plugins', 'tools', 'knowledge_base_ids', 'allow_code_execution', 'user_interaction'):
                    if key in source:
                        agent[key] = json.loads(json.dumps(source[key], ensure_ascii=False))
    elif body.action == 'resolve_clarification':
        clarification = result.get('clarification') or {}
        if body.clarification_id and clarification.get('id') != body.clarification_id:
            raise HTTPException(409, '当前确认选项已经失效，请刷新后继续')
        option = next(
            (item for item in clarification.get('options', [])
             if str(item.get('value')) == str(body.clarification_value)),
            None,
        )
        if not option:
            raise HTTPException(422, '请选择有效的确认选项')
        option_patch = option.get('patch') or {}
        if isinstance(option_patch, str):
            try:
                option_patch = json.loads(option_patch)
            except json.JSONDecodeError:
                option_patch = {}
        if not isinstance(option_patch, dict):
            option_patch = {}
        else:
            option_patch = dict(option_patch)
        for protected in ('stage', 'confirmed_stages', 'kind_confirmed', 'capability_card', 'capability_blocked'):
            option_patch.pop(protected, None)
        result = _json_merge_patch(result, option_patch)
        result.setdefault('resolved_clarifications', {})[body.clarification_id] = body.clarification_value
        selected_kind = option_patch.get('recommended_kind') or option_patch.get('kind')
        if selected_kind in confirmations.KINDS:
            confirmations.mark_kind(result)
        if (body.clarification_id == 'interaction_mode'
                and option_patch.get('interaction_mode') in confirmations.INTERACTION_MODES):
            confirmations.mark_interaction(result)
        result['clarification'] = None
    elif body.action == 'confirm_capabilities':
        for key in ('capability_requirements', 'capability_blocked', 'tools'):
            if key in submitted:
                result[key] = json.loads(json.dumps(submitted[key], ensure_ascii=False))
        confirmations.mark_resources(result)
        result['capability_card'] = False
        result['capability_blocked'] = []
        result['clarification'] = None

    next_kind = result.get('recommended_kind') or result.get('kind')
    return result, bool(previous_kind in {'crew', 'flow'} and next_kind in {'crew', 'flow'} and previous_kind != next_kind)


def normalize_capability_requirements(proposal: dict, resources: dict) -> dict:
    """Remove contradictory KB requirements already covered by a retrieval tool."""
    result = json.loads(json.dumps(proposal or {}, ensure_ascii=False))
    requirements = [dict(item) for item in result.get('capability_requirements', [])]
    resource_keys = {'knowledge': 'knowledge', 'skill': 'skills', 'tool': 'tools'}
    available_by_type = {
        resource_type: {str(item.get('id')) for item in resources.get(collection, [])}
        for resource_type, collection in resource_keys.items()
    }
    for item in requirements:
        available = available_by_type.get(item.get('resource_type'), set())
        item['selected_ids'] = [
            str(value) for value in item.get('selected_ids', [])
            if str(value) in available
        ]
    tools = {str(item.get('id')): item for item in resources.get('tools', [])}
    selected_tool_ids = {
        str(value)
        for item in requirements
        if item.get('resource_type') == 'tool'
        for value in item.get('selected_ids', [])
    }
    selected_tool_ids.update(str(value) for value in result.get('tools', []))
    selected_tool_ids.update(
        str(value)
        for agent in result.get('agents', [])
        for value in (agent.get('plugins', []) or agent.get('tools', []))
    )
    retrieval_tool = any(
        (tools.get(tool_id, {}).get('kind') in {'mcp_http', 'mcp_sse'} or
         any(word in f"{tools.get(tool_id, {}).get('name', '')} {tools.get(tool_id, {}).get('description', '')}".lower()
             for word in ('knowledge', '知识', 'rag', '检索', 'retrieve')))
        for tool_id in selected_tool_ids
    )
    if retrieval_tool:
        kept = []
        removed_ids = set()
        available_knowledge = {str(item.get('id')) for item in resources.get('knowledge', [])}
        for item in requirements:
            if item.get('resource_type') == 'knowledge':
                # An explicitly selected retrieval MCP tool is the knowledge
                # provider for this plan. Do not create a second native KB
                # requirement just because the model emitted one.
                selected = {str(value) for value in item.get('selected_ids', [])}
                if not selected or not selected <= available_knowledge:
                    removed_ids.add(item.get('id'))
                    continue
            kept.append(item)
        requirements = kept
        if removed_ids:
            for agent in result.get('agents', []):
                agent['knowledge_base_ids'] = [
                    value for value in agent.get('knowledge_base_ids', [])
                    if str(value) not in removed_ids
                ]
    clarification = result.get('clarification') or {}
    clarification_text = ' '.join(str(clarification.get(key, '')) for key in ('question', 'id'))
    resource_clarification = any(
        word in clarification_text.lower()
        for word in ('knowledge', '知识库', '平台知识', '普通知识', '绑定', 'skill', '工具', 'mcp')
    )
    if resource_clarification and result.get('preflight'):
        # Resource selection is a multi-resource configuration card, never a
        # mutually exclusive clarification question.
        result['clarification'] = None
        result['capability_card'] = True
    elif resource_clarification and (requirements or retrieval_tool):
        # Resource availability is handled by the dedicated capability card.
        # It must never become a choice card that implicitly confirms the architecture.
        result['clarification'] = None
        if result.get('stage') != 'generation':
            result['capability_card'] = False
    elif resource_clarification and not requirements:
        result['clarification'] = None
        requirements = [{
            'id': 'platform_knowledge',
            'resource_type': 'knowledge',
            'label': '平台知识库',
            'reason': '当前方案需要可检索的知识库资源，请从工作空间中选择或新建一个知识库。',
            'required': True,
            'selected_ids': [],
        }]
    result['capability_requirements'] = requirements
    result['kind_confirmed'] = 'architecture' in result.get('confirmed_stages', [])
    result.pop('capability_blocked', None)
    return result


def preflight_capability_card(proposal: dict, resources: dict) -> dict:
    """Build the editable multi-resource card around model recommendations."""
    result = normalize_capability_requirements(proposal, resources)
    requirements = [dict(item) for item in result.get('capability_requirements', [])]
    optional_types = {
        'skill': ('optional_skills', '其他 Skill', '可选添加这个应用需要使用的其他技能。'),
        'tool': ('optional_tools', '其他 Tool', '可选添加这个应用需要调用的其他工具。'),
        'knowledge': ('optional_knowledge', '其他知识库', '可选添加这个应用需要检索的其他知识库。'),
    }
    existing_ids = {str(item.get('id')) for item in requirements}
    for resource_type, (requirement_id, label, reason) in optional_types.items():
        if requirement_id in existing_ids:
            continue
        requirements.append({
            'id': requirement_id,
            'resource_type': resource_type,
            'label': label,
            'reason': reason,
            'required': False,
            'selected_ids': [],
        })
    result['capability_requirements'] = requirements
    result['tools'] = list(dict.fromkeys([
        str(value)
        for item in requirements if item.get('resource_type') == 'tool'
        for value in item.get('selected_ids', [])
    ]))
    result['capability_blocked'] = missing_capability_requirements(result, resources)
    result['capability_card'] = True
    result['preflight'] = True
    result['resource_selection_confirmed'] = False
    result['clarification'] = None
    return result


def missing_capability_requirements(proposal: dict, resources: dict) -> list[dict]:
    resource_keys = {'knowledge': 'knowledge', 'skill': 'skills', 'tool': 'tools'}
    available_by_type = {
        key: {str(item.get('id')) for item in resources.get(key, [])}
        for key in ('knowledge', 'skills', 'tools')
    }
    requirements = proposal.get('capability_requirements', []) or []
    selected_tool_ids = {
        str(value) for item in requirements if item.get('resource_type') == 'tool'
        for value in item.get('selected_ids', [])
    }
    selected_tool_ids.update(str(value) for value in proposal.get('tools', []))
    selected_tool_ids.update(
        str(value)
        for agent in proposal.get('agents', [])
        for value in (agent.get('plugins', []) or agent.get('tools', []))
    )
    retrieval_tool = any(
        tools_item.get('kind') in {'mcp_http', 'mcp_sse'} or
        any(word in f"{tools_item.get('name', '')} {tools_item.get('description', '')}".lower()
            for word in ('knowledge', '知识', 'rag', '检索', 'retrieve'))
        for tool_id, tools_item in ((str(item.get('id')), item) for item in resources.get('tools', []))
        if tool_id in selected_tool_ids
    )
    missing = []
    for item in requirements:
        if item.get('required', True) is False:
            continue
        if item.get('resource_type') == 'knowledge' and retrieval_tool:
            continue
        available = available_by_type.get(resource_keys.get(item.get('resource_type'), ''), set())
        selected = {str(value) for value in item.get('selected_ids', [])}
        if not selected or not selected <= available:
            missing.append(dict(item))
    return missing


def capability_card(proposal: dict, resources: dict) -> dict:
    result = normalize_capability_requirements(proposal, resources)
    missing = missing_capability_requirements(result, resources)
    result['capability_requirements'] = missing
    result['capability_blocked'] = missing
    result['capability_card'] = True
    result['clarification'] = None
    result['confirmed_stages'] = [stage for stage in result.get('confirmed_stages', []) if stage != 'generation']
    return result


CANVAS_COLUMN_GAP = 330


CANVAS_AGENT_START_Y = 70


CANVAS_AGENT_ROW_GAP = 250


CANVAS_TASK_Y = 500


def studio_workflow(proposal: dict, orchestration_id: str, resources: dict | None = None) -> dict:
    proposal = json.loads(json.dumps(proposal or {}, ensure_ascii=False))
    normalize_definition_names(proposal)
    normalize_crew_execution_contract(proposal)
    normalize_flow_crew_execution_contract(proposal)
    # Preserve the normalized public IDs in the generated document; the
    # builder should not rename a graph a second time after this point.
    resources = resources or {}
    skills = {str(item.get('id')): item for item in resources.get('skills', [])}
    selected_details = resources.get('selected_resource_details', {}) or {}
    for item in selected_details.get('skills', []) or []:
        resource_id = str(item.get('id'))
        skills[resource_id] = {**skills.get(resource_id, {}), **item}
    tools = {
        str(item.get('id')): item
        for item in resources.get('tools', resources.get('plugins', []))
    }
    for item in selected_details.get('tools', []) or []:
        resource_id = str(item.get('id'))
        tools[resource_id] = {**tools.get(resource_id, {}), **item}
    selected_by_type = {'skills': [], 'plugins': [], 'knowledge_base_ids': []}
    type_keys = {'skill': 'skills', 'tool': 'plugins', 'knowledge': 'knowledge_base_ids'}
    resource_collections = {
        'skills': resources.get('skills', []),
        'plugins': resources.get('tools', resources.get('plugins', [])),
        'knowledge_base_ids': resources.get('knowledge', []),
    }
    for requirement in proposal.get('capability_requirements', []) or []:
        key = type_keys.get(requirement.get('resource_type'))
        if not key:
            continue
        available = {str(item.get('id')) for item in resource_collections[key]}
        selected_by_type[key].extend(
            str(value) for value in requirement.get('selected_ids', []) or []
            if str(value) in available and str(value) not in selected_by_type[key]
        )
    selected_tools = list(dict.fromkeys(
        [str(value) for value in proposal.get('tools', []) or []]
        + selected_by_type['plugins']
    ))

    source_agents = proposal.get('agents', []) or []
    source_tasks = proposal.get('tasks', []) or []
    hierarchical_crew = bool(
        proposal.get('kind', 'crew') == 'crew'
        and proposal.get('process') == 'hierarchical'
    )
    manager_agent_id = ''
    if hierarchical_crew and source_agents:
        manager_agent_id = str(
            proposal.get('manager_agent_id')
            or next((agent.get('id') for agent in source_agents if agent.get('allow_delegation')), '')
            or source_agents[0].get('id')
            or ''
        )
    interactive_task_ids: list[str] = []
    if proposal.get('interaction_mode') == 'multi_turn' and source_agents:
        interaction = proposal.get('interaction')
        interaction = interaction if isinstance(interaction, dict) else {}
        interactive_ids = {
            str(agent.get('id')) for agent in source_agents
            if agent.get('id') and agent.get('user_interaction')
        }
        configured_ids = interaction.get('interactive_task_ids')
        if configured_ids is None:
            legacy_id = str(interaction.get('collection_task_id') or '').strip()
            configured_ids = [legacy_id] if legacy_id else []
        if not isinstance(configured_ids, list):
            raise RuntimeError('interactive_task_ids 必须是数组')
        for raw_id in configured_ids:
            task_id = str(raw_id or '').strip()
            if not task_id or task_id in interactive_task_ids:
                continue
            source_task = next((task for task in source_tasks if str(task.get('id') or '') == task_id), None)
            if source_task is None:
                raise RuntimeError(f'交互节点不存在：{task_id}')
            source_type = str(source_task.get('node_type') or 'task')
            source_owners = (
                {str(source_task.get('agent_id') or '')}
                if source_type in {'task', 'agent'}
                else {str(value) for value in source_task.get('crew_agent_ids', []) or []}
                if source_type == 'crew' else set()
            )
            if not source_owners & interactive_ids:
                raise RuntimeError(f'交互节点配置无效：{task_id}')
            interactive_task_ids.append(task_id)
        if not interactive_task_ids:
            interactive_task_ids = [
                str(task.get('id')) for task in source_tasks
                if (
                    str(task.get('agent_id') or '') in interactive_ids
                    or bool({str(value) for value in task.get('crew_agent_ids', []) or []} & interactive_ids)
                )
            ]
    embedded_manager_ids = {
        str(task.get('crew_manager_agent_id') or (task.get('crew_agent_ids') or [''])[0])
        for task in source_tasks
        if task.get('node_type') == 'crew'
        and task.get('crew_process') == 'hierarchical'
        and task.get('crew_agent_ids')
    }

    agents = []
    for index, item in enumerate(proposal.get('agents', [])):
        agent_skills = list(dict.fromkeys([str(value) for value in item.get('skills', []) or []]))
        agent_plugins = list(dict.fromkeys([str(value) for value in item.get('plugins', []) or []]))
        agent_knowledge = list(dict.fromkeys([str(value) for value in item.get('knowledge_base_ids', []) or []]))
        # Convert an exact human-facing resource name/slug emitted by the
        # model to its stable id before relational persistence.
        for values, key, collection in (
            (agent_skills, 'skills', resources.get('skills', [])),
            (agent_plugins, 'plugins', resources.get('tools', resources.get('plugins', []))),
            (agent_knowledge, 'knowledge_base_ids', resources.get('knowledge', [])),
        ):
            lookup = {}
            for resource in collection:
                resource_id = str(resource.get('id'))
                lookup[resource_id] = resource_id
                for label in (resource.get('name'), resource.get('slug')):
                    normalized = re.sub(r'\s+', '', str(label or '').strip()).casefold()
                    if normalized:
                        lookup[normalized] = resource_id
            resolved = []
            for value in values:
                raw = str(value or '').strip()
                resolved_value = lookup.get(raw) or lookup.get(re.sub(r'\s+', '', raw).casefold())
                resolved.append(resolved_value or raw)
            values[:] = list(dict.fromkeys(resolved))

        # Capability-card selections describe required application abilities;
        # they are not Agent bindings. Only explicit bindings emitted by the
        # architecture/generation Composer are materialized. The generation
        # review gate reports a selected resource that nobody uses, allowing
        # the model to choose the correct Agent instead of keyword matching.
        # Code execution follows capability_policy: an explicit switch or a
        # bound Skill that ships scripts.  Task wording is never a signal.
        code_capability = bool(item.get('allow_code_execution'))
        agent_goal = item.get('goal') or item.get('purpose') or '完成分配任务'
        agent_role = item.get('role') or '任务专家'
        agent_id = str(item.get('id', f'agent_{index + 1}'))
        is_manager = agent_id == manager_agent_id or agent_id in embedded_manager_ids
        if is_manager:
            # Managers coordinate only. Do not let capability defaults or a
            # stale draft attach worker Skills, Tools, or knowledge to them.
            agent_skills = []
            agent_plugins = []
            agent_knowledge = []
        backstory = item.get('backstory') or (
            f'你是一名{agent_role}，围绕“{agent_goal}”工作，遵循输入约束并交付可验证结果。'
        )
        if hierarchical_crew and not is_manager:
            backstory += ' 在层级协作中如果发现必要信息缺失，向管理 Agent 明确汇报缺口，不直接向用户提问。'
        agents.append({'id': agent_id, 'role': agent_role,
                       'goal': agent_goal, 'backstory': backstory,
                       'model_profile_id': None, 'skills': agent_skills, 'plugins': agent_plugins,
                       'knowledge_base_ids': agent_knowledge,
                       'tools': [] if agent_plugins else item.get('tools', []), 'max_iter': 12,
                       'max_rpm': None, 'max_execution_time': None, 'max_retry_limit': 2,
                       'reasoning': item.get('reasoning', False), 'max_reasoning_attempts': None,
                        'allow_delegation': bool(is_manager), 'memory': item.get('memory', proposal.get('memory', False)),
                       'respect_context_window': True, 'multimodal': any(x.get('input_type') in {'file', 'image'} for x in proposal.get('inputs', [])),
                       'allow_code_execution': code_capability, 'inject_date': False,
                       # ``ask_user`` is an explicit Agent capability. Do not
                       # silently attach it merely because the application is
                       # multi-turn; the Composer must choose the collector
                       # Agent and set this switch in its generated contract.
                       'user_interaction': bool(item.get('user_interaction', False)),
                       'date_format': '%Y-%m-%d', 'use_system_prompt': True, 'function_calling_model_profile_id': None,
                       # The editor renders variable-height cards. Keep the
                       # initial value conservative; the final per-column
                       # layout below prevents long Agent cards from covering
                       # their Task cards.
                       'position': {'x': 120 + index * CANVAS_COLUMN_GAP,
                                    'y': CANVAS_AGENT_START_Y + index * CANVAS_AGENT_ROW_GAP}})
    requires_agent = any(
        str(item.get('node_type') or 'task') in {'task', 'agent', 'crew'}
        for item in source_tasks if isinstance(item, dict)
    )
    if not agents and (proposal.get('kind', 'crew') != 'flow' or requires_agent):
        raise RuntimeError('生成方案未包含任何 Agent，无法形成可运行编排')
    role_ids = {item['role']: item['id'] for item in agents}
    tasks = []
    if not source_tasks:
        raise RuntimeError('生成方案未包含任何 Task，无法形成可运行编排')
    source_task_ids = [str(item.get('id') or f'task_{index + 1}') for index, item in enumerate(source_tasks)]
    for index, item in enumerate(source_tasks):
        task_id = source_task_ids[index]
        node_type = 'task' if proposal.get('kind', 'crew') == 'crew' else item.get('node_type', 'agent')
        if proposal.get('kind') == 'flow' and node_type == 'task':
            node_type = 'agent'
        agent_id = (
            None if hierarchical_crew else
            item.get('agent_id') or role_ids.get(item.get('agent_role'))
            or (
                agents[min(index, len(agents)-1)]['id']
                if proposal.get('kind', 'crew') == 'crew' and agents else None
            )
        )
        if proposal.get('kind') == 'flow' and node_type in {'router', 'code', 'tool'}:
            agent_id = None
        output_variables = json.loads(json.dumps(item.get('output_variables') or [
            {'name': 'result', 'description': '任务最终输出', 'value_type': 'string'},
        ], ensure_ascii=False))
        assigned_agent = next((agent for agent in agents if agent['id'] == str(agent_id or '')), None)
        crew_agents = {
            agent['id']: agent for agent in agents
            if agent['id'] in {str(value) for value in item.get('crew_agent_ids', []) or []}
        }
        tasks.append({'id': task_id, 'name': item.get('name', f'执行步骤 {index + 1}'),
                      'description': item.get('description', item.get('objective', '')),
                      'expected_output': item.get('expected_output', '清晰、完整的最终结果'), 'agent_id': agent_id,
                      'crew_agent_ids': item.get('crew_agent_ids', []), 'crew_tasks': item.get('crew_tasks', []),
                      # Models often omit a dependency on a linear plan. Use
                      # the actual previous task id instead of assuming the
                      # synthetic ``task_1`` name, which can create an invalid
                      # graph for model-emitted ids.
                      'depends_on': (item.get('depends_on') if item.get('depends_on') is not None
                                     else ([source_task_ids[index - 1]] if index else [])),
                      'output_variables': output_variables,
                      'dependency_variables': json.loads(json.dumps(item.get('dependency_variables') or {}, ensure_ascii=False)),
                      'code_snippet': item.get('code_snippet', ''),
                      'input_bindings': json.loads(json.dumps(item.get('input_bindings') or {}, ensure_ascii=False)),
                      'execution_contract': item.get('execution_contract', 'legacy'),
                      'output_mode': item.get('output_mode', 'text'),
                      'tool_id': item.get('tool_id'),
                      'router_rules': json.loads(json.dumps(item.get('router_rules') or [], ensure_ascii=False)),
                      'routes': json.loads(json.dumps(item.get('routes') or {}, ensure_ascii=False)),
                      'condition': item.get('condition', ''),
                      'run_if': item.get('run_if', ''),
                      'node_type': node_type,
                      'crew_process': item.get('crew_process', 'sequential'),
                      'async_execution': False,
                      'crew_memory': bool(item.get('crew_memory', False)),
                      'crew_planning': bool(item.get('crew_planning', False)),
                      'crew_cache': bool(item.get('crew_cache', True)),
                      'crew_output_log_file': item.get('crew_output_log_file', ''),
                      'crew_manager_agent_id': item.get('crew_manager_agent_id'),
                      'crew_manager_model_profile_id': item.get('crew_manager_model_profile_id'),
                      'crew_planning_model_profile_id': item.get('crew_planning_model_profile_id'),
                      'crew_verbose': bool(item.get('crew_verbose', False)),
                      'human_feedback': bool(item.get('human_feedback', False)) if proposal.get('kind', 'crew') == 'flow' else False,
                      'feedback_message': item.get('feedback_message', '请审核当前结果'),
                      'feedback_outcomes': item.get('feedback_outcomes') or ['approved', 'revise'],
                      'feedback_default_outcome': item.get('feedback_default_outcome'),
                      'markdown': True, 'output_file': '', 'create_directory': True,
                      'guardrail': '', 'guardrail_max_retries': 3,
                      # Keep the generated graph readable at a glance. Main
                      # execution nodes form a horizontal lane; Agent cards
                      # are positioned above their first connected node below.
                      'position': {'x': 120 + index * CANVAS_COLUMN_GAP,
                                   'y': CANVAS_TASK_Y}})
    if (
        proposal.get('kind', 'crew') != 'flow'
        and not hierarchical_crew
        and any(not task.get('agent_id') for task in tasks)
    ) or any(
        proposal.get('kind') == 'flow'
        and task.get('node_type') in {'task', 'agent'}
        and not task.get('agent_id')
        for task in tasks
    ):
        raise RuntimeError('生成方案存在未绑定 Agent 的 Task，无法形成可运行编排')
    task_index_by_id = {task['id']: index for index, task in enumerate(tasks)}
    column_rows: dict[int, int] = {}
    for index, agent in enumerate(agents):
        assigned = [
            task_index_by_id[task['id']]
            for task in tasks
            if task.get('agent_id') == agent['id']
            or agent['id'] in {str(value) for value in task.get('crew_agent_ids', []) or []}
        ]
        column = min(assigned) if assigned else index
        row = column_rows.get(column, 0)
        column_rows[column] = row + 1
        agent['position'] = {
            'x': 120 + column * CANVAS_COLUMN_GAP,
            'y': CANVAS_AGENT_START_Y + row * CANVAS_AGENT_ROW_GAP,
        }
    if proposal.get('interaction_mode') == 'multi_turn' and tasks:
        interactive_set = set(interactive_task_ids)
        for interactive_task in tasks:
            if str(interactive_task.get('id') or '') not in interactive_set:
                continue
            if '{conversation_history}' not in str(interactive_task.get('description') or ''):
                interactive_task['description'] = (
                    '使用平台提供的当前会话历史 {conversation_history}，结合本轮运行输入完成以下工作：\n'
                    + str(interactive_task.get('description') or '')
                )
    title = str(proposal.get('title') or '').strip()
    if not title or title in {'未命名智能体', 'Untitled automation'}:
        source = str(proposal.get('summary') or proposal.get('request') or '新智能体').strip().splitlines()[0]
        title = source[:18].rstrip('，。；： ') or '新智能体'
    description = str(proposal.get('summary') or '').strip()
    if not description:
        description = f'面向{title or "用户需求"}提供可运行的 CrewAI 智能应用，按已确认输入完成处理并交付可验证结果。'
    proposal_interaction = proposal.get('interaction')
    proposal_interaction = proposal_interaction if isinstance(proposal_interaction, dict) else {}
    workflow_interaction = json.loads(json.dumps(proposal_interaction, ensure_ascii=False))
    if proposal.get('interaction_mode') == 'multi_turn':
        # Keep the legacy alias for clients that still render one collector;
        # execution is driven exclusively by the complete interactive list.
        if interactive_task_ids and not workflow_interaction.get('collection_task_id'):
            workflow_interaction['collection_task_id'] = interactive_task_ids[0]
        workflow_interaction['interactive_task_ids'] = interactive_task_ids
    workflow = {'id': orchestration_id, 'name': title, 'description': description,
            'original_request': proposal.get('original_request') or proposal.get('request', ''),
            'stage_summaries': json.loads(json.dumps(proposal.get('stage_summaries', {}), ensure_ascii=False)),
            'kind': proposal.get('kind', 'crew'), 'process': proposal.get('process', 'sequential'), 'planning': proposal.get('planning', False),
            'memory': proposal.get('memory', False),
            'cache': bool(proposal.get('cache', True)),
            'output_log_file': str(proposal.get('output_log_file') or ''),
            'model': 'workspace_default',
            'memory_policy': {
                'conversation_history': True,
                'runtime_checkpoint': True,
                'long_term_semantic': bool(proposal.get('memory', False)),
            },
            'interaction_mode': proposal.get('interaction_mode', 'single_run'),
            'interaction': workflow_interaction,
            'manager_agent_id': manager_agent_id or None,
            'capability_requirements': json.loads(json.dumps(proposal.get('capability_requirements', []), ensure_ascii=False)),
            'tools': selected_tools,
            'status': 'draft', 'agents': agents, 'tasks': tasks,
            'inputs': normalize_studio_input_contract(
                proposal.get('inputs', []), proposal.get('interaction_mode'),
            ), 'tags': [],
            'chat_history': [], 'structure_confirmed': True}
    normalize_studio_definition(workflow)
    apply_capability_policy(workflow, resources)
    ensure_message_task_reference(workflow)
    workflow['execution_graph'] = execution_graph(workflow)
    return workflow


def _selected_studio_resource_ids(proposal: dict | None) -> dict[str, set[str]]:
    selected = {'skill': set(), 'tool': set(), 'knowledge': set()}
    for requirement in (proposal or {}).get('capability_requirements', []) or []:
        resource_type = str(requirement.get('resource_type') or '')
        if resource_type in selected:
            selected[resource_type].update(
                str(value) for value in requirement.get('selected_ids', []) or []
            )
    selected['tool'].update(str(value) for value in (proposal or {}).get('tools', []) or [])
    for agent in (proposal or {}).get('agents', []) or []:
        selected['skill'].update(str(value) for value in agent.get('skills', []) or [])
        selected['tool'].update(str(value) for value in agent.get('plugins', []) or [])
        selected['knowledge'].update(
            str(value) for value in agent.get('knowledge_base_ids', []) or []
        )
    return selected


def _compact_studio_message(item: dict) -> dict:
    if not isinstance(item, dict):
        return item
    allowed = {
        'role', 'content', 'job_id', 'error', 'status', 'attachments',
        'clarification', 'clarificationAnswer',
    }
    result = {key: item[key] for key in allowed if key in item}
    proposal = item.get('proposal')
    if isinstance(proposal, dict):
        stage = str(proposal.get('stage') or 'generation')
        result['proposal'] = stage_summary(proposal, stage)
        for key in ('stage', 'clarification', 'capability_card', 'confirmed_stages'):
            if key in proposal:
                result['proposal'][key] = json.loads(json.dumps(proposal[key], ensure_ascii=False))
    return result
