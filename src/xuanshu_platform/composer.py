import json
from .capability_policy import agents_requiring_code, expects_object
from .contracts import (architecture_contract_errors, deterministic_binding_errors, design_quality_errors,
    summary_looks_like_change_note, fix_json_field_placeholders,
                        force_object_code_contract, generation_code_node_errors)
from .input_contracts import conversational_inputs

import logging
from collections.abc import Callable
from contextvars import ContextVar
from typing import Literal

from crewai import Agent, LLM
from crewai.flow.flow import Flow, listen, start
from crewai.flow.persistence import persist
from pydantic import BaseModel, Field, field_validator

from . import confirmations
from .capability_policy import apply_output_mode_policy
from .flow_persistence import NullFlowPersistence, RedisFlowPersistence
from .contracts import (
    ensure_fixed_output_contracts,
    normalize_deterministic_dependencies,
    variable_contract_errors,
)
from .memory import persistent_memory
from .model_runtime import kickoff_structured, parse_structured_output, profile_llm
from .services import composer_dir


ComposerProgressCallback = Callable[[str, str], None]
_composer_progress_callback: ContextVar[ComposerProgressCallback | None] = ContextVar(
    'composer_progress_callback', default=None,
)


def _emit_composer_progress(phase: str, message: str) -> None:
    callback = _composer_progress_callback.get()
    if callback is not None:
        callback(phase, message)


class RuntimeInput(BaseModel):
    name: str = Field(description='面向用户显示的中文输入名称')
    variable: str = Field(description='仅含 ASCII 英文字母、数字和下划线的英文 snake_case 变量名')
    type: Literal['text', 'long_text', 'file', 'image', 'number', 'boolean', 'json'] = Field(
        default='text',
        description='用户直接键入短内容用 text，多行正文用 long_text，上传外部文件用 file，上传图片用 image',
    )
    required: bool = False
    multiple: bool = False
    description: str = ''


class ClarificationOption(BaseModel):
    label: str
    value: str
    description: str = ''
    recommended: bool = False
    # Strict structured-output schemas cannot contain an open-ended object.
    # Keep the merge-patch payload as a JSON string at the model boundary and
    # normalize it back to an object before it reaches the Studio/API layer.
    patch: str = '{}'

    @field_validator('patch', mode='before')
    @classmethod
    def serialize_patch(cls, value):
        if value is None:
            return '{}'
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return '{}'
            return json.dumps(parsed, ensure_ascii=False, separators=(',', ':')) if isinstance(parsed, dict) else '{}'
        return '{}'


class Clarification(BaseModel):
    id: str
    question: str
    options: list[ClarificationOption] = Field(default_factory=list)
    allow_custom: bool = True


class ProposedAgent(BaseModel):
    id: str
    role: str
    goal: str
    backstory: str = ''
    responsibilities: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    memory: bool = False
    reasoning: bool = False
    allow_delegation: bool = False
    allow_code_execution: bool = Field(
        default=False,
        description='仅当该 Agent 需要在沙箱里运行 Python/命令（计算、生成 DOCX/XLSX 等文件）时设为 true；读取上传的文档和表格有内置工具，不需要开启；绑定带脚本的 Skill 时平台会自动开启；与用户本轮最新要求冲突时以最新要求为准',
    )
    user_interaction: bool = Field(
        default=False,
        description='仅 multi_turn 且该 Agent 的任务必须在运行中向用户提问、补充缺失信息时设为 true；起草、审核、汇总、交付等只处理上游结果的 Agent 必须为 false；层级 Crew 的所有成员（含 manager）必须为 false；与用户本轮最新要求冲突时以最新要求为准',
    )


class ProposedOutputVariable(BaseModel):
    name: str
    description: str = ''
    value_type: Literal['string', 'number', 'boolean', 'object', 'array', 'file'] = 'string'


class ProposedCrewTask(BaseModel):
    id: str
    name: str
    description: str
    expected_output: str
    agent_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    output_variables: list[ProposedOutputVariable] = Field(default_factory=list)
    dependency_variables: dict[str, list[dict]] = Field(default_factory=dict)
    async_execution: bool = False
    markdown: bool = False
    output_file: str = ''
    guardrail: str = ''
    guardrail_max_retries: int = 3
    output_mode: Literal['text', 'json'] = 'text'


def _normalize_binding(value):
    """One binding in canonical form: {source, node_id?, variable?, value?}."""
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, (dict, str))), None)
    if isinstance(value, str):
        text = value.strip().strip('{}')
        node_id, _, field = text.partition('.')
        return ({'source': 'node', 'node_id': node_id, 'variable': field}
                if field else {'source': 'input', 'variable': node_id})
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for alias in ('field', 'source_field', 'output', 'key'):
        if alias in result and not result.get('variable'):
            result['variable'] = result.pop(alias)
    if not result.get('source'):
        result['source'] = 'node' if result.get('node_id') else ('literal' if 'value' in result else 'input')
    return result


def normalize_input_bindings(value):
    """Accept the shapes models tend to return and emit ``{param: binding}``."""
    if value is None:
        return {}
    if isinstance(value, list):
        result = {}
        for item in value:
            if isinstance(item, dict):
                name = item.get('name') or item.get('param') or item.get('parameter')
                if name:
                    result[str(name)] = _normalize_binding(
                        {k: v for k, v in item.items() if k not in {'name', 'param', 'parameter'}})
        return result
    if isinstance(value, dict):
        return {str(name): _normalize_binding(item) for name, item in value.items()}
    return value


class ProposedTask(BaseModel):
    id: str
    name: str
    description: str = ''
    expected_output: str = ''
    agent_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    node_type: Literal['task', 'agent', 'crew', 'router', 'code', 'tool'] = 'task'
    crew_agent_ids: list[str] = Field(default_factory=list)
    crew_tasks: list[ProposedCrewTask] = Field(default_factory=list)
    crew_process: Literal['sequential', 'hierarchical'] = 'sequential'
    crew_memory: bool = False
    crew_planning: bool = False
    crew_cache: bool = True
    crew_output_log_file: str = ''
    crew_manager_agent_id: str | None = None
    crew_manager_model_profile_id: str | None = None
    crew_planning_model_profile_id: str | None = None
    crew_verbose: bool = False
    code_snippet: str = ''
    input_bindings: dict[str, dict] = Field(default_factory=dict)
    execution_contract: Literal['legacy', 'object'] = 'legacy'
    tool_id: str | None = None
    router_rules: list[dict] = Field(default_factory=list)

    @field_validator('input_bindings', mode='before')
    @classmethod
    def _coerce_input_bindings(cls, value):
        return normalize_input_bindings(value)
    routes: dict[str, list[str]] = Field(default_factory=dict)
    condition: str = ''
    run_if: str = ''
    human_feedback: bool = False
    feedback_message: str = '请审核当前结果'
    feedback_outcomes: list[str] = Field(default_factory=lambda: ['approved', 'revise'])
    feedback_default_outcome: str | None = None
    output_variables: list[ProposedOutputVariable] = Field(default_factory=list)
    dependency_variables: dict[str, list[dict]] = Field(default_factory=dict)
    output_mode: Literal['text', 'json'] = 'text'


class CapabilityRequirement(BaseModel):
    id: str
    resource_type: Literal['knowledge', 'skill', 'tool']
    label: str
    reason: str
    required: bool = True
    selected_ids: list[str] = Field(default_factory=list)


class ComposerDecision(BaseModel):
    intent: Literal['design', 'conversation']
    reply: str = ''
    request_summary: str = ''
    orchestration_intent_confirmed: bool = False
    application_purpose_known: bool = False
    title: str = '未命名智能体'
    kind: Literal['crew', 'flow'] = 'crew'
    summary: str = ''
    interaction_mode: Literal['single_run', 'multi_turn'] = 'single_run'
    inputs: list[RuntimeInput] = Field(default_factory=list)
    process: Literal['sequential', 'hierarchical'] = 'sequential'
    # Stage models ask for the hierarchical manager explicitly.  Without this
    # field the value was dropped on validation and the manager had to be
    # guessed from role names.
    manager_agent_id: str | None = None
    memory: bool = False
    planning: bool = False
    agents: list[ProposedAgent] = Field(default_factory=list)
    tasks: list[ProposedTask] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    capability_requirements: list[CapabilityRequirement] = Field(default_factory=list)
    clarification: Clarification | None = None


class ComposerPatch(BaseModel):
    """Stage-local update returned by architecture and generation Agents."""
    intent: Literal['design', 'conversation'] = 'design'
    reply: str | None = None
    title: str | None = None
    kind: Literal['crew', 'flow'] | None = None
    summary: str | None = Field(default=None, description='应用简介：描述应用目标、交付物和主要处理方式；只有应用目标或交付发生变化时才返回；不得写本次修改了什么，修改说明写在 reply')
    interaction_mode: Literal['single_run', 'multi_turn'] | None = None
    process: Literal['sequential', 'hierarchical'] | None = None
    manager_agent_id: str | None = None
    memory: bool | None = None
    planning: bool | None = None
    agents: list[ProposedAgent] | None = None
    tasks: list[ProposedTask] | None = None
    tools: list[str] | None = None
    capability_requirements: list[CapabilityRequirement] | None = None
    clarification: Clarification | None = None


class ArchitectureStageDecision(BaseModel):
    """Complete output contract for the architecture confirmation stage."""
    intent: Literal['design', 'conversation'] = 'design'
    reply: str = ''
    title: str = ''
    summary: str = Field(default='', description='面向用户的应用简介，说明目标、交付物和主要处理方式；不得省略')
    kind: Literal['crew', 'flow'] = 'crew'
    process: Literal['sequential', 'hierarchical'] = 'sequential'
    interaction_mode: Literal['single_run', 'multi_turn'] = 'single_run'
    manager_agent_id: str | None = None
    agents: list[ProposedAgent] = Field(default_factory=list, description='至少一个具有 role/goal/backstory/responsibilities 的 Agent')
    tasks: list[ProposedTask] = Field(default_factory=list, description='至少一个可执行的 Agent、Crew 或确定性节点')


class GenerationStageDecision(BaseModel):
    """Complete output contract for the direct generation stage."""
    intent: Literal['design', 'conversation'] = 'design'
    reply: str = ''
    title: str = ''
    summary: str = Field(default='', description='面向用户的应用简介，不得为空')
    kind: Literal['crew', 'flow'] = 'crew'
    process: Literal['sequential', 'hierarchical'] = 'sequential'
    interaction_mode: Literal['single_run', 'multi_turn'] = 'single_run'
    manager_agent_id: str | None = None
    agents: list[ProposedAgent] = Field(default_factory=list, description='至少一个完整 Agent')
    tasks: list[ProposedTask] = Field(default_factory=list, description='至少一个可执行的 Agent、Crew 或确定性节点')
    tools: list[str] = Field(default_factory=list)
    memory: bool = False
    planning: bool = False
    capability_requirements: list[CapabilityRequirement] = Field(default_factory=list)


class ArchitectureReview(BaseModel):
    approved: bool = False
    findings: list[str] = Field(default_factory=list)


def _select_crew_manager(decision: ComposerDecision, state: 'ComposerState') -> str:
    """Return the hierarchical manager chosen explicitly, or '' for none.

    Only explicit markers count: the model's ``manager_agent_id``, the
    previously confirmed manager, or the single Agent flagged with
    ``allow_delegation``.  Role names are never used, and an ordinary worker
    is never promoted.  With no marker, '' is returned and the contract
    normalizer adds a dedicated manager Agent.
    """
    agent_ids = [str(agent.id) for agent in decision.agents if agent.id]
    candidates = [
        str(decision.manager_agent_id or ''),
        str(state.existing.get('manager_agent_id') or ''),
    ]
    for candidate in candidates:
        if candidate and candidate in agent_ids:
            return candidate
    delegating = [str(agent.id) for agent in decision.agents if agent.allow_delegation]
    if len(delegating) == 1:
        return delegating[0]
    prior_delegating = [
        str(item.get('id')) for item in state.existing.get('agents', []) or []
        if isinstance(item, dict) and item.get('allow_delegation') and str(item.get('id')) in agent_ids
    ]
    return prior_delegating[0] if len(prior_delegating) == 1 else ''


def _locked_crew_process(state: 'ComposerState') -> str:
    """Crew process a revision must keep, or '' when the model may choose.

    Revising an existing Crew graph keeps the process the user confirmed,
    unless the revision router explicitly requested another one.  Adding a
    step must not silently turn a sequential Crew into a hierarchical one.
    """
    existing = state.existing or {}
    kind = str(existing.get('recommended_kind') or existing.get('kind') or state.kind or '')
    if kind == 'crew' and existing.get('interaction_mode') == 'multi_turn':
        # A hierarchical manager cannot hold tools (CrewAI), so it cannot use
        # ask_user, and workers may not ask the user.  A multi-turn top-level
        # Crew therefore only works sequentially.
        return 'sequential'
    requested = str(existing.get('requested_process') or '')
    if requested in {'sequential', 'hierarchical'}:
        return requested
    if kind != 'crew' or not existing.get('tasks'):
        return ''
    process = str(existing.get('recommended_process') or existing.get('process') or '')
    return process if process in {'sequential', 'hierarchical'} else ''


