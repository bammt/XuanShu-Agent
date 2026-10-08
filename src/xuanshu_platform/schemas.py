import copy
import json
import re
from typing import Literal
from pydantic import BaseModel, Field, model_validator

from .contracts import (code_signature, ensure_fixed_output_contracts, execution_order,
                        fixed_output_contract, upstream_node_ids)

class AgentDefinition(BaseModel):
    id: str; role: str; goal: str; backstory: str = ''
    model_id: int | None = None; model_profile_id: str | None = None
    memory: bool = False; skills: list[str] = Field(default_factory=list); plugins: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    max_iter: int = 12; max_rpm: int | None = None; max_execution_time: int | None = None
    max_retry_limit: int = 2; reasoning: bool = False; max_reasoning_attempts: int | None = None
    allow_delegation: bool = False; respect_context_window: bool = True; multimodal: bool = False
    allow_code_execution: bool = False
    inject_date: bool = False; date_format: str = '%Y-%m-%d'; use_system_prompt: bool = True
    function_calling_model_profile_id: str | None = None; tools: list[str] = Field(default_factory=list)
    # ``ask_user`` is an explicit Agent capability. It is materialized only for
    # this Agent in a multi-turn application. Chat text arrives through the
    # platform-reserved ``message`` value; declared inputs are optional hints.
    user_interaction: bool = False

class OutputVariable(BaseModel):
    name: str
    value_type: Literal['string', 'number', 'boolean', 'object', 'array', 'file', 'any'] = 'string'
    description: str = ''

class CrewTaskDefinition(BaseModel):
    id: str; name: str; description: str; expected_output: str = '清晰、完整的最终结果'
    agent_id: str | None = None; depends_on: list[str] = Field(default_factory=list)
    output_variables: list[OutputVariable] = Field(default_factory=list); dependency_variables: dict[str, list[dict]] = Field(default_factory=dict)
    async_execution: bool = False; markdown: bool = False; output_file: str = ''; create_directory: bool = True
    output_mode: Literal['text', 'json'] = 'text'
    guardrail: str = ''; guardrail_max_retries: int = 3

    @model_validator(mode='after')
    def normalize_platform_contract(self):
        self.output_variables = [
            OutputVariable.model_validate(item) for item in fixed_output_contract('task')
        ]
        self.guardrail = ''
        self.guardrail_max_retries = 0
        return self

class TaskDefinition(BaseModel):
    id: str; name: str; description: str = ''; expected_output: str = '清晰、完整的最终结果'
    agent_id: str | None = None; depends_on: list[str] = Field(default_factory=list)
    node_type: Literal['task', 'agent', 'crew', 'router', 'code', 'tool'] = 'task'
    crew_agent_ids: list[str] = Field(default_factory=list); crew_tasks: list[CrewTaskDefinition] = Field(default_factory=list)
    crew_process: Literal['sequential', 'hierarchical'] = 'sequential'
    # Crew-node settings mirror the application Crew settings instead of
    # silently inheriting unrelated defaults.
    crew_memory: bool = False
    crew_planning: bool = False
    crew_cache: bool = True
    crew_output_log_file: str = ''
    crew_manager_agent_id: str | None = None
    # A nested hierarchical Crew may use CrewAI's manager_llm instead of a
    # manager Agent, matching the top-level Crew setting.
    crew_manager_model_profile_id: str | None = None
    crew_planning_model_profile_id: str | None = None
    # Keep the complete Crew runtime surface available on an embedded Crew.
    # Flow Crew membership is supplied by Agent→Crew canvas edges and is
    # persisted in ``crew_agent_ids``; the node itself still executes normally.
    crew_verbose: bool = False
    condition: str = ''; run_if: str = ''; routes: dict[str, list[str]] = Field(default_factory=dict)
    router_rules: list[dict] = Field(default_factory=list)
    code_snippet: str = ''
    input_bindings: dict[str, dict] = Field(default_factory=dict)
    execution_contract: Literal['legacy', 'object'] = 'legacy'
    tool_id: str | None = None
    human_feedback: bool = False
    feedback_message: str = '请审核当前结果'
    feedback_outcomes: list[str] = Field(default_factory=lambda: ['approved', 'revise'])
    feedback_default_outcome: str | None = None
    output_variables: list[OutputVariable] = Field(default_factory=list); dependency_variables: dict[str, list[dict]] = Field(default_factory=dict)
    async_execution: bool = False; markdown: bool = False; output_file: str = ''; create_directory: bool = True
    # Output format belongs to the Task contract. An Agent may execute tasks
    # with different formats, so it must not be configured on AgentDefinition.
    output_mode: Literal['text', 'json'] = 'text'
    guardrail: str = ''; guardrail_max_retries: int = 3

    @model_validator(mode='after')
    def normalize_deterministic_prompts(self):
        self.output_variables = [OutputVariable.model_validate(item) for item in fixed_output_contract(self.node_type)]
        if self.node_type == 'crew' and self.crew_tasks:
            order = execution_order({'tasks': [item.model_dump() for item in self.crew_tasks]})
            final_task = next(item for item in self.crew_tasks if item.id == order[-1])
            self.output_mode = final_task.output_mode
        self.guardrail = ''
        self.guardrail_max_retries = 0
        if self.node_type in {'code', 'tool'}:
            self.description = ''
            self.expected_output = ''
            self.output_file = ''
            self.markdown = False
            self.async_execution = False
            self.human_feedback = False
            self.feedback_message = ''
            self.feedback_outcomes = []
            self.feedback_default_outcome = None
        return self

