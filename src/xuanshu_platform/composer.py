import json
from collections.abc import Callable
from contextvars import ContextVar
from typing import Literal

from crewai import Agent, LLM
from crewai.flow.flow import Flow, listen, start
from crewai.flow.persistence import persist
from pydantic import BaseModel, Field, field_validator

from .flow_persistence import NullFlowPersistence, RedisFlowPersistence
from .contracts import variable_contract_errors
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
    allow_code_execution: bool = False
    user_interaction: bool = False


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


class ProposedTask(BaseModel):
    id: str
    name: str
    description: str
    expected_output: str
    agent_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    node_type: Literal['task', 'agent', 'crew', 'router', 'code'] = 'task'
    crew_agent_ids: list[str] = Field(default_factory=list)
    crew_tasks: list[ProposedCrewTask] = Field(default_factory=list)
    crew_process: Literal['sequential', 'hierarchical'] = 'sequential'
    human_feedback: bool = False
    feedback_message: str = '请审核当前结果'
    feedback_outcomes: list[str] = Field(default_factory=lambda: ['approved', 'revise'])
    feedback_default_outcome: str | None = None
    output_variables: list[ProposedOutputVariable] = Field(default_factory=list)
    dependency_variables: dict[str, list[dict]] = Field(default_factory=dict)


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
    summary: str | None = None
    interaction_mode: Literal['single_run', 'multi_turn'] | None = None
    process: Literal['sequential', 'hierarchical'] | None = None
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
    agents: list[ProposedAgent] = Field(default_factory=list, description='至少一个具有 role/goal/backstory/responsibilities 的 Agent')
    tasks: list[ProposedTask] = Field(default_factory=list, description='至少一个绑定 agent_id 的 Task')


class GenerationStageDecision(BaseModel):
    """Complete output contract for the direct generation stage."""
    intent: Literal['design', 'conversation'] = 'design'
    reply: str = ''
    title: str = ''
    summary: str = Field(default='', description='面向用户的应用简介，不得为空')
    kind: Literal['crew', 'flow'] = 'crew'
    process: Literal['sequential', 'hierarchical'] = 'sequential'
    interaction_mode: Literal['single_run', 'multi_turn'] = 'single_run'
    agents: list[ProposedAgent] = Field(default_factory=list, description='至少一个完整 Agent')
    tasks: list[ProposedTask] = Field(default_factory=list, description='至少一个绑定 Agent 的 Task')
    tools: list[str] = Field(default_factory=list)
    memory: bool = False
    planning: bool = False
    capability_requirements: list[CapabilityRequirement] = Field(default_factory=list)


class ArchitectureReview(BaseModel):
    approved: bool = False
    findings: list[str] = Field(default_factory=list)


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
    common = {
        key: existing[key]
        for key in ('title', 'original_request', 'interaction_mode',
                    'interaction_mode_preselected', 'resolved_clarifications',
                    'orchestration_intent_confirmed', 'application_purpose_known')
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
            'inputs': existing.get('inputs', []),
            'capability_requirements': existing.get('capability_requirements', []),
        }
    if state.stage == 'architecture':
        return {
            **common,
            'inputs': existing.get('inputs', []),
            'kind': existing.get('recommended_kind') or existing.get('kind') or state.kind,
            'process': existing.get('recommended_process') or existing.get('process', 'sequential'),
            'capability_requirements': existing.get('capability_requirements', []),
            'selected_resource_details': state.resources.get('selected_resource_details', {}),
        }
    architecture_summary = prior_summaries.get('architecture') or {}
    graph_agents = architecture_summary.get('agents') or existing.get('agents', [])
    graph_tasks = architecture_summary.get('tasks') or existing.get('tasks', [])
    return {
        **common,
        'inputs': existing.get('inputs', []),
        'kind': existing.get('recommended_kind') or existing.get('kind') or state.kind,
        'process': existing.get('recommended_process') or existing.get('process', 'sequential'),
        'capability_requirements': existing.get('capability_requirements', []),
        'agents': graph_agents,
        'tasks': graph_tasks,
        'tools': existing.get('tools', []),
        'selected_resource_details': state.resources.get('selected_resource_details', {}),
    }