def _enforce_locked_process(decision: ComposerDecision, state: 'ComposerState') -> list[str]:
    """Apply the locked Crew process; return findings the model must repair."""
    locked = _locked_crew_process(state)
    if not locked or decision.kind != 'crew':
        return []
    decision.process = locked
    if locked != 'sequential':
        return []
    decision.manager_agent_id = None
    existing_ids = {str(item.get('id')) for item in state.existing.get('agents', []) or []
                    if isinstance(item, dict)}
    assigned = {str(task.agent_id) for task in decision.tasks if task.agent_id}
    # A coordinator the model added for a hierarchical design has no work in
    # a sequential Crew.  Drop only new, unassigned Agents, and only when the
    # other Agents are visibly assigned, so a real worker is never removed.
    if assigned:
        decision.agents = [
            agent for agent in decision.agents
            if str(agent.id) in assigned or str(agent.id) in existing_ids
        ]
    for agent in decision.agents:
        agent.allow_delegation = False
    agent_ids = {str(agent.id) for agent in decision.agents}
    return [
        f'顺序 Crew 必须为任务“{task.name or task.id}”（{task.id}）指定 agents 中已有的 agent_id'
        for task in decision.tasks
        if task.node_type in {'task', 'agent'} and str(task.agent_id or '') not in agent_ids
    ]


def _normalize_generation_contract(decision: ComposerDecision, state: 'ComposerState') -> ComposerDecision:
    """Preserve confirmed capabilities and align obvious structured edges."""
    if state.stage != 'generation' or decision.intent != 'design':
        return decision
    for task in decision.tasks:
        if task.node_type == 'code':
            # Generated code always runs as main(**input_bindings); never
            # let an omitted field fall back to the legacy template mode.
            task.execution_contract = 'object'
        if task.node_type == 'crew':
            # Crew nodes pass upstream data through placeholders/CrewAI
            # context; input_bindings are only for code/tool nodes.
            task.input_bindings = {}
        for item in [task, *(getattr(task, 'crew_tasks', []) or [])]:
            if hasattr(item, 'output_mode') and expects_object(getattr(item, 'expected_output', '')):
                item.output_mode = 'json'
                if hasattr(item, 'markdown'):
                    item.markdown = False
    locked_process = str(
        state.existing.get('recommended_process') or state.existing.get('process') or ''
    )
    if locked_process in {'sequential', 'hierarchical'}:
        decision.process = locked_process
    if decision.kind == 'crew':
        if decision.process == 'hierarchical':
            manager_id = _select_crew_manager(decision, state)
            decision.manager_agent_id = manager_id or None
            for agent in decision.agents:
                is_manager = str(agent.id) == manager_id
                agent.allow_delegation = is_manager
                if is_manager:
                    agent.skills = []
                    agent.plugins = []
                    agent.tools = []
                    agent.knowledge_base_ids = []
            for task in decision.tasks:
                if task.node_type in {'task', 'agent'}:
                    task.agent_id = None
        else:
            decision.manager_agent_id = None
            for agent in decision.agents:
                agent.allow_delegation = False
    existing_agents = {
        str(item.get('id')): item
        for item in (state.existing.get('agents') or [])
        if isinstance(item, dict) and item.get('id')
    }
    existing_requirements = {
        str(item.get('id')): item
        for item in (state.existing.get('capability_requirements') or [])
        if isinstance(item, dict) and item.get('id')
    }
    confirmed_plugins = list(dict.fromkeys(
        str(value)
        for item in existing_agents.values()
        for value in (item.get('plugins') or [])
    ))
    requirement_by_key = {
        (str(item.resource_type), str(value)): item
        for item in (decision.capability_requirements or [])
        for value in item.selected_ids
    }
    for agent in decision.agents:
        old = existing_agents.get(str(agent.id), {})
        for field in ('skills', 'plugins', 'knowledge_base_ids', 'tools'):
            values = list(dict.fromkeys([
                *(str(value) for value in getattr(agent, field, []) or []),
                *(str(value) for value in old.get(field, []) or []),
            ]))
            setattr(agent, field, values)
        if agent.skills and not agent.plugins and confirmed_plugins:
            # A selected Skill may declare a companion retrieval tool while a
            # generation response leaves the binding on another Agent. Keep
            # the confirmed tool available to the Skill's executor.
            agent.plugins = list(confirmed_plugins)
        if 'user_interaction' in old and 'architecture' in (state.existing.get('confirmed_stages') or []):
            # After the architecture card is confirmed, that card is the
            # user's latest decision for this switch (on and off).  Before
            # that, the current design turn decides; an old True is never
            # re-applied over the model's latest answer.
            agent.user_interaction = bool(old['user_interaction'])
        if 'allow_code_execution' in old and 'architecture' in (state.existing.get('confirmed_stages') or []):
            # Same rule as user_interaction: after the architecture card is
            # confirmed it is the latest decision (on and off); before that
            # the current design turn decides.  Skills that ship scripts are
            # still forced on later by capability_policy.
            agent.allow_code_execution = bool(old['allow_code_execution'])
        if agent.plugins:
            # Tools are exposed through the platform plugin binding. Keeping a
            # duplicate name in Agent.tools makes the generated contract look
            # like two separate tool registrations.
            agent.tools = []
        # Code execution is an explicit capability: it comes from the model's
        # ``allow_code_execution`` field, a confirmed card (above), the user's
        # toggle or a bound Skill with scripts.  The latest request wins.  It is never inferred from words in a task prompt.
        # Confirmed Skills are likewise never dropped by keyword matching.
        for resource_type, field in (
            ('skill', 'skills'), ('tool', 'plugins'), ('tool', 'tools'),
            ('knowledge', 'knowledge_base_ids'),
        ):
            for value in getattr(agent, field, []) or []:
                key = (resource_type, str(value))
                if key in requirement_by_key:
                    continue
                prior = next(
                    (item for item in existing_requirements.values()
                     if item.get('resource_type') == resource_type
                     and str(value) in {str(selected) for selected in item.get('selected_ids', [])}),
                    None,
                )
                requirement = prior or CapabilityRequirement(
                    id=f'generated_{resource_type}_{value}',
                    resource_type=resource_type,
                    label=str(value),
                    reason='由已绑定的 Agent 能力自动纳入应用资源契约。',
                    required=True,
                    selected_ids=[str(value)],
                )
                decision.capability_requirements.append(
                    requirement if isinstance(requirement, CapabilityRequirement)
                    else CapabilityRequirement.model_validate(requirement)
                )
                requirement_by_key[key] = decision.capability_requirements[-1]
    if decision.kind == 'crew' and decision.process == 'hierarchical':
        # The capability merge above restores prior bindings onto every Agent,
        # including the manager.  Strip them again from the one selected
        # manager only; other Agents keep their tools whatever their role name.
        for agent in decision.agents:
            if str(agent.id) == str(decision.manager_agent_id or ''):
                agent.skills = []
                agent.plugins = []
                agent.tools = []
                agent.knowledge_base_ids = []
                agent.allow_delegation = True
    # Structured output follows capability_policy: a node is JSON exactly
    # when a downstream placeholder, binding or router reads its ``object``.
    task_dicts = [task.model_dump() for task in decision.tasks]
    interactive = {str(agent.id) for agent in decision.agents if agent.user_interaction}
    apply_output_mode_policy(task_dicts, interactive)
    for task, updated in zip(decision.tasks, task_dicts):
        task.output_mode = updated['output_mode']
        task.expected_output = updated.get('expected_output') or task.expected_output
    # Code execution follows capability_policy (Skill with code, or a task that
    # must generate/run code or deliver a file) so the review sees the final state.
    code_agents = agents_requiring_code(decision.model_dump())
    for agent in decision.agents:
        if str(agent.id) in code_agents and str(agent.id) != str(decision.manager_agent_id or ''):
            agent.allow_code_execution = True
    return decision


def _mentions_id(text: str, identifier: str) -> bool:
    """Match an ID as a whole token, so ``t1`` never matches ``t10``."""
    if not identifier:
        return False

    def is_word(char: str) -> bool:
        return char == '_' or _is_ascii_letter(char) or _is_ascii_digit(char)

    start = text.find(identifier)
    while start != -1:
        end = start + len(identifier)
        before = text[start - 1] if start else ''
        after = text[end] if end < len(text) else ''
        if not (before and is_word(before)) and not (after and is_word(after)):
            return True
        start = text.find(identifier, start + 1)
    return False


def _filter_generation_findings(decision: ComposerDecision, findings: list[str]) -> list[str]:
    """Drop only review findings that the normalized contract clearly refutes.

    A finding is discarded when it names one specific task/Agent/resource and
    the claim is false for that object.  Anything ambiguous is kept, because a
    dropped finding silently counts as "review passed".
    """
    task_by_id = {str(task.id): task for task in decision.tasks}
    agent_by_id = {str(agent.id): agent for agent in decision.agents}
    requirements = {(str(item.resource_type), str(value))
                    for item in decision.capability_requirements
                    for value in item.selected_ids}
    resource_ids = {value for _, value in requirements}
    inputs_by_name = {str(item.variable): item for item in decision.inputs}
    filtered = []
    for finding in findings:
        text = str(finding)
        tasks = [task for task_id, task in task_by_id.items() if _mentions_id(text, task_id)]
        agents = [agent for agent_id, agent in agent_by_id.items() if _mentions_id(text, agent_id)]
        task_match = tasks[0] if len(tasks) == 1 else None
        agent_match = agents[0] if len(agents) == 1 else None
        if task_match and 'output_mode' in text and task_match.output_mode == 'json':
            continue
        if task_match and ('expected_output' in text or 'JSON 业务字段' in text) and task_match.output_mode == 'json':
            continue
        if agent_match and 'allow_code_execution' in text and agent_match.allow_code_execution:
            continue
        if agent_match and 'plugins' in text and agent_match.plugins:
            continue
        if 'capability_requirements' in text:
            named = {value for value in resource_ids if _mentions_id(text, value)}
            # Refuted only when it names resources that are all declared.
            if named and all(any(value == selected for _, selected in requirements) for value in named):
                continue
        if 'inputs[' in text:
            named_inputs = [name for name in inputs_by_name if _mentions_id(text, name)]
            if named_inputs and all(inputs_by_name[name].variable and inputs_by_name[name].type
                                    for name in named_inputs):
                continue
        filtered.append(text)
    return list(dict.fromkeys(filtered))


class InputComposerDecision(BaseModel):
    """Narrow first-turn contract; downstream architecture is intentionally absent."""
    intent: Literal['design', 'conversation']
    reply: str = ''
    title: str = '未命名智能体'
    kind: Literal['crew', 'flow'] = 'crew'
    summary: str = ''
    interaction_mode: Literal['single_run', 'multi_turn'] = 'single_run'
    inputs: list[RuntimeInput] = Field(default_factory=list)


class DiscoveryDecision(BaseModel):
    """Small contract for the fast preflight clarification stage."""
    intent: Literal['design', 'conversation']
    reply: str = ''
    request_summary: str = Field(
        default='',
        description='intent=design 时，将当前消息与相关历史合并成可独立理解的一段应用需求',
    )
    orchestration_intent_confirmed: bool = Field(
        default=False,
        description='整段会话是否已明确要求创建或修改智能体应用',
    )
    application_purpose_known: bool = Field(
        default=False,
        description='整段会话是否已说明智能体要完成的具体业务用途',
    )
    interaction_mode: Literal['single_run', 'multi_turn'] | None = None
    interaction_mode_explicit: bool = False
    kind: Literal['crew', 'flow'] | None = None
    kind_explicit: bool = False
    tools: list[str] = Field(default_factory=list)
    capability_requirements: list[CapabilityRequirement] = Field(default_factory=list)
    resource_selection_explicit: bool = False
    resource_configuration_required: bool = False
    clarification: Clarification | None = None


class ComposerState(BaseModel):
    id: str = ''
    request: str = ''
    stage: str = 'inputs'
    kind: str = 'auto'
    existing: dict = Field(default_factory=dict)
    model: dict = Field(default_factory=dict)
    resources: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)
    memories: list[str] = Field(default_factory=list)
    analysis: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    review_policy: Literal['always', 'never', 'on_kind_change', 'review_only'] = 'always'
    existing_kind: str = ''
    discovery_kind_explicit: bool = False
    discovery_interaction_explicit: bool = False
    discovery_resource_explicit: bool = False
    discovery_resource_configuration: bool = False