class InputDefinition(BaseModel):
    name: str
    label: str = ''
    input_type: Literal['text', 'long_text', 'file', 'image', 'number', 'boolean', 'json'] = 'text'
    required: bool = False
    multiple: bool = False
    description: str = ''


class MemoryPolicy(BaseModel):
    """Three independent forms of memory used by a published application."""
    conversation_history: bool = True
    runtime_checkpoint: bool = True
    long_term_semantic: bool = False

class ApplicationDefinition(BaseModel):
    kind: Literal['crew', 'flow'] = 'crew'
    process: Literal['sequential', 'hierarchical'] = 'sequential'
    agents: list[AgentDefinition]; tasks: list[TaskDefinition]; memory: bool = False
    inputs: list[InputDefinition] = Field(default_factory=list)
    interaction_mode: Literal['single_run', 'multi_turn'] = 'single_run'
    interaction: dict = Field(default_factory=dict)
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    model_profile_id: str | None = None
    planning: bool = False; manager_agent_id: str | None = None; manager_model_profile_id: str | None = None
    planning_model_profile_id: str | None = None; cache: bool = True
    verbose: bool = False
    output_log_file: str = ''; max_method_calls: int = 100

    @model_validator(mode='before')
    @classmethod
    def normalize_output_contracts(cls, value):
        if not isinstance(value, dict):
            return value
        document = copy.deepcopy(value)
        inputs = list(document.get('inputs') or [])
        file_inputs = [item for item in inputs if isinstance(item, dict) and item.get('input_type', item.get('type')) in {'file', 'image'}]
        if file_inputs:
            old_names = {
                str(item.get('name') or item.get('variable') or '').strip()
                for item in file_inputs
                if str(item.get('name') or item.get('variable') or '').strip()
                and str(item.get('name') or item.get('variable') or '').strip() != 'files'
            }
            if not old_names and any(
                str(item.get('name') or item.get('variable') or '') == 'files'
                for item in inputs if isinstance(item, dict)
            ):
                placeholder = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
                file_hint = re.compile(r'(?:file|files|attachment|document|reference|material|template|upload|excel|csv|pdf)', re.I)
                for task in document.get('tasks', []) or []:
                    for field in ('description', 'objective', 'expected_output', 'code_snippet'):
                        value = task.get(field) if isinstance(task, dict) else None
                        if isinstance(value, str):
                            old_names.update(
                                name for name in placeholder.findall(value)
                                if name != 'files' and file_hint.search(name)
                            )
            document['inputs'] = [item for item in inputs if item not in file_inputs]
            if not any(
                str(item.get('name') or item.get('variable') or '') == 'files'
                for item in document['inputs'] if isinstance(item, dict)
            ):
                document['inputs'].append({
                    'name': 'files',
                    'label': '本轮文件',
                    'input_type': 'file',
                    'required': any(bool(item.get('required')) for item in file_inputs),
                    'multiple': True,
                    'description': '用户每轮对话上传的文件列表，后续轮次追加，不覆盖已有文件。',
                })

            def rewrite_task(task):
                if not isinstance(task, dict):
                    return
                for field in ('description', 'objective', 'expected_output', 'code_snippet'):
                    if isinstance(task.get(field), str):
                        for old in old_names:
                            task[field] = task[field].replace('{' + old + '}', '{files}')
                bindings = task.get('input_bindings') or {}
                if isinstance(bindings, dict):
                    for binding in bindings.values():
                        if (isinstance(binding, dict) and binding.get('source') == 'input'
                                and str(binding.get('variable') or '') in old_names):
                            binding['variable'] = 'files'
                for rule in task.get('router_rules') or []:
                    expression = rule.get('expression') if isinstance(rule, dict) else None
                    if (isinstance(expression, dict) and expression.get('source') == 'input'
                            and str(expression.get('variable') or '') in old_names):
                        expression['variable'] = 'files'
                for nested in task.get('crew_tasks') or []:
                    rewrite_task(nested)

            for task in document.get('tasks', []) or []:
                rewrite_task(task)
        elif any(
            str(item.get('name') or item.get('variable') or '') == 'files'
            for item in inputs if isinstance(item, dict)
        ):
            # Some persisted Studio proposals normalized the input card before
            # the model's old file variable was rewritten. Recover only clear
            # file-shaped aliases such as ``alert_excel`` or ``reference_file``.
            placeholder = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
            file_hint = re.compile(r'(?:file|files|attachment|document|reference|material|template|upload|excel|csv|pdf)', re.I)

            def rewrite_implicit(task):
                if not isinstance(task, dict):
                    return
                for field in ('description', 'objective', 'expected_output', 'code_snippet'):
                    value = task.get(field)
                    if isinstance(value, str):
                        for name in placeholder.findall(value):
                            if name != 'files' and file_hint.search(name):
                                value = value.replace('{' + name + '}', '{files}')
                        task[field] = value
                for nested in task.get('crew_tasks') or []:
                    rewrite_implicit(nested)

            for task in document.get('tasks', []) or []:
                rewrite_implicit(task)
        return ensure_fixed_output_contracts(document)

    @model_validator(mode='after')
    def validate_graph(self):
        if self.memory:
            self.memory_policy.long_term_semantic = True
        aids = {x.id for x in self.agents}; tids = {x.id for x in self.tasks}
        input_types = {item.name: item.input_type for item in self.inputs}
        input_types.update({'message': 'text', 'files': 'file', 'conversation_history': 'json'})
        if len(aids) != len(self.agents) or len(tids) != len(self.tasks): raise ValueError('Agent 或任务 ID 重复')
        collection_id = str(self.interaction.get('collection_task_id') or '').strip()
        if collection_id and collection_id not in tids:
            raise ValueError(f'交互收集节点不存在：{collection_id}')
        interactive_agents = [item for item in self.agents if item.user_interaction]
        if interactive_agents and self.interaction_mode != 'multi_turn':
            raise ValueError('ask_user 只能用于 multi_turn 应用，single_run 不允许开启 Agent 用户交互')
        if self.interaction_mode == 'multi_turn' and self.tasks and interactive_agents:
            # There is no single configured collection node. Every node bound
            # to an interactive Agent may request input. A run pauses at the
            # first request actually reached and resumes from that checkpoint.
            interactive_ids = {item.id for item in interactive_agents}
            interactive_tasks = [
                item for item in self.tasks
                if (
                    item.node_type in {'task', 'agent'} and item.agent_id in interactive_ids
                ) or (
                    item.node_type == 'crew'
                    and bool(set(item.crew_agent_ids) & interactive_ids)
                )
            ]
            if not interactive_tasks:
                raise ValueError('多轮应用至少需要一个 Agent 或 Crew 节点包含启用了 ask_user 的 Agent')
            interactive_task_ids = self.interaction.get('interactive_task_ids') or []
            if not isinstance(interactive_task_ids, list):
                raise ValueError('interactive_task_ids 必须是数组')
            for task_id in interactive_task_ids:
                task = next((item for item in self.tasks if item.id == str(task_id)), None)
                if task is None:
                    raise ValueError(f'交互节点不存在：{task_id}')
                task_owners = (
                    {task.agent_id} if task.node_type in {'task', 'agent'}
                    else set(task.crew_agent_ids) if task.node_type == 'crew'
                    else set()
                )
                if not task_owners & interactive_ids:
                    raise ValueError(f'交互节点配置无效：{task_id}')
            # Keep the old field readable for old clients, but never use it to
            # restrict the set of interactive tasks.
            legacy_id = str(self.interaction.get('collection_task_id') or '').strip()
            if legacy_id:
                legacy = next((item for item in self.tasks if item.id == legacy_id), None)
                if legacy is None:
                    raise ValueError(f'交互收集节点不存在：{legacy_id}')
                legacy_owners = (
                    {legacy.agent_id} if legacy.node_type in {'task', 'agent'}
                    else set(legacy.crew_agent_ids) if legacy.node_type == 'crew'
                    else set()
                )
                if not legacy_owners & interactive_ids:
                    raise ValueError(f'交互收集节点配置无效：{legacy_id}')
            if self.process == 'hierarchical':
                if not self.manager_agent_id:
                    raise ValueError('多轮层级 Crew 必须指定管理 Agent')
                if {item.id for item in interactive_agents} != {self.manager_agent_id}:
                    raise ValueError('多轮层级 Crew 只能由管理 Agent 启用 ask_user')
        # These fields are runtime envelopes, not user-authored schemas. Set
        # them before validating router expressions so a router can bind to a
        # newly-created deterministic node even when its draft has no explicit
        # output_variables yet.
        for task in self.tasks:
            task.output_variables = [OutputVariable.model_validate(item) for item in fixed_output_contract(task.node_type)]
        task_by_id = {item.id: item for item in self.tasks}
        for router in self.tasks:
            if router.node_type != 'router' or not router.router_rules:
                continue
            rule_ids = [str(rule.get('id') or '').strip() for rule in router.router_rules]
            if any(not rule_id for rule_id in rule_ids) or len(rule_ids) != len(set(rule_ids)):
                raise ValueError(f'路由节点 {router.name} 的 CASE ID 必须存在且唯一')
            _validate_router_expression(router.router_rules, input_types, task_by_id, router)
            allowed_routes = set(rule_ids) | {'else'}
            unknown_routes = set(router.routes) - allowed_routes
            if unknown_routes:
                raise ValueError(f'路由节点 {router.name} 存在未知分支：{", ".join(sorted(unknown_routes))}')
            for branch, targets in router.routes.items():
                for target_id in targets:
                    target = task_by_id.get(str(target_id))
                    if target is None:
                        raise ValueError(f'路由节点 {router.name} 的分支引用不存在的节点：{target_id}')
                    if router.id not in target.depends_on:
                        target.depends_on.append(router.id)
        for task in self.tasks:
            task.output_variables = [OutputVariable.model_validate(item) for item in fixed_output_contract(task.node_type)]
            output_names = [field.name for field in task.output_variables]
            if len(output_names) != len(set(output_names)):
                raise ValueError(f'任务 {task.name} 的输出变量名重复')
            if task.node_type in {'code', 'tool'}:
                # Legacy deterministic nodes encoded execution dependencies
                # only in dependency_variables. Promote those sources before
                # validating the common dependency contract so old drafts do
                # not lose their execution order during migration.
                for dependency_id in task.dependency_variables:
                    if dependency_id not in tids:
                        raise ValueError(f'任务 {task.name} 引用了不存在的依赖节点：{dependency_id}')
                    if dependency_id not in task.depends_on:
                        task.depends_on.append(dependency_id)
            for dependency_id, mappings in task.dependency_variables.items():
                if dependency_id not in task.depends_on:
                    raise ValueError(f'任务 {task.name} 的变量映射必须引用直接依赖节点：{dependency_id}')
                for mapping in mappings:
                    target = str(mapping.get('target_variable') or '').strip()
                    source = str(mapping.get('source_variable') or 'result').strip()
                    if not target.isidentifier():
                        raise ValueError(f'任务 {task.name} 的依赖变量目标名无效：{target}')
                    if not source:
                        raise ValueError(f'任务 {task.name} 的依赖变量来源不能为空')
            if task.node_type in {'code', 'tool'}:
                # Older Studio drafts represented deterministic-node arguments
                # only as dependency_variables. Migrate those mappings into the
                # binding contract that the runtime actually passes to main/tool.
                for dependency_id, mappings in task.dependency_variables.items():
                    dependency = next((item for item in self.tasks if item.id == dependency_id), None)
                    for mapping in mappings:
                        target = str(mapping.get('target_variable') or '').strip()
                        source = str(mapping.get('source_variable') or '$raw').strip()
                        if not target or target in task.input_bindings:
                            continue
                        source_field = next(
                            (field for field in (dependency.output_variables if dependency else [])
                             if field.name == source),
                            None,
                        )
                        task.input_bindings[target] = {
                            'source': 'node', 'node_id': dependency_id,
                            'variable': source,
                            'value_type': source_field.value_type if source_field else 'object',
                        }
                # A deterministic binding is itself an executable data
                # dependency. Accept direct API payloads that provide the
                # binding but omit the redundant canvas edge; the edge is
                # materialized before validating direct-dependency references.
                for binding in task.input_bindings.values():
                    if binding.get('source') != 'node':
                        continue
                    dependency_id = binding.get('node_id')
                    ancestors = upstream_node_ids(
                        task.id,
                        [item.model_dump(mode='python') for item in self.tasks],
                    )
                    if (dependency_id in tids and dependency_id not in task.depends_on
                            and dependency_id not in ancestors):
                        task.depends_on.append(dependency_id)
                for name, binding in task.input_bindings.items():
                    if not name.isidentifier(): raise ValueError(f'无效节点输入名：{name}')
                    if binding.get('source') not in {'input', 'node', 'literal'}:
                        raise ValueError(f'节点输入 {name} 缺少有效来源')
                    if binding.get('source') == 'node' and binding.get('node_id') not in tids:
                        raise ValueError(f'节点输入 {name} 引用了不存在的上游节点')
                    if binding.get('source') == 'input' and binding.get('variable') not in {x.name for x in self.inputs} | {'message','files','conversation_history'}:
                        raise ValueError(f'节点输入 {name} 引用了未声明的输入')
                if task.node_type == 'tool' and not task.tool_id:
                    raise ValueError('工具节点必须选择工具')
                if task.node_type == 'code' and not task.code_snippet.strip():
                    raise ValueError('代码节点必须提供代码')
                if task.node_type == 'code' and task.execution_contract == 'object':
                    declared, required, accepts_kwargs = code_signature(task.code_snippet)
                    if declared and not accepts_kwargs:
                        unknown = sorted(set(task.input_bindings) - declared)
                        if unknown:
                            raise ValueError(
                                f'代码节点 {task.name} 的输入不在 main 签名中：{", ".join(unknown)}'
                            )
                        missing = sorted(required - set(task.input_bindings))
                        if missing:
                            raise ValueError(
                                f'代码节点 {task.name} 缺少 main 必填输入：{", ".join(missing)}'
                            )

            if task.agent_id and task.agent_id not in aids: raise ValueError(f'任务 {task.name} 引用了不存在的 Agent')
            if task.node_type in {'task', 'agent'} and not task.agent_id and self.process != 'hierarchical': raise ValueError(f'任务 {task.name} 必须选择 Agent')
            if task.node_type == 'crew' and not task.crew_agent_ids: raise ValueError(f'Crew 节点 {task.name} 至少需要一个 Agent')
            if task.node_type == 'crew' and not task.crew_tasks: raise ValueError(f'Crew 节点 {task.name} 至少需要一个内部 Task')
            if task.node_type == 'crew' and task.crew_process == 'hierarchical':
                if len(task.crew_agent_ids) < 2:
                    raise ValueError(f'层级 Crew 节点 {task.name} 至少需要一个管理 Agent和一个执行 Agent')
                manager_id = task.crew_manager_agent_id
                if manager_id and manager_id not in task.crew_agent_ids:
                    raise ValueError(f'层级 Crew 节点 {task.name} 的管理 Agent 必须属于该 Crew')
                if not manager_id and not task.crew_manager_model_profile_id:
                    raise ValueError(f'层级 Crew 节点 {task.name} 必须指定独立的管理 Agent')
                if manager_id:
                    manager = next((agent for agent in self.agents if agent.id == manager_id), None)
                    if manager is not None:
                        if any(nested.agent_id == manager_id for nested in task.crew_tasks):
                            raise ValueError(f'层级 Crew 节点 {task.name} 的管理 Agent 不得绑定内部 Task')
                        if manager.skills or manager.plugins or manager.knowledge_base_ids or manager.tools:
                            raise ValueError(f'层级 Crew 节点 {task.name} 的管理 Agent 不得绑定 Skill、Tool 或知识库')
                invalid_interactive = [
                    agent.id for agent in self.agents
                    if agent.id in task.crew_agent_ids and agent.user_interaction
                    and agent.id != (manager_id or task.crew_agent_ids[0])
                ]
                if invalid_interactive:
                    raise ValueError(f'层级 Crew 节点 {task.name} 只能由第一个管理 Agent 启用 ask_user')
            if any(x not in aids for x in task.crew_agent_ids): raise ValueError(f'Crew 节点 {task.name} 存在无效 Agent')
            if task.node_type == 'crew':
                nested_ids = {item.id for item in task.crew_tasks}
                if len(nested_ids) != len(task.crew_tasks): raise ValueError(f'Crew 节点 {task.name} 的内部 Task ID 重复')
                for nested in task.crew_tasks:
                    nested.output_variables = [OutputVariable.model_validate(item) for item in fixed_output_contract('task')]
                    nested_names = [field.name for field in nested.output_variables]
                    if len(nested_names) != len(set(nested_names)):
                        raise ValueError(f'Crew 节点 {task.name} 的内部 Task 输出变量名重复')
                    if task.crew_process == 'hierarchical':
                        nested.agent_id = None
                    if nested.agent_id and nested.agent_id not in task.crew_agent_ids:
                        raise ValueError(f'Crew 节点 {task.name} 的内部 Task 引用了未加入 Crew 的 Agent')
                    if task.crew_process != 'hierarchical' and not nested.agent_id:
                        raise ValueError(
                            f'顺序 Crew 节点 {task.name} 的内部 Task {nested.name} 必须明确选择该 Crew 中的 Agent'
                        )
                    if any(key not in nested_ids for key in nested.depends_on):
                        raise ValueError(f'Crew 节点 {task.name} 的内部 Task 存在无效依赖')
                    if any(key not in nested.depends_on for key in nested.dependency_variables):
                        raise ValueError(f'Crew 节点 {task.name} 的内部 Task 变量映射必须引用直接依赖')
                nested_pending = {item.id: set(item.depends_on) for item in task.crew_tasks}
                nested_resolved: set[str] = set()
                while nested_pending:
                    nested_ready = [
                        node_id for node_id, dependencies in nested_pending.items()
                        if dependencies <= nested_resolved
                    ]
                    if not nested_ready:
                        raise ValueError(f'Crew 节点 {task.name} 的内部 Task 存在循环依赖')
                    for node_id in nested_ready:
                        nested_resolved.add(node_id)
                        nested_pending.pop(node_id)
            if any(x not in tids for x in task.depends_on): raise ValueError(f'任务 {task.name} 存在无效依赖')
            if task.node_type in {'code', 'tool'}:
                # A node binding is only allowed to address a declared output
                # field on a direct dependency.  Runtime must never discover a
                # typo after an expensive tool or code invocation starts.
                for name, binding in task.input_bindings.items():
                    if binding.get('source') != 'node':
                        continue
                    dependency = next((item for item in self.tasks if item.id == binding.get('node_id')), None)
                    if dependency is None:
                        continue
                    if dependency.id not in upstream_node_ids(task.id, [item.model_dump(mode='python') for item in self.tasks]):
                        raise ValueError(f'节点 {task.name} 的输入 {name} 必须选择执行依赖路径上的上游节点')
                    path = str(binding.get('variable') or 'output').strip()
                    root = path.split('.', 1)[0]
                    dependency_outputs = {field.name for field in dependency.output_variables}
                    if path != '$raw' and root not in dependency_outputs:
                        raise ValueError(
                            f'节点 {task.name} 的输入 {name} 引用了上游 {dependency.name} 未声明的输出：{path}'
                        )
        pending = {item.id: set(item.depends_on) for item in self.tasks}
        resolved: set[str] = set()
        while pending:
            ready = [node_id for node_id, dependencies in pending.items() if dependencies <= resolved]
            if not ready:
                raise ValueError('Flow 存在循环依赖')
            for node_id in ready:
                resolved.add(node_id)
                pending.pop(node_id)
        if self.manager_agent_id and self.manager_agent_id not in aids:
            raise ValueError('管理 Agent 不存在')
        if self.process == 'hierarchical' and self.manager_agent_id and len(aids) < 2:
            raise ValueError('层级 Crew 除管理 Agent 外至少还需要一个执行 Agent')
        if self.process == 'hierarchical' and not self.manager_agent_id:
            raise ValueError('层级 Crew 必须指定独立的管理 Agent')
        if self.process == 'hierarchical' and self.manager_agent_id:
            manager = next(agent for agent in self.agents if agent.id == self.manager_agent_id)
            if any(task.agent_id == self.manager_agent_id for task in self.tasks):
                raise ValueError('层级 Crew 的管理 Agent 不得绑定任何任务')
            if manager.skills or manager.plugins or manager.knowledge_base_ids or manager.tools:
                raise ValueError('层级 Crew 的管理 Agent 不得绑定 Skill、Tool 或知识库')
        if self.process == 'hierarchical':
            for agent in self.agents:
                agent.allow_delegation = bool(
                    self.manager_agent_id and agent.id == self.manager_agent_id
                )
        hierarchical_managers = set()
        if self.kind == 'crew' and self.process == 'hierarchical' and self.manager_agent_id:
            hierarchical_managers.add(str(self.manager_agent_id))
        for node in self.tasks:
            if node.node_type != 'crew' or node.crew_process != 'hierarchical':
                continue
            manager_id = node.crew_manager_agent_id
            if not manager_id and not node.crew_manager_model_profile_id and node.crew_agent_ids:
                manager_id = node.crew_agent_ids[0]
            if manager_id:
                hierarchical_managers.add(str(manager_id))
                for agent in self.agents:
                    if agent.id in node.crew_agent_ids:
                        agent.allow_delegation = agent.id == str(manager_id)
        for agent in self.agents:
            if agent.allow_delegation and agent.id not in hierarchical_managers:
                raise ValueError(f'Agent {agent.role} 只有作为层级 Crew 的管理 Agent 才能启用允许委派')
        return self