def _stage_request_payload(state: ComposerState) -> dict:
    """Build the small, stage-local request envelope sent to an Agent."""
    payload = {
        'stage': state.stage,
        'original_request': state.existing.get('original_request') or state.request,
        'latest_user_message': state.request,
        'confirmed_context': _stage_existing_context(state),
        'stage_summary': (state.existing.get('stage_summaries') or {}).get(state.stage, {}),
    }
    if state.stage == 'discovery' and state.history:
        payload['conversation_history'] = state.history
    return payload


def composer_prompt(state: ComposerState) -> list[dict]:
    contracts = {
        'architecture': '''你是编排架构 Agent。只完成架构阶段：基于已锁定的输入和前置选择，设计最小可运行的 Crew/Flow 图。
返回 ArchitectureStageDecision JSON，必须包含：summary（面向用户的应用简介，1-2 句）、kind、process、interaction_mode、agents、tasks。
   confirmed_context.inputs 是用户已经确认且可能删改过的唯一运行输入清单，必须逐项读取其中的机器变量名；禁止继续引用清单中已删除的变量。每个 Agent 必须有 id、具体 role、可执行 goal、2-3 句 backstory 和 2-5 条 responsibilities；每个 Task 必须绑定 agent_id，description 明确使用的 `{variable}`，expected_output 可验收，依赖用 depends_on。只追踪从运行输入到最终交付的完整闭环端到端数据流。
   架构阶段必须一次性完成变量来源设计，不依赖后续 Agent 猜测：首节点可以直接使用 confirmed_context.inputs 中存在的 `{运行输入变量}`，不得把运行输入写进 dependency_variables；每个会被下游使用的结果都必须在生产任务的 output_variables 中显式声明；下游任务的 depends_on 必须包含生产它的直接上游 Task ID；dependency_variables 的对象键只能是该直接上游 Task ID，source_variable 必须是该上游 output_variables 中已声明的名称，target_variable 必须与下游 description/expected_output 中的占位符完全一致。description、expected_output、code_snippet 和内部 Crew Task 中出现的每个占位符，要么是已确认运行输入，要么有上述完整上游映射。禁止裸写变量名、引用不存在的任务或输出字段。只按这些规则生成一次架构，不调用额外审查 Agent。
   multi_turn 的首节点必须是普通 Agent，且必须由 user_interaction=true 的普通 Agent 负责（首节点必须是普通 Agent，不是把 Agent 记录成输入状态的伪任务）；层级 Crew 仅 manager 可交互，其他成员把缺失信息汇报给 manager；single_run 禁止 ask_user。user_interaction 只声明平台能力开关，不得把 ask_user 调用规则写入 Agent goal、backstory、responsibilities 或 Task description/expected_output，运行时会自动注入。尊重已确认的 kind、interaction_mode、inputs 和资源，不返回 generation 专属配置。
   资源绑定必须以 confirmed_context.capability_requirements 和 available_resources.selected_resource_details 为权威来源。资源 ID 只在各自 resource_type 内唯一，同一个数字可能同时是不同的 Skill、Tool 或 Knowledge；skill 只能写入 agent.skills，tool 只能写入 agent.plugins，knowledge 只能写入 agent.knowledge_base_ids。不得因为 ID 相同而跨类型解析，也不得绑定未被用户选中的 available_resources。''',
        'generation': '''你是编排生成 Agent。只完成生成阶段：把已确认的架构直接落实为可运行定义，检查每个节点的详细开关、Agent/Task 绑定、变量可达、资源绑定和最终交付。
返回 GenerationStageDecision JSON，必须包含完整可执行的 agents 和 tasks；保留已确认的 inputs、interaction_mode、kind、资源选择。补齐每个 Agent 的 role、goal、backstory、responsibilities；不得返回空数组或未绑定任务。summary 为空时也必须生成面向用户的应用简介。不要改变用户已锁定的前置选择。
confirmed_context.inputs 和 confirmed_context.tasks 是上一轮用户确认后的权威契约，其中包含用户删改后的输入以及架构阶段确定的 output_variables/dependency_variables。必须以它们为基础生成，不得恢复已删除输入，也不得丢弃或重新猜测已确认的变量来源。
变量核对是最终生成的硬门槛：逐个扫描 inputs、所有 task 和内部 Crew Task。每个 `{x}` 必须来自当前可见输入或真实上游映射；source_variable 必须存在于直接上游 output_variables，target_variable 必须是 ASCII snake_case 并在下游提示中使用同名占位符。先模拟首节点到最终节点的变量传递，发现任何变量不存在就修正后再返回。
交互约束：multi_turn 的第一个执行节点必须绑定 user_interaction=true 的普通 Agent。层级 Crew 中只有 manager/允许委派的管理 Agent 可以 user_interaction=true；其他 Agent 必须为 false，缺少信息时在任务结果中明确汇报给 manager。user_interaction 只声明平台能力开关，不得把 ask_user 调用规则写入 Agent goal、backstory、responsibilities 或 Task description/expected_output，运行时会自动注入。
文件约束：允许代码或文件工具的节点若会生成文件，必须在 output_variables 增加 value_type=file 的文件变量。若它不是最终节点，所有下游节点必须用 dependency_variables 接收并继续传递该文件变量，最终节点也必须声明同名 file 输出；不要把 MinIO key、本地绝对路径或 Markdown 伪链接当成文件变量。
资源绑定必须保留 confirmed_context.capability_requirements 中已确认的 resource_type 与 selected_ids，并以 available_resources.selected_resource_details 中的同类型详情配置 Agent。资源 ID 只在各自类型内唯一：skill→agent.skills、tool→agent.plugins、knowledge→agent.knowledge_base_ids；严禁按相同数字跨类型替换，严禁重新添加用户未选择的资源。''',
    }
    system = '你是玄枢平台的 CrewAI 应用总设计师，当前只负责一个阶段。' + contracts.get(
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
    resolved = state.existing.get('resolved_clarifications', {})
    next_unconfirmed_step = (
        'interaction_mode' if not (
            state.existing.get('interaction_mode_preselected') or resolved.get('interaction_mode')
        ) else 'resources' if not (
            state.existing.get('resource_selection_confirmed') or resolved.get('resource_selection')
        ) else 'orchestration_kind' if not (
            state.kind in {'crew', 'flow'} or state.existing.get('kind_preselected')
            or state.existing.get('kind_confirmed') or resolved.get('orchestration_kind')
        ) else 'complete'
    )
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
2. interaction_mode 必须保留 existing_proposal 已确认的值。multi_turn 除 message 外，只能增加随消息上传的 file/image；主题、格式和偏好由后续对话收集。single_run 必须一次列全完成最终交付所需、且应用无法从已配置资源或其他字段可靠取得的外部信息。不要把 message 当作所有业务字段的替代品：逐项反推最终交付物，凡是缺失后会迫使执行 Agent 追问、猜测或无法完成的独立信息，都应成为明确输入；只有真正可选或可由应用推断的内容才省略或设为非必填。
3. 外部文档、参考资料、附件、模板和历史文件使用 file；直接键入短内容用 text，多行正文用 long_text；可能多份文件时 multiple=true。
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
            '只补齐可运行的 agents 和 tasks，以及确实需要同步修改的字段。补齐前逐项核对 inputs、depends_on、dependency_variables 和 output_variables，不能引用不存在的变量或上游字段。不要返回空数组，不要解释，只返回 ComposerPatch JSON。'
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
            '每个下游占位符必须来自 confirmed_context.inputs，或来自直接上游 Task：'
            '上游在 output_variables 声明 source_variable，下游以该 Task ID 作为 '
            'dependency_variables 的键，并让 target_variable 与下游占位符同名。'
            '不得只在 description/expected_output 中口头声称输出了某个变量。'
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
    patch = parse_structured_output(output, ComposerPatch)
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


def generation_contract_findings(decision: ComposerDecision) -> list[str]:
    """Run the publish-time variable checks against Composer-shaped inputs."""
    definition = decision.model_dump()
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
    return variable_contract_errors(definition)


def generation_correction_prompt(state: ComposerState, proposal: ComposerDecision,
                                 review: ArchitectureReview) -> list[dict]:
    return [
        {'role': 'system', 'content': (
            '你是玄枢平台的 generation 修正 Agent。根据最终生成定义和审查结果，'
            '只修正审查指出的可运行性问题，保留用户目标、已确认输入和合理设计。必须重新核对每个输入变量、任务依赖、dependency_variables 的 source/target 和 output_variables；任何不存在的变量都要在本次修正中消除。'
            '只要 findings 涉及变量，必须返回完整 tasks：生产任务在 output_variables 声明下游所需输出；消费任务在 depends_on 引用生产任务 ID，并以该 Task ID 作为 dependency_variables 的键建立 source_variable/target_variable 映射；消费任务占位符使用 target_variable。不得只改说明文字或返回 summary。'
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
    system = '''你是玄枢平台的 CrewAI generation 最终审查 Agent。当前已经完成用户意图、运行输入、资源和 Crew/Flow 架构确认；只审查最终生成定义，不重新判断是否进入编排，也不提出新的确认问题。请从整体交付路径审查完整的端到端数据流：由运行时输入进入系统开始，沿着任务依赖、Agent 职责和真实资源一直追踪到最终输出，确认整套定义可真实运行、数据可达、能力可用且结果可验收。只返回精炼的问题清单，不重写整份方案。

关键审查规则：
1. 输入可达：逐项复核输入的 `name`、`variable` 和 `type`。`name` 是中文显示名称；`variable` 必须是具有业务语义的英文 snake_case，只能含 ASCII 英文字母、数字和下划线，严禁出现中文或直接复制 `name`。类型必须反映用户实际提供数据的方式：上传或选取外部文档必须是 `file`，用户称为参考资料、参考材料、附件、模板或历史文件且意图是上传给应用时也必须是 `file`，用户直接键入短内容才是 `text`，直接键入多行正文才是 `long_text`；不得把文件型资料降级成 `text`。每个运行输入都要有明确消费者，首个消费者必须在 description 中使用精确的 `{variable}`。裸写变量名、声明后未使用、让 Agent 重复询问已提供输入，都要在本轮直接改正。Flow 内嵌 Crew 的内部任务也必须看到它使用的显式变量或上游上下文。
2. 任务数据流：每个任务只有一个主要目的，description 必须包含执行方式、可用输入和约束，expected_output 必须对应可验证交付物；后续节点通过 depends_on 接收上游结果，最终任务真正交付用户目标。平台会把执行工具实际生成的文件作为观察性回执传给下游，但不能用固定扩展名强迫 Agent 生成用户本轮没有要求的文件。
3. 结构规模：只有职责、权限、模型、工具或输出契约有实质差异才拆分 Agent。Crew 承担自治协作，Flow 承担确定路由、状态、审批和 Crew 组合；hierarchical 只用于需要动态委派的场景。
4. 资源真实性：资源 id 必须来自 available_resources。Skill 放入 Agent.skills，Tool 放入 Agent.plugins，Knowledge 放入 Agent.knowledge_base_ids，并与 capability requirement 一致。选中的 MCP/HTTP Tool 已提供知识检索时，不得再制造重复的必需 Knowledge；平台 Knowledge 只有实际选中时才声明。Skill 依赖下载、命令、脚本或文件生成时，对应 Agent 必须设置 allow_code_execution=true。
   文件生成任务不得硬编码 `/workspace` 或任何宿主机路径；应要求代码从 `XUANSHU_WORKSPACE` 环境变量取得应用工作目录。Skill 已提供 scripts/ 时，先遵循其说明并让执行代码能够导入这些文件；可按任务需要直接调用、组合或扩展 Skill 中的代码，不得因为存在脚本而限制为单一执行方式。
5. 高级配置：reasoning 和 memory 必须有明确设计理由，不能作为默认装饰配置。
6. 交互状态：已确认的 interaction_mode=multi_turn 必须有普通 Agent 节点负责收集本轮消息，并设置 user_interaction=true；其余 Agent 保持 false。interaction_mode=single_run 不得引入 ask_user 或无意义的会话状态节点。
7. 分步确认：只有 Flow 任务可以设置 human_feedback=true。Crew 任务不得使用 human_feedback 或 CrewAI human_input。
8. 变量审计：把 inputs、每个任务的 output_variables 和 dependency_variables 画成逐节点传递表。确认每个 description、expected_output、code_snippet 和内部 Crew Task 中的 `{变量}` 都能在该节点实际获得；dependency_variables 的对象键必须是直接上游任务 ID，不能把 contract_files 等输入或输出变量名当成任务 ID；source_variable 必须存在于直接上游的 output_variables，target_variable 必须是 ASCII snake_case 并与下游提示中的占位符完全一致；禁止使用不存在的任务 ID、输出字段、中文变量名或只在自然语言中提到但没有声明的字段。发现一处不匹配就必须列为 finding，要求修正具体字段，不能用“运行时自行处理”带过。

只返回 ArchitectureReview：没有问题时 approved=true 且 findings=[]；有问题时 approved=false，每条 finding 必须说明具体字段、错误及修正要求。不要输出内部推理过程。'''
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
            resolved = self.state.existing.get('resolved_clarifications', {})

            def discovery_status(value: DiscoveryDecision) -> tuple[str, bool, bool, bool]:
                interaction_explicit = bool(
                    value.interaction_mode_explicit
                    or self.state.existing.get('interaction_mode_preselected')
                    or resolved.get('interaction_mode')
                )
                resources_explicit = bool(
                    value.resource_selection_explicit
                    or self.state.existing.get('resource_selection_confirmed')
                    or resolved.get('resource_selection')
                )
                kind_explicit = bool(
                    value.kind_explicit or self.state.kind in {'crew', 'flow'}
                    or self.state.existing.get('kind_preselected')
                    or self.state.existing.get('kind_confirmed')
                    or resolved.get('orchestration_kind')
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
                repaired = merge_composer_patch(decision.model_dump(), repair_patch)
                repaired['intent'] = repair_patch.get('intent', 'design')
                decision = ComposerDecision.model_validate(_normalize_composer_payload(repaired))
            if decision.intent == 'design' and self.state.stage == 'architecture':
                findings = generation_contract_findings(decision)
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
                    corrected = merge_composer_patch(decision.model_dump(), correction_patch)
                    corrected['intent'] = correction_patch.get('intent', 'design')
                    decision = ComposerDecision.model_validate(
                        _normalize_composer_payload(corrected)
                    )
                    findings = generation_contract_findings(decision)
        self.state.analysis = decision.model_dump()
        return self.state.analysis

    @listen(analyze_intent)
    def review_architecture(self, decision_data):
        decision = ComposerDecision.model_validate(decision_data)
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
        output = kickoff_structured(
            generation_review_agent(self.state), generation_review_prompt(self.state, decision),
            ArchitectureReview, self.state.model, label='generation_review',
        )
        reviewed = parse_structured_output(output, ArchitectureReview)
        findings = list(dict.fromkeys([
            *generation_contract_findings(decision),
            *reviewed.findings,
        ]))
        if not findings:
            self.state.analysis = decision.model_dump()
            return self.state.analysis
        corrected = decision
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
                merge_composer_patch(corrected.model_dump(), correction_patch)
            ))
            corrected.intent = 'design'
            if self.state.kind in {'crew', 'flow'}:
                corrected.kind = self.state.kind
            findings = generation_contract_findings(corrected)
            if not findings:
                break
        self.state.analysis = corrected.model_dump()
        return self.state.analysis

    @listen(review_architecture)
    def merge_proposal(self, decision_data):
        decision = ComposerDecision.model_validate(decision_data)
        if decision.intent == 'conversation':
            return self.state.result
        if self.state.kind in {'crew', 'flow'}:
            decision.kind = self.state.kind
        proposal = {
            **self.state.existing,
            **decision.model_dump(exclude={'intent', 'reply'}),
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
        resolved = self.state.existing.get('resolved_clarifications', {}) or {}
        locked_interaction = bool(
            self.state.existing.get('interaction_mode_preselected')
            or resolved.get('interaction_mode')
        )
        locked_mode = resolved.get('interaction_mode')
        if locked_mode not in {'single_run', 'multi_turn'}:
            locked_mode = self.state.existing.get('interaction_mode')
        if locked_interaction and locked_mode in {'single_run', 'multi_turn'}:
            proposal['interaction_mode'] = locked_mode
            proposal['interaction_mode_preselected'] = True
        if self.state.stage != 'discovery' and proposal.get('interaction_mode') == 'multi_turn':
            normalized_inputs = normalize_runtime_inputs(proposal.get('inputs', []))
            message_input = next(
                (item for item in normalized_inputs if item.get('variable') == 'message'),
                {'name': '用户需求', 'variable': 'message', 'type': 'long_text',
                 'required': True, 'multiple': False, 'description': '用户本轮需求描述'},
            )
            message_input.update({'variable': 'message', 'type': 'long_text', 'required': True,
                                  'multiple': False})
            proposal['inputs'] = [
                message_input,
                *[
                    item for item in normalized_inputs
                    if item.get('variable') != 'message' and item.get('type') in {'file', 'image'}
                ],
            ]
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
            memory.remember(
                _memory_summary(result),
                scope='/preferences',
                categories=['composer-preference'],
                metadata={'workspace_id': workspace_id, 'stage': stage},
                importance=.75,
                source=str(user_id),
                private=True,
            )
        return result
    finally:
        _composer_progress_callback.reset(progress_token)
        if memory:
            memory.close()