def _stage_existing_context(state: ComposerState) -> dict:
    """Expose only the confirmed contract that the current stage can change.

    The Studio keeps the full proposal for rendering and persistence, but the
    stage Agent should not receive the whole transcript/graph on every turn.
    This keeps stage boundaries explicit and prevents a later stage from
    silently rewriting an earlier confirmation.
    """
    existing = state.existing or {}
    def prompt_inputs(values):
        normalized = []
        for item in values or []:
            if not isinstance(item, dict):
                continue
            variable = str(item.get('variable') or item.get('name') or '').strip()
            label = str(item.get('label') or item.get('name') or variable).strip()
            input_type = item.get('type') or item.get('input_type') or 'text'
            if variable:
                normalized.append({
                    'name': label,
                    'variable': variable,
                    'type': input_type,
                    'required': bool(item.get('required', False)),
                    'multiple': bool(item.get('multiple', False)),
                    'description': item.get('description', ''),
                })
        return normalized
    common = {
        key: existing[key]
        for key in ('title', 'original_request', 'interaction_mode',
                    'interaction_mode_preselected', 'resolved_clarifications',
                    'orchestration_intent_confirmed', 'application_purpose_known',
                    'architecture_constraints')
        if key in existing
    }
    summaries = existing.get('stage_summaries') or {}
    stage_order = ('discovery', 'inputs', 'architecture', 'generation')
    current_index = stage_order.index(state.stage) if state.stage in stage_order else len(stage_order)
    prior_summaries = {
        key: value for key, value in summaries.items()
        if key in stage_order and stage_order.index(key) < current_index
    }
    if prior_summaries:
        common['stage_summaries'] = prior_summaries
    if state.stage == 'discovery':
        return {
            **common,
            **{key: existing[key] for key in (
                'resource_selection_confirmed', 'kind_preselected', 'kind_confirmed',
                'capability_requirements',
            ) if key in existing},
        }
    if state.stage == 'inputs':
        return {
            **common,
            'interaction_mode': existing.get('interaction_mode', state.existing.get('interaction_mode', 'single_run')),
            'inputs': prompt_inputs(existing.get('inputs', [])),
            'capability_requirements': existing.get('capability_requirements', []),
        }
    if state.stage == 'architecture':
        selected_kind = existing.get('recommended_kind') or existing.get('kind') or state.kind
        context = {
            **common,
            'locked_process': _locked_crew_process(state) or None,
            'inputs': prompt_inputs(existing.get('inputs', [])),
            'kind': existing.get('recommended_kind') or existing.get('kind') or state.kind,
            'process': ('sequential' if selected_kind == 'flow' else existing.get('recommended_process') or existing.get('process', 'sequential')),
            'requested_process': existing.get('requested_process'),
            'requested_crew_node_id': existing.get('requested_crew_node_id'),
            'revision_instruction': existing.get('revision_instruction', ''),
            'revision_log': existing.get('revision_log', []),
            'capability_requirements': existing.get('capability_requirements', []),
            'selected_resource_details': state.resources.get('selected_resource_details', {}),
        }
        if existing.get('tasks'):
            context['confirmed_graph'] = {
                'agents': existing.get('agents', []),
                'tasks': existing.get('tasks', []),
            }
            context['architecture_confirmed'] = bool(
                existing.get('architecture_confirmed')
                or 'architecture' in (existing.get('confirmed_stages') or [])
            )
        return context
    architecture_summary = prior_summaries.get('architecture') or {}
    graph_agents = architecture_summary.get('agents') or existing.get('agents', [])
    graph_tasks = architecture_summary.get('tasks') or existing.get('tasks', [])
    selected_kind = existing.get('recommended_kind') or existing.get('kind') or state.kind
    context = {
        **common,
        'inputs': prompt_inputs(existing.get('inputs', [])),
        'kind': existing.get('recommended_kind') or existing.get('kind') or state.kind,
        'process': ('sequential' if selected_kind == 'flow' else existing.get('recommended_process') or existing.get('process', 'sequential')),
        'requested_process': existing.get('requested_process'),
        'requested_crew_node_id': existing.get('requested_crew_node_id'),
        'revision_instruction': existing.get('revision_instruction', ''),
        'revision_log': existing.get('revision_log', []),
        'capability_requirements': existing.get('capability_requirements', []),
        'agents': graph_agents,
        'tasks': graph_tasks,
        'tools': existing.get('tools', []),
        'selected_resource_details': state.resources.get('selected_resource_details', {}),
    }
    if existing.get('tasks'):
        context['confirmed_graph'] = {
            'agents': graph_agents,
            'tasks': graph_tasks,
        }
        context['architecture_confirmed'] = bool(
            existing.get('architecture_confirmed')
            or 'architecture' in (existing.get('confirmed_stages') or [])
        )
    return context


def _stage_request_payload(state: ComposerState) -> dict:
    """Build the small, stage-local request envelope sent to an Agent."""
    payload = {
        'stage': state.stage,
        'original_request': state.existing.get('original_request') or state.request,
        'latest_user_message': state.request,
        'confirmed_context': _stage_existing_context(state),
        'stage_summary': (state.existing.get('stage_summaries') or {}).get(state.stage, {}),
        'design_summary': design_summary(state.existing),
    }
    if state.stage == 'discovery' and state.history:
        payload['conversation_history'] = state.history
    return payload


def design_summary(existing: dict | None) -> str:
    """Build a compact semantic handoff without duplicating full proposals."""
    existing = existing or {}
    original = str(existing.get('original_request') or existing.get('request_summary') or '').strip()
    kind = existing.get('recommended_kind') or existing.get('kind') or '未确定'
    process = existing.get('recommended_process') or existing.get('process') or '未确定'
    inputs = '、'.join(
        str(item.get('label') or item.get('name') or item.get('variable'))
        for item in existing.get('inputs', []) or [] if isinstance(item, dict)
    ) or '未确定'
    agents = '、'.join(
        str(item.get('role') or item.get('id'))
        for item in existing.get('agents', []) or [] if isinstance(item, dict)
    ) or '未生成'
    tasks = '、'.join(
        str(item.get('name') or item.get('id'))
        for item in existing.get('tasks', []) or [] if isinstance(item, dict)
    ) or '未生成'
    return (
        f'原始需求：{original or "未记录"}\n'
        f'编排类型：{kind}；执行方式：{process}\n'
        f'运行输入：{inputs}\n'
        f'当前 Agent：{agents}\n'
        f'当前任务：{tasks}'
    )