def _validate_router_expression(rules, input_types, task_by_id, router):
    allowed_operators = {
        'equals', 'not_equals', 'contains', 'not_contains', 'starts_with',
        'ends_with', 'greater_than', 'less_than', 'is_empty', 'is_not_empty',
    }

    def visit(expression):
        if not isinstance(expression, dict):
            raise ValueError(f'路由节点 {router.name} 的条件格式无效')
        if expression.get('type') == 'group':
            if expression.get('operator') not in {'and', 'or'}:
                raise ValueError(f'路由节点 {router.name} 的条件组必须使用 AND 或 OR')
            children = expression.get('conditions')
            if not isinstance(children, list) or not children:
                raise ValueError(f'路由节点 {router.name} 的条件组不能为空')
            for child in children:
                visit(child)
            return
        if expression.get('type') != 'condition':
            raise ValueError(f'路由节点 {router.name} 存在未知条件类型')
        if expression.get('operator') not in allowed_operators:
            raise ValueError(f'路由节点 {router.name} 存在不支持的条件运算符')
        operator = expression['operator']
        value_type = str(expression.get('value_type') or '').strip()
        if operator not in {'is_empty', 'is_not_empty'} and 'value' not in expression:
            raise ValueError(f'路由节点 {router.name} 的条件缺少比较值')
        source = expression.get('source')
        variable = str(expression.get('variable') or '').strip()
        if not variable:
            raise ValueError(f'路由节点 {router.name} 的条件必须选择变量')
        if source == 'input':
            if variable not in input_types:
                raise ValueError(f'路由节点 {router.name} 引用了未声明的运行输入：{variable}')
            declared_type = input_types[variable]
        elif source == 'node':
            node_id = str(expression.get('node_id') or '')
            dependency = task_by_id.get(node_id)
            if dependency is None:
                raise ValueError(f'路由节点 {router.name} 引用了不存在的上游节点：{node_id}')
            ancestors = upstream_node_ids(
                router.id, [item.model_dump(mode='python') for item in task_by_id.values()],
            )
            if node_id not in ancestors and node_id not in router.depends_on:
                router.depends_on.append(node_id)
            root = variable.split('.', 1)[0]
            output_field = next((field for field in dependency.output_variables if field.name == root), None)
            if output_field is None:
                raise ValueError(f'路由节点 {router.name} 引用了上游未声明的输出变量：{variable}')
            declared_type = output_field.value_type
        else:
            raise ValueError(f'路由节点 {router.name} 的条件来源必须是运行输入或节点输出')
        type_aliases = {'text': 'string', 'long_text': 'string', 'json': 'object', 'image': 'file'}
        declared_type = type_aliases.get(str(declared_type), str(declared_type))
        value_type = type_aliases.get(value_type, value_type) if value_type else declared_type
        if value_type != declared_type:
            raise ValueError(f'路由节点 {router.name} 的条件变量类型与来源不一致：{variable}')
        expression['value_type'] = value_type
        if operator in {'greater_than', 'less_than'} and value_type != 'number':
            raise ValueError(f'路由节点 {router.name} 的大于/小于条件只能用于数字变量')
        if operator not in {'is_empty', 'is_not_empty'} and value_type == 'number':
            try:
                float(expression['value'])
            except (TypeError, ValueError) as exc:
                raise ValueError(f'路由节点 {router.name} 的数字比较值无效') from exc
        if operator not in {'is_empty', 'is_not_empty'} and value_type in {'object', 'array'}:
            expected = expression['value']
            if isinstance(expected, str):
                try:
                    expected = json.loads(expected)
                except ValueError as exc:
                    raise ValueError(f'路由节点 {router.name} 的 JSON 比较值无效') from exc
                expression['value'] = expected
            if value_type == 'object' and not isinstance(expected, dict):
                raise ValueError(f'路由节点 {router.name} 的对象条件值必须是 JSON 对象')
            if value_type == 'array' and not isinstance(expected, list):
                raise ValueError(f'路由节点 {router.name} 的数组条件值必须是 JSON 数组')

    for rule in rules:
        if not isinstance(rule, dict) or not str(rule.get('id') or '').strip():
            raise ValueError(f'路由节点 {router.name} 存在无效 CASE')
        visit(rule.get('expression'))