def composer_prompt(state: ComposerState) -> list[dict]:
    selected_kind = str(state.existing.get('recommended_kind') or state.existing.get('kind') or state.kind or 'auto')
    if selected_kind == 'crew':
        kind_rules = ('当前已确定为 Crew 编排：只设计 CrewAI Agent、Task、depends_on 和 process。'
                      '不要生成 Flow 节点、router、code 或 tool；process 只能是 sequential 或 hierarchical。'
                      'sequential 时每个顶层 Task 必须填写 agent_id，且从第二个任务开始每个任务的 depends_on 必须包含前一个任务的 ID（如 task_2 depends_on ["task_1"]，task_3 depends_on ["task_2"]），形成完整的顺序执行链；hierarchical 时必须额外创建一个独立的 manager Agent。'
                      'manager Agent 不得承担任何 Task，不得绑定 Skill、Tool、Knowledge 或代码执行能力，只负责动态分配、协调和验收；'
                      '所有顶层 Task 都不得填写 agent_id，由 manager 动态分配；仅 manager 的 allow_delegation=true，其他 Agent=false。')
    elif selected_kind == 'flow':
        kind_rules = ('当前已确定为 Flow 编排：这是节点图，不是 Crew process。'
                      '顶层可使用 agent、crew、router、code、tool；process 固定填写 sequential。'
                      'Flow 中的 crew 节点内部复用 Crew 规则：内部 process=sequential 时每个内部 Task 必须填写属于 crew_agent_ids 的 agent_id，且从第二个内部任务开始每个任务的 depends_on 必须包含前一个内部任务的 ID（如 task_2 depends_on ["task_1"]，task_3 depends_on ["task_2"]），形成完整的顺序执行链；'
                      '内部 process=hierarchical 时必须在 crew_agent_ids 中额外创建一个独立 manager Agent，'
                      '该 manager 不得承担任何内部 Task，不得绑定 Skill、Tool、Knowledge 或代码执行能力；所有内部 Task 的 agent_id 必须为空，由 manager 动态分配，'
                      '仅 manager 才能 allow_delegation。不得把执行某个内部 Task 的 Agent 同时设为 manager。')
    else:
        kind_rules = '编排类型尚未确定，不要臆造类型专属字段。'
    contracts = {
        'architecture': '''你是编排架构 Agent。只完成架构阶段：基于已锁定的输入和前置选择，设计最小可运行的编排图。
{kind_rules}
Flow 支持 agent、crew、router、code、tool 节点。所有节点都必须有 description，说明该节点的职责、上游依据和交付内容；Crew 节点必须有明确的 crew_agent_ids；普通 Agent 节点必须通过唯一的 Agent→任务连线指定执行者。资源优先原则：若已选择的 Skill 包含可执行脚本文件（scripts/ 目录或 .py/.sh 等后缀），必须将该 Skill 绑定到负责最终交付的 Agent，由 Agent 启用代码执行能力并调用 Skill 完成工作，禁止为该工作单独创建 code 节点；只有在没有合适 Skill 且需要纯计算逻辑时才使用 code 节点。明确的 HTTP/Python 工具调用优先使用 tool 节点。节点输出字段由平台固定：Agent/Crew/Tool 为 object、file、text，Code 为 output、file、text，Router 为 route，模型不能自定义。Code 使用 execution_contract="object"，main(显式参数) 返回对象：函数体用到的每个上游数据或运行输入都必须声明为 main 参数，并在 input_bindings 中逐个绑定（上游数据写 source=node、node_id=直接上游节点 ID、variable=text/object/output；运行输入写 source=input），禁止在函数体里使用未定义变量；Tool 同样把每个工具参数写入 input_bindings，参数值优先绑定上游节点字段。Flow 节点通过节点 ID 引用所有可达上游字段，Code/Tool 参数写入 input_bindings；Crew 内部 Task 继续使用 CrewAI context。代码不能插值用户输入到源码。
返回 ArchitectureStageDecision JSON，必须包含：summary（面向用户的应用简介，1-2 句）、kind、process、interaction_mode、manager_agent_id（层级 Crew 必填且必须对应独立管理 Agent）、agents、tasks。
   confirmed_context.inputs 是用户已经确认且可能删改过的唯一运行输入清单，必须逐项读取其中的机器变量名；**严禁继续引用清单中已删除的变量**，每次编写节点 description 或 Task expected_output 前必须先核对变量是否存在于 confirmed_context.inputs 中，不存在的变量绝对不能使用。若 confirmed_context.requested_process 存在，它是用户本轮通过阶段路由确认的 Crew 运行方式；Flow 中若同时有 requested_crew_node_id，只修改该 Crew 节点的内部运行方式和成员任务绑定；若 locked_process 存在，process 必须保持该值，不得增删 manager；revision_instruction 是本轮修改约束，必须结合原始需求和当前图执行；revision_log 是用户历次修改（按时间先后），仍须遵守，相互冲突时以后出现的为准；stage=failed 的条目是上一轮失败原因，不要重复导致失败的结构。每个 Agent 必须有 id、具体 role、可执行 goal、2-3 句 backstory 和 2-5 条 responsibilities；Flow 的 agent 节点通过 Agent 连线绑定一个 Agent，router/code/tool 节点不绑定 Agent；语言任务填写 description 和 expected_output，确定性 code/tool 只填写执行契约和 input_bindings。只追踪从运行输入到最终交付的完整闭环端到端数据流，并把运行输入占位符写成 `{variable}`。
   架构阶段必须一次性完成变量来源设计：首节点直接使用 confirmed_context.inputs 中存在的运行输入；Flow 顶层节点的占位符必须写成 `{variable}` 或 `{node_id.field}`，禁止裸变量名或裸的”节点ID.字段”文本，严禁深层引用如 `{node_id.field.subfield}`（object 类型的内部字段由执行器自行解析，不得在架构阶段预设路径）。Code/Tool 节点在架构阶段只声明用途、依赖（depends_on）和 tool_id，并在 description 中写明需要哪些上游节点数据和运行输入；不要写 code_snippet 和 input_bindings，它们由生成阶段写出；删除 code/tool 节点时，原需求中的交付物（如 Word 文件）必须保留，改由负责最终交付的 Agent 生成，并在其任务 expected_output 写明文件交付；Crew 内部 Task 的依赖使用 depends_on 和 CrewAI context。不要生成 dependency_variables 作为 Flow 顶层数据传递机制，不要创建自定义 output_variables。只按这些规则生成一次架构，不调用额外审查 Agent。
   上下文设计约束：若 `confirmed_context.architecture_confirmed=true`，`confirmed_context.confirmed_graph` 是用户已确认且锁定的架构；本次为校验重试时只能修正 description、expected_output、output_mode、input_bindings、资源开关和变量引用，必须保持 Agent 数量/ID、Task 数量/ID、node_type、depends_on、crew_agent_ids 与 crew_tasks 拓扑完全不变。若用户明确要求结构修改，才允许改变拓扑，且必须按要求精确执行。用户说“只需要三个 Agent，后两个放一个 Crew 中”时，顶层保持三个 Agent 定义，Flow 调用节点为一个独立 Agent 节点加一个 Crew 节点，Crew 使用后两个成员；不得生成四个独立 Agent 调用节点。
   multi_turn 中只有任务必须在运行中向用户提问、补充缺失信息的 Agent 才能启用 user_interaction=true，起草、审核、汇总、交付等只处理上游结果的 Agent 必须为 false，与用户本轮最新要求冲突时以最新要求为准；不要生成单一 collection_task_id。每个 Agent 使用独立请求通道，执行到对应节点时暂停并按节点恢复。层级 Crew 的所有成员（含 manager）都不能交互，manager 不能绑定任何工具（包括 ask_user），其他成员把缺失信息汇报给 manager；Flow 需要问用户时，在层级 Crew 节点之前放一个启用 user_interaction 的 Agent 节点；层级 Crew 内部 Task 不填写 agent_id，由 manager 动态分配；顺序 Crew 必须为每个内部 Task 指定 agent_id，且从第二个内部任务开始每个任务的 depends_on 必须包含前一个内部任务的 ID（如 task_2 depends_on ["task_1"]，task_3 depends_on ["task_2"]），形成完整的顺序执行链；single_run 禁止 ask_user。user_interaction 只声明平台能力开关，不得把 ask_user 调用规则写入 Agent goal、backstory、responsibilities 或 Task description/expected_output，运行时会自动注入。尊重已确认的 kind、interaction_mode、inputs 和资源，不返回 generation 专属配置。
   资源绑定约束：首先检查 confirmed_context.resource_selection_confirmed 状态。若为 true，则用户已确认资源选择，必须严格以 confirmed_context.capability_requirements 中的 selected_ids 为唯一权威来源，并从 available_resources.selected_resource_details 中读取对应资源详情配置 Agent；资源 ID 只在各自 resource_type 内唯一，同一个数字可能同时是不同的 Skill、Tool 或 Knowledge，skill 只能写入 agent.skills、tool 只能写入 agent.plugins、knowledge 只能写入 agent.knowledge_base_ids，不得跨类型解析或添加 capability_requirements 之外的资源。若 resource_selection_confirmed 为 false 或不存在，说明资源尚未经用户确认，此时不得为任何 Agent 绑定 Skill、Tool 或 Knowledge，所有 agents 的 skills、plugins、knowledge_base_ids 必须保持空数组。''',
        'generation': '''你是编排生成 Agent。只完成生成阶段：把已确认的架构直接落实为可运行定义，检查每个节点的详细开关、Agent/Task 绑定、变量可达、资源绑定和最终交付。
{kind_rules}
返回 GenerationStageDecision JSON，必须包含完整可执行的 agents 和 tasks，并包含 manager_agent_id；保留已确认的 inputs、interaction_mode、kind、资源选择。遵守 revision_log 中用户历次修改，冲突时以后出现的为准。若 architecture_confirmed=true，必须逐项保留 confirmed_graph 的节点数、ID、node_type、依赖边、Crew 成员及内部任务拓扑，只填充实现细节和修复指出的问题，不可拆 Crew、增加/删除 Agent 或 Task、将 Crew 成员改为独立调用。补齐每个 Agent 的 role、goal、backstory、responsibilities；语言 agent 节点必须通过连线绑定 Agent，router/code/tool 按各自确定性契约运行，不得为它们虚构 Agent。节点输入在本阶段确定：每个 code 节点必须写出完整 code_snippet（main 显式声明全部参数）和逐个对应的 input_bindings，每个 tool 节点必须写出 input_bindings，input_bindings 必须是对象 {参数名: {source, node_id, variable}}；上游字段只能用该节点的固定输出：Agent/Crew 节点为 text、object、file，Code 节点为 output、file、text，Tool 节点为 object、file、text，Router 为 route；架构卡片不包含这些内容，不能沿用或留空。修正不可达变量时必须改为可达的上游引用（如 {agent_1.object}），不能只删除；Crew 内部第一个任务必须引用上游节点输出。下游读取 object、或输出要求是 JSON/结构化对象的任务必须 output_mode=json，JSON 字段名写固定英文名，不能写成 {变量} 占位符。需要生成并执行代码或导出文件的 Agent 必须 allow_code_execution=true。summary 始终是应用简介，不写本次修改说明。summary 为空时也必须生成面向用户的应用简介。不要改变用户已锁定的前置选择。
   confirmed_context.inputs 和 confirmed_context.tasks 是上一轮用户确认后的权威契约，必须保留用户删改后的输入和节点拓扑。固定输出字段不可修改：Agent/Crew/Tool 使用 object、file、text；Code 使用 output、file、text；Router 使用 route。逐个扫描所有提示词和内部 Crew Task，占位符必须严格写成 `{variable}` 或 `{node_id.field}`，严禁深层引用如 `{node_id.field.subfield}`（例如禁止 `{crew_1.object.body}`、`{agent_1.object.title}` 等，object 类型的内部字段由执行器自行解析，不得在生成阶段预设路径），裸变量名和裸的”节点ID.字段”文本都不算引用；Code/Tool 逐项检查 main 参数或工具参数与 input_bindings 一一对应。先模拟首节点到最终节点的数据传递，发现任何变量不存在就修正后再返回。
   交互约束：multi_turn 中只有任务必须在运行中向用户提问、补充缺失信息的 Agent 才能启用 user_interaction=true，起草、审核、汇总、交付等只处理上游结果的 Agent 必须为 false，与用户本轮最新要求冲突时以最新要求为准；不要生成单一 collection_task_id。每个 Agent 使用独立请求通道，执行到对应节点时暂停并按节点恢复。层级 Crew 必须有独立 manager Agent；manager 不执行任何 Task、不得绑定 Skill/Tool/Knowledge/代码能力，只有 manager 可以 allow_delegation=true；所有成员（含 manager）的 user_interaction 必须为 false，缺少信息时在任务结果中明确汇报给 manager。层级 Crew 所有内部 Task 的 agent_id 必须为空，由 manager 动态分配；顺序 Crew 必须为每个内部 Task 指定 agent_id，且从第二个内部任务开始每个任务的 depends_on 必须包含前一个内部任务的 ID（如 task_2 depends_on ["task_1"]，task_3 depends_on ["task_2"]），形成完整的顺序执行链。user_interaction 只声明平台能力开关，不得把 ask_user 调用规则写入 Agent goal、backstory、responsibilities 或 Task description/expected_output，运行时会自动注入。
   文件约束：file 是平台固定输出字段，实际值由执行回执填写。需要读取或生成文件时，Code/Tool/Agent 都可以通过对应的 file 字段使用；不得自定义 generated_files、draft_docx 等输出字段，也不要把 MinIO key、本地绝对路径或 Markdown 伪链接当成文件变量。
资源绑定约束：首先检查 confirmed_context.resource_selection_confirmed 状态。若为 true，则用户已确认资源选择，必须严格保留 confirmed_context.capability_requirements 中已确认的 resource_type 与 selected_ids，并以 available_resources.selected_resource_details 中的同类型详情配置 Agent；资源 ID 只在各自类型内唯一，skill→agent.skills、tool→agent.plugins、knowledge→agent.knowledge_base_ids，严禁按相同数字跨类型替换或添加 capability_requirements 之外的资源。**Skill/Tool/Knowledge 绑定规则：只绑定给实际需要该能力完成工作的 Agent；负责澄清需求、收集信息、询问用户、确认参数的 Agent 不得绑定任何 Skill/Tool/Knowledge（这些 Agent 只与用户交互，不执行具体工作）；负责起草内容、生成文件、调用外部服务、执行计算的 Agent 才需要绑定对应资源；若多个 Agent 都需要执行相同类型的工作（如都需要生成文件），则都应绑定相同的 Skill；按 Agent 的实际职责和任务描述判断，而非按节点顺序或名称关键词。** 若 resource_selection_confirmed 为 false 或不存在，说明资源尚未经用户确认，此时不得为任何 Agent 绑定 Skill、Tool 或 Knowledge，所有 agents 的 skills、plugins、knowledge_base_ids 必须保持空数组。''',
    }
    contracts = {key: value.replace('{kind_rules}', kind_rules) for key, value in contracts.items()}
    output_guidance = ('语言任务使用 output_mode 开关：默认 text，直接输出正文；需要供下游使用结构化对象时设置 json，'
                       'expected_output 明确业务字段。该设置属于任务，同一 Agent 可执行不同模式的任务。'
                       'Flow Crew 节点的输出模式由最后一个内部任务决定；需要结构化对象时把该内部任务设为 json，'
                       '中间任务保留各自模式。')
    system = output_guidance + '你是玄枢平台的 CrewAI 应用总设计师，当前只负责一个阶段。' + contracts.get(
        state.stage, '只处理当前阶段的职责并返回对应结构化 JSON。'
    )
    if state.stage == 'architecture':
        system += ' 架构阶段结束后用户会单独确认架构，确认前不得暗示已生成最终应用。'
    if state.stage == 'generation':
        system += ' 生成结果会立即转换为画布工作流，不存在额外的生成清单确认步骤。'
    payload = _stage_request_payload(state)
    payload['available_resources'] = {
        key: value for key, value in state.resources.items()
        if key in {'skills', 'tools', 'knowledge', 'selected_resource_details'}
    }
    payload['user_design_preferences'] = state.memories
    schema_name = {
        'architecture': 'ArchitectureStageDecision',
        'generation': 'GenerationStageDecision',
    }.get(state.stage, 'ComposerPatch')
    return [
        {'role': 'system', 'content': system + f' 只返回符合 {schema_name} schema 的结构化结果，不输出推理。'},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]


def discovery_prompt(state: ComposerState) -> list[dict]:
    """Compact prompt used before the full Composer contract is needed."""
    system = '''你是玄枢编排会话入口 Agent。结合 conversation_history、latest_user_message 和 confirmed_context，先检查两个必须同时满足的入口门槛：
A. orchestration_intent_confirmed：用户已明确要求创建、编排或修改一个智能体应用；
B. application_purpose_known：用户已说明智能体具体用来完成什么业务，例如公文写作、合同审核、知识问答或生成某类交付物。
这两个条件可以由多轮消息共同表达，不能只看第一条或匹配固定词。“帮我做个智能体”“创建一个应用”“做个好用的助手”只有编排意图，没有具体用途，B 必须为 false；不得把“智能体”本身当作用途。用途至少应能回答“这个智能体替用户完成什么工作”，但此时不要求用户已经给出全部运行参数。
只有 A、B 同时为 true，才返回 intent=design、填写可独立理解且包含具体用途的 request_summary，并开始检查三个架构前提：
1. 运行交互方式：single_run 一次提交，或 multi_turn 分步对话；
2. 是否使用当前 available_resources 中的 Skill、Tool、Knowledge；
3. 编排类型：crew 或 flow。
只要 A 或 B 任一不满足，就必须返回 intent=conversation、clarification=null、resource_configuration_required=false、request_summary=''，不创建卡片、不推进 discovery。A 已满足但 B 缺失时，reply 只自然追问一个问题：“你希望这个智能体具体帮你完成什么工作？”；B 已满足但 A 缺失时，继续自然对话，不擅自开始编排。仅有问候、寒暄、测试文本、无意义字符或普通问答时，在 reply 中自然、简短地直接回应当前消息，不要机械要求创建应用。不得因为 schema 默认字段或系统要求检查前提就推断成 design。
当多轮内容已经共同形成明确的创建或修改需求，返回 intent=design，并在 request_summary 中只合并与应用目标有关的信息，写成脱离聊天记录也能独立理解的需求摘要；忽略寒暄和无关内容。用户说“把刚才讨论的做成应用”之类指代语时，必须从历史补齐所指目标。后续阶段只会读取 request_summary，不会重放整段陪聊历史。
用户已经明确说明的内容必须跳过。只有用户原话或 existing_proposal 中的确认标记明确给出交互方式时，interaction_mode_explicit 才能为 true；你的推荐不算用户已确认。编排类型同理，只有用户原话或确认标记明确给出时 kind_explicit 才能为 true。只有用户原话明确选择了 available_resources 中的具体资源或明确说不使用资源时，resource_selection_explicit 才能为 true；根据目标作出的推荐不算用户已确认。严格按交互方式、资源配置、编排类型的顺序检查，前一项未确认时不得询问后一项。交互方式未明确时返回 clarification.id=interaction_mode。资源步骤不是选择题：当资源未明确时，必须返回 resource_configuration_required=true、clarification=null，并根据目标分析需要的 Skill、Tool、Knowledge。每项需要写入 capability_requirements；available_resources 中已有且匹配的资源必须把真实字符串 id 放入 selected_ids，作为卡片默认推荐，允许推荐多个。若必需能力当前不存在，required=true 且 selected_ids=[]，明确说明缺少什么和原因，由配置卡阻止继续。不要为了凑数推荐无关资源。资源配置确认后，才可返回 clarification.id=orchestration_kind 询问 crew 或 flow。existing_proposal 中默认出现的 kind/recommended_kind 只是界面占位；只有 kind_preselected=true、kind_confirmed=true 或 resolved_clarifications.orchestration_kind 存在时才表示用户已经确认类型。每次最多提出一个 clarification，选项必须互斥，patch 必须是最小 JSON Merge Patch。三个前提都明确且所需能力已可用后返回 clarification=null，不要生成输入字段、Agent、Task 或完整架构。只返回 DiscoveryDecision JSON。'''
    next_unconfirmed_step = confirmations.next_preflight_step(state.existing, state.kind)
    payload = {
        'next_unconfirmed_step': next_unconfirmed_step,
        **_stage_request_payload(state),
        'available_resources': {
            key: value for key, value in state.resources.items()
            if key in {'skills', 'tools', 'knowledge'}
        },
    }
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]


def input_prompt(state: ComposerState) -> list[dict]:
    """Small input-contract prompt used only after discovery is complete."""
    system = '''你是玄枢平台的运行输入设计助手。只设计发布后用户可见的输入契约，不生成 Agent、Task、Tool、Skill、Knowledge 或架构。
1. 第一个输入固定为 name=用户需求、variable=message、type=long_text、required=true，用于本轮需求描述，不得省略或改名。
2. interaction_mode 必须保留 existing_proposal 已确认的值。multi_turn 保留 message 和独立业务字段，用户交互只使用 text/long_text 或 file/image；数字、布尔、JSON 等业务值以文本收集，由执行 Agent 理解、校验并转换，禁止删除这些字段。single_run 必须一次列全完成最终交付所需、且应用无法从已配置资源或其他字段可靠取得的外部信息。不要把 message 当作所有业务字段的替代品：逐项反推最终交付物，凡是缺失后会迫使执行 Agent 追问、猜测或无法完成的独立信息，都应成为明确输入；只有真正可选或可由应用推断的内容才省略或设为非必填。
3. 文件输入只有一个固定变量 files，类型为 file 列表；外部文档、参考资料、附件、模板和历史文件使用 file。用户每轮上传的文件追加到该列表，不设计 reference_file 或其他文件变量，也不要输出 multiple 开关。直接键入短内容用 text，多行正文用 long_text。
4. name 使用中文显示名；variable 使用 ASCII snake_case。输入之间不得重复：message 用于总体需求和补充说明，结构化字段承载必须明确提供的关键值。返回前逐项检查“目标、对象/范围、内容依据、输出约束以及必要附件”是否都有对应来源；只保留与当前业务实际相关的项。
只返回 InputComposerDecision JSON，不解释架构。'''
    payload = _stage_request_payload(state)
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]


def graph_completion_prompt(state: ComposerState, decision: ComposerDecision) -> list[dict]:
    """Ask the same design Agent to finish an incomplete graph in-place."""
    return [
        {'role': 'system', 'content': (
            '你是玄枢平台的 CrewAI 应用总设计师。你仍在当前编排阶段，上一份结构化结果不完整：必须同时返回至少一个 Agent 和一个 Task，'
            '并让每个 Task 绑定真实 Agent。保留用户已确认的输入、交互方式、能力和编排类型；'
            '只补齐可运行的 agents 和 tasks，以及确实需要同步修改的字段。补齐前逐项核对 inputs、depends_on、固定输出字段和可达上游变量，不能引用不存在的变量或字段。Flow 顶层不要生成 dependency_variables；Code/Tool 参数使用 input_bindings。不要返回空数组，不要解释，只返回 ComposerPatch JSON。'
        )},
        {'role': 'user', 'content': json.dumps({
            **_stage_request_payload(state),
            'incomplete_result': decision.model_dump(),
        }, ensure_ascii=False)},
    ]


def architecture_contract_correction_prompt(state: ComposerState, proposal: ComposerDecision,
                                            findings: list[str]) -> list[dict]:
    """Repair an invalid architecture with the same stage Agent, without a review Agent."""
    return [
        {'role': 'system', 'content': (
            '你是玄枢平台的架构编排修正 Agent，仍然只负责 architecture 阶段。'
            '上一份架构没有形成可执行的变量来源契约，请按确定性校验结果原地修正。'
            '保留 Agent、Task 的数量、ID、角色、绑定和 depends_on 拓扑；返回完整 tasks。'
            '平台固定节点输出契约不可修改：Agent/Crew/Tool 只能输出 object、file、text，'
            'Code 只能输出 output、file、text，Router 只能输出 route。'
            'Flow 节点可以引用所有可达上游节点，变量必须写成“节点ID.字段”，重复名称也必须按节点ID区分。'
            'Flow 顶层节点不使用 dependency_variables；Code/Tool 用 input_bindings，Crew 内部任务继续使用 CrewAI context。'
            '不得返回自定义 output_variables，也不得把运行输入或上游结果伪装成新的字段。'
            '只返回 ComposerPatch JSON，不输出解释或审查过程。'
        )},
        {'role': 'user', 'content': json.dumps({
            **_stage_request_payload(state),
            'invalid_architecture': proposal.model_dump(),
            'contract_findings': findings,
        }, ensure_ascii=False)},
    ]


def discovery_correction_prompt(state: ComposerState, prior: DiscoveryDecision,
                                expected_step: str) -> list[dict]:
    return [
        {'role': 'system', 'content': (
            '你是玄枢编排前置确认助手。上次输出没有遵守逐项确认协议。'
            f'当前唯一允许的步骤是 {expected_step}。'
            'interaction_mode 表示返回 clarification.id=interaction_mode；'
            'resources 表示 clarification=null 且 resource_configuration_required=true；'
            'orchestration_kind 表示返回 clarification.id=orchestration_kind；'
            'complete 表示 clarification=null 且 resource_configuration_required=false。'
            '问题和选项应结合用户需求自然生成，不要生成输入或架构。'
            '保留或补全 request_summary，使其成为脱离聊天记录也能理解的应用需求摘要。'
            '只返回 DiscoveryDecision JSON。'
        )},
        {'role': 'user', 'content': json.dumps({
            **_stage_request_payload(state),
            'available_resources': state.resources,
            'invalid_previous_output': prior.model_dump(),
        }, ensure_ascii=False)},
    ]


def decision_from_output(output) -> ComposerDecision:
    decision = parse_structured_output(output, ComposerDecision, normalize=_normalize_composer_payload)
    return ComposerDecision.model_validate(_normalize_composer_payload(decision.model_dump()))


def patch_from_output(output) -> dict:
    """Parse only fields explicitly returned by a stage-local patch."""
    patch = parse_structured_output(output, ComposerPatch, normalize=_normalize_composer_payload)
    return patch.model_dump(exclude_unset=True, exclude_none=True)


def merge_composer_patch(existing: dict, patch: dict) -> dict:
    """Merge a partial stage response without allowing schema defaults to erase state."""
    result = json.loads(json.dumps(existing or {}, ensure_ascii=False))
    for key, value in (patch or {}).items():
        if key in {'intent', 'reply'}:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_composer_patch(result[key], value)
        else:
            result[key] = json.loads(json.dumps(value, ensure_ascii=False))
    return result


def merge_graph_patch(existing: dict, patch: dict) -> dict:
    """Merge a model repair without allowing omitted graph nodes to vanish."""
    result = merge_composer_patch(existing, patch)
    # A repair patch may update the app intro, but never replace it with a
    # note about what this repair changed (that belongs in ``reply``).
    for key in ('summary', 'title'):
        if summary_looks_like_change_note((patch or {}).get(key)):
            result[key] = (existing or {}).get(key, result.get(key))
    for key in ('agents', 'tasks'):
        if not isinstance(patch.get(key), list) or not isinstance(existing.get(key), list):
            continue
        old_by_id = {str(item.get('id')): item for item in existing[key]
                     if isinstance(item, dict) and item.get('id')}
        new_by_id = {str(item.get('id')): item for item in patch[key]
                     if isinstance(item, dict) and item.get('id')}
        merged = []
        for item in existing[key]:
            item_id = str(item.get('id') or '') if isinstance(item, dict) else ''
            merged.append(new_by_id.get(item_id, item) if item_id else item)
        merged.extend(item for item in patch[key]
                      if isinstance(item, dict) and str(item.get('id') or '') not in old_by_id)
        result[key] = merged
    return result


def _structure_edit_requested(message: str) -> bool:
    text = str(message or '').casefold()
    return any(term in text for term in (
        '增加', '新增', '添加', '删除', '移除', '减少', '合并', '拆分',
        '几个agent', '几个智能体', '多少个agent', '多少个智能体',
        '放入crew', '放到crew', '改成crew', '改为crew', '独立agent',
        'crew成员', '成员关系', '拓扑', '节点关系',
    ))


def _preserve_architecture_topology(generated: dict, confirmed: dict) -> dict:
    """Keep a confirmed graph while allowing a retry to repair its details."""
    result = json.loads(json.dumps(generated or {}, ensure_ascii=False))
    confirmed_agents = [item for item in confirmed.get('agents', []) or [] if isinstance(item, dict)]
    confirmed_tasks = [item for item in confirmed.get('tasks', []) or [] if isinstance(item, dict)]
    generated_agents = [item for item in result.get('agents', []) or [] if isinstance(item, dict)]
    generated_tasks = [item for item in result.get('tasks', []) or [] if isinstance(item, dict)]
    if not confirmed_agents or not confirmed_tasks:
        return result

    merged_agents = []
    generated_by_id = {str(item.get('id')): item for item in generated_agents if item.get('id')}
    for index, old in enumerate(confirmed_agents):
        candidate = generated_by_id.get(str(old.get('id'))) or (
            generated_agents[index] if index < len(generated_agents) else {}
        )
        merged = {**json.loads(json.dumps(old, ensure_ascii=False)), **json.loads(json.dumps(candidate, ensure_ascii=False))}
        for key in ('id', 'role', 'user_interaction'):
            if key in old:
                merged[key] = old[key]
        merged_agents.append(merged)

    def merge_task(old, candidate):
        merged = {**json.loads(json.dumps(old, ensure_ascii=False)), **json.loads(json.dumps(candidate or {}, ensure_ascii=False))}
        for key in ('id', 'name', 'node_type', 'agent_id', 'depends_on', 'crew_agent_ids',
                    'crew_process', 'crew_manager_agent_id'):
            if key in old:
                merged[key] = json.loads(json.dumps(old[key], ensure_ascii=False))
        old_nested = [item for item in old.get('crew_tasks', []) or [] if isinstance(item, dict)]
        new_nested = [item for item in (candidate or {}).get('crew_tasks', []) or [] if isinstance(item, dict)]
        if old_nested:
            new_by_id = {str(item.get('id')): item for item in new_nested if item.get('id')}
            merged['crew_tasks'] = [
                merge_task(item, new_by_id.get(str(item.get('id'))) or (new_nested[index] if index < len(new_nested) else {}))
                for index, item in enumerate(old_nested)
            ]
        return merged

    generated_by_id = {str(item.get('id')): item for item in generated_tasks if item.get('id')}
    result['agents'] = merged_agents
    result['tasks'] = [
        merge_task(old, generated_by_id.get(str(old.get('id'))) or
                   (generated_tasks[index] if index < len(generated_tasks) else {}))
        for index, old in enumerate(confirmed_tasks)
    ]
    return result


_INPUT_TYPE_ALIASES = {
    'string': 'text',
    'str': 'text',
    'text': 'text',
    'textarea': 'long_text',
    'multiline': 'long_text',
    'longtext': 'long_text',
    'integer': 'number',
    'int': 'number',
    'float': 'number',
    'decimal': 'number',
    'bool': 'boolean',
    'boolean': 'boolean',
    'object': 'json',
    'dict': 'json',
    'json_object': 'json',
    'array': 'json',
    'list': 'json',
    'document': 'file',
    'pdf': 'file',
    'docx': 'file',
    'upload': 'file',
}

def _is_ascii_letter(value: str) -> bool:
    return len(value) == 1 and (('a' <= value <= 'z') or ('A' <= value <= 'Z'))


def _is_ascii_digit(value: str) -> bool:
    return len(value) == 1 and '0' <= value <= '9'


def _ascii_snake(value: object) -> str:
    """Convert an ASCII portion to snake_case using character inspection."""
    text = str(value or '').strip()
    output: list[str] = []
    previous_ascii = ''
    pending_separator = False
    for char in text:
        if _is_ascii_letter(char):
            if (char.isupper() and previous_ascii and previous_ascii.islower()
                    and output and output[-1] != '_'):
                output.append('_')
            if pending_separator and output and output[-1] != '_':
                output.append('_')
            output.append(char.lower())
            pending_separator = False
            previous_ascii = char
        elif _is_ascii_digit(char):
            if pending_separator and output and output[-1] != '_':
                output.append('_')
            output.append(char)
            pending_separator = False
            previous_ascii = char
        elif char == '_':
            if output and output[-1] != '_':
                output.append('_')
            pending_separator = True
            previous_ascii = ''
        else:
            pending_separator = True
            previous_ascii = ''
    while output and output[-1] == '_':
        output.pop()
    return ''.join(output)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _variable_name(value: object, fallback: str = 'input') -> str:
    normalized = _ascii_snake(value)
    if not normalized:
        return fallback
    if _is_ascii_digit(normalized[0]):
        return f'{fallback}_{normalized}'
    return normalized


def _agent_backstory(role: object, goal: object) -> str:
    role_text = str(role or '专业执行智能体').strip()
    goal_text = str(goal or '完成分配任务').strip()
    return (
        f'你是一名{role_text}，具备与该职责相关的专业经验。'
        f'你围绕“{goal_text}”工作，遵循输入约束，保持判断可追溯，并只交付可验证的结果。'
    )


def _normalize_runtime_input(value: object, index: int) -> dict:
    item = dict(value) if isinstance(value, dict) else {'name': str(value)}
    raw_name = item.get('name') or item.get('label') or item.get('display_name')
    variable = item.get('variable') or item.get('key') or item.get('input_name')
    if not variable and isinstance(item.get('id'), str):
        variable = item['id']
    variable = _variable_name(variable, '') if variable else ''
    if not variable:
        variable = _variable_name(raw_name, f'input_{index + 1}')
    name = item.get('label') or item.get('display_name') or raw_name or variable
    raw_type = str(item.get('type') or item.get('input_type') or 'text').strip().lower()
    item.update({
        'name': str(name),
        'variable': variable,
        'type': _INPUT_TYPE_ALIASES.get(raw_type, raw_type if raw_type in {
            'text', 'long_text', 'file', 'image', 'number', 'boolean', 'json'
        } else 'text'),
        'required': item.get('required', False),
        'multiple': item.get('multiple', item.get('allow_multiple', False)),
        'description': item.get('description') or item.get('help') or '',
    })
    return item


def normalize_runtime_inputs(values: object) -> list[dict]:
    """Normalize input structure without inferring business meaning from labels."""
    result = []
    used: set[str] = set()
    for index, value in enumerate(_as_list(values)):
        item = _normalize_runtime_input(value, index)
        base = item['variable']
        variable = base
        suffix = 2
        while variable in used:
            variable = f'{base}_{suffix}'
            suffix += 1
        item['variable'] = variable
        used.add(variable)
        result.append(item)
    return result


def _normalize_agent(value: object, index: int) -> dict:
    item = dict(value) if isinstance(value, dict) else {'role': str(value)}
    role = item.get('role') or item.get('name') or item.get('title') or f'执行智能体 {index + 1}'
    agent_id = item.get('id') or item.get('agent_id') or item.get('key') or _variable_name(role, f'agent_{index + 1}')
    goal = item.get('goal') or item.get('purpose') or item.get('objective') or '完成分配任务'
    item.update({
        'id': str(agent_id),
        'role': str(role),
        'goal': goal,
        'backstory': item.get('backstory') or item.get('context') or _agent_backstory(role, goal),
        'responsibilities': item.get('responsibilities') or [goal],
        'skills': item.get('skills') or [],
        'plugins': item.get('plugins') or item.get('tools') or [],
        'knowledge_base_ids': item.get('knowledge_base_ids') or item.get('knowledge') or [],
        'tools': item.get('tools') or [],
    })
    return item


def _normalize_task(value: object, index: int) -> dict:
    item = dict(value) if isinstance(value, dict) else {'description': str(value)}
    task_id = item.get('id') or item.get('task_id') or item.get('key') or f'task_{index + 1}'
    name = item.get('name') or item.get('title') or item.get('label') or task_id
    description = item.get('description') or item.get('objective') or item.get('instructions') or ''
    expected_output = item.get('expected_output') or item.get('output') or item.get('deliverable') or '完成任务并返回可验证结果'
    depends_on = item.get('depends_on')
    if depends_on is None:
        depends_on = item.get('dependencies') or item.get('depends') or []
    item.update({
        'id': str(task_id),
        'name': str(name),
        'description': str(description),
        'expected_output': str(expected_output),
        'depends_on': _as_list(depends_on),
        'output_variables': _as_list(item.get('output_variables')),
        'dependency_variables': item.get('dependency_variables') or {},
        'crew_tasks': [
            _normalize_task(crew_task, child_index)
            for child_index, crew_task in enumerate(_as_list(item.get('crew_tasks')))
        ],
    })
    if str(item.get('output_mode') or '').lower() == 'object':
        item['output_mode'] = 'json'
    return item


def _normalize_composer_payload(payload: object) -> object:
    """Accept common legacy aliases emitted by local OpenAI-compatible models."""
    if not isinstance(payload, dict):
        return payload
    result = dict(payload)
    # Some OpenAI-compatible models occasionally emit the first RuntimeInput
    # object directly instead of the enclosing decision object. Treat that as
    # a design response during the narrow inputs stage; the API will still
    # validate the normalized input contract afterwards.
    if 'intent' not in result and ('name' in result or 'variable' in result) and (
        'input_type' in result or 'type' in result
    ):
        result = {'intent': 'design', 'inputs': [result], **result}
    result.setdefault('intent', 'design')
    if 'inputs' not in result:
        result['inputs'] = result.get('runtime_inputs') or result.get('runtimeInputs') or []
    if 'agents' not in result:
        result['agents'] = result.get('sub_agents') or result.get('agent_definitions') or []
    if 'tasks' not in result:
        result['tasks'] = result.get('workflow_tasks') or result.get('task_definitions') or []
    result['inputs'] = normalize_runtime_inputs(result.get('inputs'))
    result['agents'] = [
        _normalize_agent(item, index)
        for index, item in enumerate(_as_list(result.get('agents')))
    ]
    result['tasks'] = [
        _normalize_task(item, index)
        for index, item in enumerate(_as_list(result.get('tasks')))
    ]
    return result


def _ensure_agent_introductions(payload: dict) -> dict:
    result = dict(payload or {})
    if result.get('intent') == 'conversation':
        return result
    agents = []
    for index, item in enumerate(result.get('agents') or []):
        normalized = _normalize_agent(item, index)
        agents.append(normalized)
    if agents:
        result['agents'] = agents
    summary = str(result.get('summary') or '').strip()
    if not summary:
        title = str(result.get('title') or '').strip()
        tasks = result.get('tasks') or []
        task_names = [str(item.get('name') or '').strip() for item in tasks if isinstance(item, dict)]
        task_names = [item for item in task_names if item]
        subject = title or (task_names[0] if task_names else '用户需求')
        result['summary'] = f'面向{subject}提供可运行的 CrewAI 智能应用，按已确认输入完成处理并交付可验证结果。'
    return result


def _normalize_clarification_patches(proposal: dict) -> dict:
    """Expose model-produced patch JSON as the object expected by Studio cards."""
    clarification = proposal.get('clarification')
    if not isinstance(clarification, dict):
        return proposal
    options = clarification.get('options')
    if not isinstance(options, list):
        return proposal
    normalized = []
    for option in options:
        if not isinstance(option, dict):
            continue
        item = dict(option)
        patch = item.get('patch', '{}')
        if isinstance(patch, str):
            try:
                patch = json.loads(patch)
            except json.JSONDecodeError:
                patch = {}
        item['patch'] = patch if isinstance(patch, dict) else {}
        normalized.append(item)
    result = dict(proposal)
    result['clarification'] = {**clarification, 'options': normalized}
    return result


def input_agent(state: ComposerState) -> Agent:
    return Agent(
        role='运行输入契约设计师',
        goal='只定义发布后用户需要提交的强类型输入，并保证 message 变量契约可执行',
        backstory='你专注于把业务需求转换成最小、明确、可校验的运行输入。你不设计 Agent、Task 或架构，也不把后续阶段的字段提前带入输入卡片。',
        llm=_model_llm(state.model), max_iter=2, max_retry_limit=2,
        reasoning=False, allow_delegation=False, verbose=False,
    )


def generation_agent(state: ComposerState) -> Agent:
    return Agent(
        role='CrewAI 可运行定义生成师',
        goal='把已确认的架构转换为完整且可执行的 Agent 与 Task 定义',
        backstory='你负责最终落地检查，熟悉 CrewAI 的变量传递、Agent/Task 绑定和资源边界。你只修正当前生成清单，不重新讨论用户已经确认的前置选择。',
        llm=_model_llm(state.model), max_iter=3, max_retry_limit=2,
        reasoning=False, allow_delegation=False, verbose=False,
    )


def discovery_agent(state: ComposerState) -> Agent:
    """Low-latency agent for the small preflight decision contract."""
    return Agent(
        role='编排会话入口与前置确认专员',
        goal='在多轮对话中准确区分陪聊和应用编排需求；进入编排后只确认一个尚缺的前置条件',
        backstory='你负责会话入口路由。没有明确应用需求时自然回应；出现明确需求时提炼独立需求摘要，再依次确认交互方式、资源使用和编排类型。你不生成架构。',
        llm=_model_llm(state.model), max_iter=1, max_retry_limit=1,
        reasoning=False, allow_delegation=False, verbose=False,
    )


def generation_review_agent(state: ComposerState) -> Agent:
    return Agent(
        role='CrewAI 生成结果审查师',
        goal='只审查已经确认架构的最终生成定义，发现变量、资源、节点配置和交付契约问题',
        backstory='你负责 generation 阶段的最终可运行性检查。交互意图、输入确认和 Crew/Flow 选择已经完成，不重新判断用户是否要编排，只沿真实运行数据流检查最终定义。',
        llm=_model_llm(state.model),
        max_iter=2,
        max_retry_limit=1,
        reasoning=False,
        allow_delegation=False,
        verbose=False,
    )


def architecture_builder_agent(state: ComposerState) -> Agent:
    return Agent(
        role='CrewAI 编排架构设计师',
        goal='根据已锁定的输入和能力，设计清晰的数据流、Agent 职责和任务依赖',
        backstory='你只负责架构阶段的方案设计，擅长把业务目标拆成最小可运行的 Agent/Task 图，并为每个节点写清角色、目标、背景、职责和可验收输出。',
        llm=_model_llm(state.model), max_iter=3, max_retry_limit=2,
        reasoning=False, allow_delegation=False, verbose=False,
    )


def needs_generation_review(decision: ComposerDecision) -> bool:
    """Every non-empty final graph receives one generation-stage review."""
    return bool(decision.agents and decision.tasks)


def _normalize_bare_runtime_references(decision: ComposerDecision) -> None:
    """Repair exact runtime references before contract validation.

    Models occasionally write ``agent_1.object`` as prose while intending a
    runtime placeholder.  The renderer only recognizes ``{agent_1.object}``,
    so normalize references against the authoritative graph before validation.
    This is deliberately limited to fixed platform output fields; arbitrary
    business keys remain ordinary text and must be described inside ``object``.
    """
    import re

    names = {item.variable for item in decision.inputs if item.variable}
    node_ids = {
        str(task.id): task
        for task in decision.tasks
        if str(task.id or '').strip()
    }
    output_fields = ('object', 'file', 'text', 'output', 'route')

    def visit(task):
        for field in ('description', 'expected_output'):
            value = getattr(task, field, '')
            if not isinstance(value, str):
                continue
            # In a JSON-object contract a bare input name is a field name
            # (doc_type), not a forgotten placeholder; do not brace it.
            json_fields = field == 'expected_output' and expects_object(value)
            for name in ([] if json_fields else names):
                value = re.sub(
                    rf'(?<![{{A-Za-z0-9_]){re.escape(name)}(?![}}A-Za-z0-9_])',
                    '{' + name + '}', value,
                )
            for node_id in node_ids:
                fields = '|'.join(output_fields)
                value = re.sub(
                    rf'(?<![{{A-Za-z0-9_]){re.escape(node_id)}\.(?:{fields})(?![}}A-Za-z0-9_])',
                    lambda match: '{' + match.group(0) + '}',
                    value,
                )
            setattr(task, field, value)
        for nested in getattr(task, 'crew_tasks', []) or []:
            visit(nested)

    if names or node_ids:
        for task in decision.tasks:
            visit(task)


def _normalize_file_alias_references(decision: ComposerDecision) -> None:
    """Keep historical reference-file names out of regenerated prompts."""
    if not any(item.variable == 'files' for item in decision.inputs):
        return
    import re
    alias_pattern = re.compile(
        r'\{([A-Za-z_][A-Za-z0-9_]*(?:file|files|attachment|document|reference|material|template|upload|excel|csv|pdf)[A-Za-z0-9_]*)\}',
        re.I,
    )
    for task in decision.tasks:
        for field in ('description', 'expected_output'):
            value = getattr(task, field, '')
            if isinstance(value, str):
                setattr(task, field, alias_pattern.sub('{files}', value))


def _normalize_reserved_runtime_tokens(decision: ComposerDecision) -> None:
    """Prevent environment-variable syntax from entering Flow placeholders."""
    def visit(task):
        if not isinstance(task, ProposedTask) and not isinstance(task, ProposedCrewTask):
            return
        for field in ('description', 'expected_output'):
            value = getattr(task, field, '')
            if isinstance(value, str):
                value = value.replace('{XUANSHU_WORKSPACE}', '`XUANSHU_WORKSPACE`')
                value = value.replace('{XUANSHU_WORK_ROOT}', '`XUANSHU_WORK_ROOT`')
                setattr(task, field, value)
        for nested in getattr(task, 'crew_tasks', []) or []:
            visit(nested)

    for task in decision.tasks:
        visit(task)


def generation_contract_findings(decision: ComposerDecision, resources: dict | None = None,
                                 *, stage: str = 'generation') -> list[str]:
    """Run the publish-time variable checks against Composer-shaped inputs."""
    _normalize_reserved_runtime_tokens(decision)
    _normalize_file_alias_references(decision)
    _normalize_bare_runtime_references(decision)
    for task in decision.tasks:
        for item in [task, *(getattr(task, 'crew_tasks', []) or [])]:
            if hasattr(item, 'expected_output'):
                item.expected_output = fix_json_field_placeholders(item.expected_output)
    definition = decision.model_dump()
    ensure_fixed_output_contracts(definition)
    normalize_deterministic_dependencies(definition)
    task_prompts = ' '.join(
        str(task.get(field) or '')
        for task in definition.get('tasks', [])
        for field in ('description', 'expected_output')
    )
    input_names = {
        str(item.get('variable') or item.get('name') or '')
        for item in definition.get('inputs', [])
    }
    if 'message' in input_names and '{message}' not in task_prompts and definition.get('tasks'):
        first = definition['tasks'][0]
        first['description'] = '根据用户本轮需求 {message} 完成以下工作：\n' + str(
            first.get('description') or ''
        )
    if stage == 'architecture':
        # Architecture confirms topology only.  Node switches, variable
        # references and field details are checked by the generation review.
        return architecture_contract_errors(definition)
    force_object_code_contract(definition)
    return list(dict.fromkeys([
        *capability_binding_findings(definition),
        *variable_contract_errors(definition),
        *deterministic_binding_errors(definition, resources),
        *generation_code_node_errors(definition),
        *design_quality_errors(definition),
    ]))


def capability_binding_findings(definition: dict) -> list[str]:
    """Require selected capabilities to be used without choosing their Agent.

    The capability card records application requirements. Agent ownership is a
    semantic architecture decision and must come from the Composer output;
    this gate only detects an omitted required binding.
    """
    agents = definition.get('agents', []) or []
    agent_skills = {str(value) for agent in agents for value in agent.get('skills', []) or []}
    agent_plugins = {str(value) for agent in agents for value in agent.get('plugins', []) or []}
    agent_knowledge = {str(value) for agent in agents for value in agent.get('knowledge_base_ids', []) or []}
    selected_tools = {str(value) for value in definition.get('tools', []) or []}
    selected_tools.update(
        str(task.get('tool_id')) for task in definition.get('tasks', []) or []
        if isinstance(task, dict) and task.get('tool_id')
    )
    used = {'skill': agent_skills, 'tool': agent_plugins | selected_tools, 'knowledge': agent_knowledge}
    findings = []
    for requirement in definition.get('capability_requirements', []) or []:
        if requirement.get('required', True) is False:
            continue
        resource_type = str(requirement.get('resource_type') or '')
        for value in requirement.get('selected_ids', []) or []:
            resource_id = str(value)
            if resource_id not in used.get(resource_type, set()):
                findings.append(
                    f'已确认的{resource_type}资源 {resource_id} 尚未绑定到任何实际执行 Agent 或节点；'
                    '请按任务职责绑定给真正使用它的对象，不要自动复制到信息收集 Agent。'
                )
    return findings


def generation_correction_prompt(state: ComposerState, proposal: ComposerDecision,
                                 review: ArchitectureReview) -> list[dict]:
    return [
        {'role': 'system', 'content': (
            '你是玄枢平台的 generation 修正 Agent。根据最终生成定义和审查结果，'
            '只修正审查指出的可运行性问题，保留用户目标、已确认输入和合理设计。必须使用平台固定输出契约，不得创建或恢复自定义 output_variables。'
            'Flow 的 Agent/Crew/Router/Code/Tool 节点统一按可达上游变量检查；Code/Tool 参数必须用 input_bindings 绑定节点ID和固定字段，Crew 内部任务的依赖仍使用 CrewAI context。'
            '运行输入必须写成 {variable}，上游引用必须写成 {node_id.field}，不能只写变量名或裸的节点ID.字段。expected_output 中 JSON 对象的字段名是固定英文名，例如字段包括 doc_type、issuer、title；字段名不加大括号。取值说明只能引用已声明输入或可达上游，不能把当前节点将要输出的 doc_type 写成 {doc_type}；Crew 内部 Task 只能依赖同一 Crew 内的 task_1/task_2，不得把外部 Flow 节点 ID 写入内部 depends_on。'
            'XUANSHU_WORKSPACE 是 Python 环境变量名，必须写成 `XUANSHU_WORKSPACE`，绝对不要写成 {XUANSHU_WORKSPACE}；只有平台输入和节点输出才使用大括号。不得只改说明文字或返回 summary。'
            '只返回需要更新的 ComposerPatch；没有需要修改的字段时只返回 intent=design，不输出解释。'
        )},
        {'role': 'user', 'content': json.dumps({
            **_stage_request_payload(state),
            'selected_resource_details': state.resources.get('selected_resource_details', {}),
            'proposal': proposal.model_dump(),
            'review_findings': review.model_dump(),
        }, ensure_ascii=False)},
    ]


def generation_review_prompt(state: ComposerState, analysis: ComposerDecision) -> list[dict]:
    # Keep the model-facing review contract compact. The previous prompt
    # repeated product background and made the environment-variable syntax
    # easy to confuse with platform placeholders.
    system = '''你是 CrewAI generation 最终审查 Agent。只审查当前完整编排，不重新讨论用户意图或编排类型。请从整体交付路径审查完整的端到端数据流：运行输入 -> 节点依赖 -> Agent/Crew/Code/Tool -> 最终交付。

关键审查规则：
1. 输入可达：输入变量使用 `{variable}`；Flow 上游引用必须是 `{node_id.field}`；`message`、`files`、`conversation_history` 是平台内置变量。`XUANSHU_WORKSPACE` 是 Python 环境变量，必须写成 `XUANSHU_WORKSPACE`，禁止写成 `{XUANSHU_WORKSPACE}`。
2. 任务数据流：检查依赖合法性。Flow 节点只能引用可达上游；Crew 内部 Task 只能依赖同一 Crew 内的 `task_1/task_2`，不得把外部 Flow 节点写入内部 `depends_on`。顺序 Crew 的每个 Task 必须绑定 Agent，且从第二个内部任务开始每个任务的 depends_on 必须包含前一个内部任务的 ID（如 task_2 depends_on ["task_1"]，task_3 depends_on ["task_2"]），形成完整的顺序执行链；层级 Crew 的每个 Task 必须不绑定 Agent并由 manager 分配。Code/Tool 必须用 `input_bindings`。
3. 结构规模：只有职责、权限、模型、工具或输出契约有实质差异才拆分 Agent；Crew 负责协作，Flow 负责确定性节点与分支。
4. 资源真实性：Skill/Tool/Knowledge 必须绑定到真实可用资源；每个已确认的必需资源必须至少被一个实际执行 Agent 或节点使用；只负责澄清、收集或询问的 Agent 不应因为能力卡自动获得资源；脚本或文件生成 Skill 的 Agent 必须开启 `allow_code_execution`；Tool 只放 `plugins`。
5. 输出契约：只能使用固定 `object/file/text`（Code 为 `output/file/text`）；下游引用 object 时生产任务使用 JSON 模式并说明业务字段。文件必须来自实际执行回执。
6. 交付一致：每个节点的职责、expected_output、资源和依赖必须一致；不要因为业务对象字段创建自定义 output_variables。
7. 输出开关：expected_output 要求 JSON/结构化对象、或下游读取其 object 的任务必须 output_mode=json 且不开 markdown；JSON 字段名不能写成 `{变量}` 占位符。
8. Crew 数据：依赖上游的 Crew 节点，其内部第一个任务必须引用上游输出（如 `{agent_1.object}`），否则内部 Agent 拿不到数据。
9. 代码执行：绑定含代码的 Skill、或任务要求生成/执行代码、导出 Word/Excel/PDF 等文件的 Agent 必须 allow_code_execution=true；删除代码节点不能丢掉原需求的文件交付。
10. 应用简介：summary 必须是应用介绍，不能是本次修改说明。

只返回 ArchitectureReview JSON。无问题返回 `approved=true, findings=[]`；有问题逐条指出字段、原因和修复方式，不重写整份方案。'''
    payload = {
        'requested_kind': state.kind,
        **_stage_request_payload(state),
        'available_resources': {
            key: value for key, value in state.resources.items()
            if key in {'skills', 'tools', 'knowledge', 'selected_resource_details'}
        },
        'analysis_draft': analysis.model_dump(),
    }
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]


@persist(NullFlowPersistence())
class ComposerFlow(Flow[ComposerState]):
    @start()
    def analyze_intent(self):
        if not self.state.model.get('model'):
            raise RuntimeError('编排意图识别需要工作空间默认模型，请先添加并设置默认模型')
        if self.state.review_policy == 'review_only':
            decision = decision_from_existing(self.state.existing)
            self.state.analysis = decision.model_dump()
            return self.state.analysis
        if self.state.stage == 'discovery':
            output = kickoff_structured(
                discovery_agent(self.state), discovery_prompt(self.state),
                DiscoveryDecision, self.state.model, label='discovery',
            )
            narrowed = parse_structured_output(
                output, DiscoveryDecision, normalize=_normalize_composer_payload,
            )
            existing_intent = bool(
                self.state.existing.get('orchestration_intent_confirmed')
            )
            existing_purpose = bool(
                self.state.existing.get('application_purpose_known')
                and self.state.existing.get('original_request')
            )
            intent_confirmed = bool(
                narrowed.orchestration_intent_confirmed or existing_intent
            )
            purpose_known = bool(
                existing_purpose
                or (narrowed.application_purpose_known and narrowed.request_summary.strip())
            )
            if narrowed.intent == 'conversation' or not (intent_confirmed and purpose_known):
                narrowed.clarification = None
                narrowed.resource_configuration_required = False
                narrowed.capability_requirements = []
                narrowed.tools = []
                narrowed.request_summary = ''
                if intent_confirmed and not purpose_known:
                    fallback_reply = '你希望这个智能体具体帮你完成什么工作？'
                else:
                    fallback_reply = '我在。你想聊点什么？'
                self.state.analysis = {
                    'intent': 'conversation',
                    'reply': narrowed.reply or fallback_reply,
                }
                return self.state.analysis
            narrowed.orchestration_intent_confirmed = True
            narrowed.application_purpose_known = True
            narrowed.request_summary = (
                narrowed.request_summary.strip()
                or str(self.state.existing.get('original_request') or '').strip()
            )
            confirmed_request_summary = narrowed.request_summary

            def discovery_status(value: DiscoveryDecision) -> tuple[str, bool, bool, bool]:
                existing = self.state.existing
                interaction_explicit = bool(
                    value.interaction_mode_explicit or confirmations.interaction_confirmed(existing)
                )
                resources_explicit = bool(
                    value.resource_selection_explicit or confirmations.resources_confirmed(existing)
                )
                kind_explicit = bool(
                    value.kind_explicit or confirmations.kind_confirmed(existing, self.state.kind)
                )
                expected = (
                    'interaction_mode' if not interaction_explicit else
                    'resources' if not resources_explicit else
                    'orchestration_kind' if not kind_explicit else 'complete'
                )
                return expected, interaction_explicit, resources_explicit, kind_explicit

            def follows_protocol(value: DiscoveryDecision, expected: str) -> bool:
                clarification_id = value.clarification.id if value.clarification else None
                if expected == 'interaction_mode':
                    return clarification_id == 'interaction_mode'
                if expected == 'resources':
                    return clarification_id is None and value.resource_configuration_required
                if expected == 'orchestration_kind':
                    return clarification_id == 'orchestration_kind'
                return clarification_id is None and not value.resource_configuration_required

            expected, interaction_explicit, resources_explicit, kind_explicit = discovery_status(narrowed)
            if not follows_protocol(narrowed, expected):
                correction = kickoff_structured(
                    discovery_agent(self.state), discovery_correction_prompt(self.state, narrowed, expected),
                    DiscoveryDecision, self.state.model, label='discovery_correction',
                )
                narrowed = parse_structured_output(
                    correction, DiscoveryDecision, normalize=_normalize_composer_payload,
                )
                narrowed.request_summary = (
                    narrowed.request_summary.strip()
                    or confirmed_request_summary
                )
                narrowed.orchestration_intent_confirmed = True
                narrowed.application_purpose_known = True
                expected, interaction_explicit, resources_explicit, kind_explicit = discovery_status(narrowed)
                if not follows_protocol(narrowed, expected):
                    # One correction is the budget.  Stop here and let the
                    # user decide whether to retry, instead of spending more
                    # model calls on the same turn.
                    raise RuntimeError(f'编排前置确认未按协议返回 {expected} 步骤')

            self.state.discovery_interaction_explicit = interaction_explicit
            self.state.discovery_resource_explicit = resources_explicit
            self.state.discovery_kind_explicit = kind_explicit
            if expected == 'interaction_mode':
                narrowed.resource_configuration_required = False
                narrowed.kind = None
                narrowed.capability_requirements = []
            elif expected == 'resources':
                narrowed.kind = None
            else:
                narrowed.resource_configuration_required = False
            self.state.discovery_resource_configuration = bool(narrowed.resource_configuration_required)
            discovered_kind = narrowed.kind
            if not discovered_kind and self.state.kind in {'crew', 'flow'}:
                discovered_kind = self.state.kind
            discovery_payload = {
                key: value for key, value in narrowed.model_dump().items()
                if value is not None
            }
            decision = ComposerDecision.model_validate({
                **discovery_payload,
                'kind': discovered_kind or 'crew',
                'inputs': [], 'agents': [], 'tasks': [],
            })
            self.state.analysis = decision.model_dump()
            return self.state.analysis
        if self.state.stage == 'inputs':
            response_model = InputComposerDecision
        elif self.state.stage == 'architecture':
            response_model = ArchitectureStageDecision
        else:
            response_model = GenerationStageDecision
        if response_model is InputComposerDecision:
            output = kickoff_structured(
                input_agent(self.state), input_prompt(self.state),
                response_model, self.state.model, label='inputs',
            )
        else:
            output = kickoff_structured(
                generation_agent(self.state) if self.state.stage == 'generation'
                else architecture_builder_agent(self.state), composer_prompt(self.state),
                response_model, self.state.model, label=f'{self.state.stage}_analysis',
            )
        if response_model is InputComposerDecision:
            narrowed = parse_structured_output(
                output, InputComposerDecision, normalize=_normalize_composer_payload,
            )
            decision = ComposerDecision.model_validate(narrowed.model_dump())
        else:
            stage_result = parse_structured_output(
                output, response_model, normalize=_normalize_composer_payload,
            )
            # Complete stage models have defaults for omitted fields. Keep
            # only fields the model actually returned so a partial revision
            # cannot erase a previously confirmed graph with empty arrays.
            stage_fields = stage_result.model_dump(
                exclude_unset=True,
                exclude_none=True,
            )
            patch = _ensure_agent_introductions(stage_fields)
            merged = merge_composer_patch(decision_from_existing(self.state.existing).model_dump(), patch)
            merged['intent'] = patch.get('intent', 'design')
            merged['reply'] = patch.get('reply') or ''
            decision = ComposerDecision.model_validate(_normalize_composer_payload(merged))
            # A compatible endpoint may omit list fields even though the
            # Pydantic schema supplies empty defaults. Retry only this invalid
            # same-stage response; valid designs still use one Agent kickoff.
            if (decision.intent == 'design' and self.state.stage in {'architecture', 'generation'}
                    and (not decision.agents or not decision.tasks)):
                repair = kickoff_structured(
                    generation_agent(self.state) if self.state.stage == 'generation'
                    else architecture_builder_agent(self.state),
                    graph_completion_prompt(self.state, decision),
                    ComposerPatch, self.state.model, label=f'{self.state.stage}_graph_completion',
                )
                repair_patch = patch_from_output(repair)
                repaired = merge_graph_patch(decision.model_dump(), repair_patch)
                repaired['intent'] = repair_patch.get('intent', 'design')
                decision = ComposerDecision.model_validate(_normalize_composer_payload(repaired))
            if decision.intent == 'design' and self.state.stage == 'architecture':
                findings = [
                    *_enforce_locked_process(decision, self.state),
                    *generation_contract_findings(decision, self.state.resources, stage='architecture'),
                ]
                for attempt in range(2):
                    if not findings:
                        break
                    _emit_composer_progress(
                        'architecture_contract_correction',
                        '正在完善架构中的任务数据传递…',
                    )
                    correction = kickoff_structured(
                        architecture_builder_agent(self.state),
                        architecture_contract_correction_prompt(self.state, decision, findings),
                        ComposerPatch, self.state.model,
                        label=('architecture_contract_correction' if attempt == 0
                               else 'architecture_contract_correction_retry'),
                    )
                    correction_patch = patch_from_output(correction)
                    corrected = merge_graph_patch(decision.model_dump(), correction_patch)
                    corrected['intent'] = correction_patch.get('intent', 'design')
                    decision = ComposerDecision.model_validate(
                        _normalize_composer_payload(corrected)
                    )
                    findings = [
                        *_enforce_locked_process(decision, self.state),
                        *generation_contract_findings(decision, self.state.resources, stage='architecture'),
                    ]
        self.state.analysis = decision.model_dump()
        return self.state.analysis

    @listen(analyze_intent)
    def review_architecture(self, decision_data):
        decision = ComposerDecision.model_validate(decision_data)
        decision = _normalize_generation_contract(decision, self.state)
        if decision.intent == 'conversation':
            self.state.result = decision.model_dump()
            return self.state.result
        should_review = (
            self.state.stage == 'generation'
            and self.state.review_policy in {'always', 'review_only'}
            and needs_generation_review(decision)
        )
        if not should_review:
            self.state.analysis = decision.model_dump()
            return self.state.analysis
        _emit_composer_progress(
            'generation_review',
            '正在审查，请耐心等待…',
        )
        def run_review(candidate: ComposerDecision, label: str):
            output = kickoff_structured(
                generation_review_agent(self.state), generation_review_prompt(self.state, candidate),
                ArchitectureReview, self.state.model, label=label,
            )
            result = parse_structured_output(output, ArchitectureReview)
            problems = list(dict.fromkeys([
                *generation_contract_findings(candidate, self.state.resources),
                *result.findings,
            ]))
            problems = _filter_generation_findings(candidate, problems)
            if not result.approved and not result.findings:
                problems.append('审查未通过，审查 Agent 未给出具体原因')
            return result, list(dict.fromkeys(problems))

        reviewed, findings = run_review(decision, 'generation_review')
        if not findings:
            self.state.analysis = decision.model_dump()
            return self.state.analysis
        review_findings = list(reviewed.findings)
        corrected = decision
        # Two LLM review rounds are the complete semantic repair loop. The
        # API performs only one final hard-contract validation after this; it
        # does not invoke a third Composer repair path.
        for attempt in range(2):
            _emit_composer_progress(
                'generation_correction',
                '正在根据审查结果修正编排，这可能需要几分钟…',
            )
            correction_review = ArchitectureReview(approved=False, findings=findings)
            correction = kickoff_structured(
                generation_agent(self.state),
                generation_correction_prompt(self.state, corrected, correction_review),
                ComposerPatch, self.state.model,
                label='generation_correction' if attempt == 0 else 'generation_correction_retry',
            )
            correction_patch = patch_from_output(correction)
            corrected = ComposerDecision.model_validate(_normalize_composer_payload(
                merge_graph_patch(corrected.model_dump(), correction_patch)
            ))
            corrected = _normalize_generation_contract(corrected, self.state)
            corrected.intent = 'design'
            if self.state.kind in {'crew', 'flow'}:
                corrected.kind = self.state.kind
            if attempt == 1:
                findings = generation_contract_findings(corrected, self.state.resources)
                break
            recheck, findings = run_review(corrected, 'generation_recheck')
            if not findings:
                break
        # Two repair rounds are the budget.  Anything still unresolved, hard
        # contract error or semantic review finding, stops the turn and is
        # reported to the user, who decides whether to retry.
        if findings:
            raise RuntimeError('编排审查修正后仍存在未解决问题：' + '；'.join(findings))
        analysis = corrected.model_dump()
        if review_findings:
            analysis['review_findings'] = review_findings
            analysis['review_findings_resolved'] = True
        self.state.analysis = analysis
        return self.state.analysis

    @listen(review_architecture)
    def merge_proposal(self, decision_data):
        decision = ComposerDecision.model_validate(decision_data)
        if decision.intent == 'conversation':
            return self.state.result
        if self.state.kind in {'crew', 'flow'}:
            decision.kind = self.state.kind
        decision_fields = decision.model_dump(exclude={'intent', 'reply'})
        if decision_fields.get('manager_agent_id') is None:
            # An absent manager must not erase one confirmed earlier.
            decision_fields.pop('manager_agent_id')
        proposal = {
            **self.state.existing,
            **decision_fields,
            'intent': 'design',
            'request': self.state.request,
        }
        if self.state.stage == 'discovery' and decision.request_summary.strip():
            proposal['original_request'] = decision.request_summary.strip()
        for key in ('orchestration_intent_confirmed', 'application_purpose_known'):
            if self.state.existing.get(key):
                proposal[key] = True
        # Structured-output defaults are empty lists. During architecture or
        # generation, an omitted graph must not erase a concrete graph already
        # present in the persisted proposal. Input-stage responses intentionally
        # remain graph-free.
        if self.state.stage in {'architecture', 'generation'}:
            for key in ('agents', 'tasks', 'tools', 'capability_requirements'):
                if not proposal.get(key) and self.state.existing.get(key):
                    proposal[key] = json.loads(json.dumps(self.state.existing[key], ensure_ascii=False))
        locked_interaction = confirmations.interaction_confirmed(self.state.existing)
        locked_mode = confirmations.locked_interaction_mode(self.state.existing)
        if locked_mode:
            confirmations.mark_interaction(proposal, locked_mode)
        if self.state.stage != 'discovery' and proposal.get('interaction_mode') == 'multi_turn':
            proposal['inputs'] = normalize_runtime_inputs(proposal.get('inputs', []))
            proposal['inputs'] = conversational_inputs(proposal['inputs'])
        if self.state.stage == 'discovery':
            proposal['kind_preselected'] = self.state.discovery_kind_explicit
            proposal['interaction_mode_preselected'] = self.state.discovery_interaction_explicit
            proposal['capability_card'] = self.state.discovery_resource_configuration
            proposal['resource_selection_confirmed'] = bool(
                self.state.discovery_resource_explicit
                and not self.state.discovery_resource_configuration
            )
        elif self.state.stage == 'inputs':
            # Preflight choices are already user-confirmed and must not be
            # replaced by defaults from the narrow input-contract response.
            if self.state.existing.get('kind_preselected'):
                for key in ('kind', 'recommended_kind', 'kind_preselected'):
                    if key in self.state.existing:
                        proposal[key] = self.state.existing[key]
            if locked_interaction:
                proposal['interaction_mode'] = self.state.existing.get('interaction_mode', 'single_run')
                proposal['interaction_mode_preselected'] = True
            for key in ('tools', 'capability_requirements'):
                if key in self.state.existing:
                    proposal[key] = self.state.existing[key]
            # Input confirmation is deliberately graph-free. If a caller is
            # replaying an older session that already has a graph, keep that
            # graph instead of allowing InputComposerDecision defaults to
            # erase it; normal Studio revisions route generated graphs to the
            # generation stage.
            for key in ('agents', 'tasks'):
                if self.state.existing.get(key):
                    proposal[key] = json.loads(json.dumps(self.state.existing[key], ensure_ascii=False))
        # Review metadata is not part of ComposerDecision, so re-validating
        # the analysis above dropped it.  Carry it onto the proposal.
        for key in ('review_findings', 'review_findings_resolved'):
            if key in (decision_data or {}):
                proposal[key] = decision_data[key]
        self.state.result = _normalize_clarification_patches(proposal)
        return self.state.result


def _model_llm(profile: dict) -> LLM:
    return profile_llm(profile)


def decision_from_existing(existing: dict) -> ComposerDecision:
    """Convert a persisted Studio proposal back to the Composer response schema."""
    inputs = [
        {
            'name': item.get('label') or item.get('name') or '输入',
            # RuntimeInput uses name as the display label and variable as the
            # machine key; persisted StudioInput uses name as the machine key.
            'variable': item.get('variable') or item.get('name') or 'input',
            'type': item.get('input_type') or item.get('type') or 'text',
            'required': item.get('required', False),
            'multiple': item.get('multiple', False),
            'description': item.get('description', ''),
        }
        for item in existing.get('inputs', [])
    ]
    agents = [
        {
            **item,
            'goal': item.get('goal') or item.get('purpose') or '完成分配任务',
        }
        for item in existing.get('agents', [])
    ]
    tasks = [
        {
            **item,
            'description': item.get('description') or item.get('objective') or '',
        }
        for item in existing.get('tasks', [])
    ]
    return ComposerDecision.model_validate({
        'intent': 'design',
        'orchestration_intent_confirmed': existing.get('orchestration_intent_confirmed', False),
        'application_purpose_known': existing.get('application_purpose_known', False),
        'title': existing.get('title', '未命名智能体'),
        'kind': existing.get('recommended_kind') or existing.get('kind') or 'crew',
        'summary': existing.get('summary', ''),
        'interaction_mode': existing.get('interaction_mode', 'single_run'),
        'inputs': inputs,
        'process': existing.get('recommended_process')
                   if existing.get('recommended_process') in {'sequential', 'hierarchical'}
                   else existing.get('process', 'sequential'),
        'manager_agent_id': existing.get('manager_agent_id'),
        'memory': existing.get('memory', False),
        'planning': existing.get('planning', False),
        'agents': agents,
        'tasks': tasks,
        'tools': existing.get('tools', []),
        'capability_requirements': existing.get('capability_requirements', []),
        'clarification': existing.get('clarification'),
    })


def _memory_summary(result: dict) -> str:
    inputs = '、'.join(f"{item.get('name')}({item.get('variable')})" for item in result.get('inputs', [])) or '无额外输入'
    agents = '、'.join(item.get('role', '') for item in result.get('agents', [])) or '无子智能体'
    tools = '、'.join(result.get('tools', [])) or '无导入工具'
    return (
        f"用户确认的应用设计偏好：应用类型 {result.get('kind', 'crew')}，运行方式 {result.get('process', 'sequential')}；"
        f"运行输入：{inputs}；子智能体：{agents}；工具：{tools}；"
        f"长期记忆 {'开启' if result.get('memory') else '关闭'}，规划 {'开启' if result.get('planning') else '关闭'}。"
    )


def run_composer(request: str, stage: str, kind: str, existing: dict, model: dict,
                 resources: dict | None = None, *, user_id: int | None = None,
                 workspace_id: int | None = None, orchestration_id: str | None = None,
                 review_policy: Literal['always', 'never', 'on_kind_change', 'review_only'] = 'always',
                 existing_kind: str | None = None,
                 history: list[dict] | None = None,
                 progress_callback: ComposerProgressCallback | None = None) -> dict:
    memory = None
    memories = []
    if (stage in {'architecture', 'generation'} and user_id is not None
            and workspace_id is not None and model.get('model')):
        memory = persistent_memory(composer_dir(user_id) / 'memory', _model_llm(model), f'/workspace/{workspace_id}')
        matches = memory.recall(
            request,
            scope='/preferences',
            categories=['composer-preference'],
            limit=5,
            depth='shallow',
            source=str(user_id),
        )
        memories = [match.record.content for match in matches]
    flow = ComposerFlow(persistence=RedisFlowPersistence()) if orchestration_id else ComposerFlow()
    progress_token = _composer_progress_callback.set(progress_callback)
    try:
        flow.kickoff(inputs={
            **({'id': orchestration_id} if orchestration_id else {}),
            'request': request,
            'stage': stage,
            'kind': kind,
            'existing': existing,
            'model': model,
            'resources': resources or {},
            'history': history or [],
            'memories': memories,
            'review_policy': review_policy,
            'existing_kind': existing_kind or '',
        })
        result = flow.state.result
        if memory and result.get('intent') == 'design' and stage in {'architecture', 'generation'}:
            try:
                memory.remember(
                    _memory_summary(result),
                    scope='/preferences',
                    categories=['composer-preference'],
                    metadata={'workspace_id': workspace_id, 'stage': stage},
                    importance=.75,
                    source=str(user_id),
                    private=True,
                )
            except Exception:
                logging.exception('failed to persist non-critical Composer memory')
        return result
    finally:
        _composer_progress_callback.reset(progress_token)
        if memory:
            try:
                memory.close()
            except Exception:
                logging.exception('failed to close Composer memory')
