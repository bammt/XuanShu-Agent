from typing import Literal
from pydantic import BaseModel, Field, model_validator

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

class CrewTaskDefinition(BaseModel):
    id: str; name: str; description: str; expected_output: str = '清晰、完整的最终结果'
    agent_id: str | None = None; depends_on: list[str] = Field(default_factory=list)
    output_variables: list[dict] = Field(default_factory=list); dependency_variables: dict[str, list[dict]] = Field(default_factory=dict)
    async_execution: bool = False; markdown: bool = False; output_file: str = ''; create_directory: bool = True
    guardrail: str = ''; guardrail_max_retries: int = 3

class TaskDefinition(BaseModel):
    id: str; name: str; description: str; expected_output: str = '清晰、完整的最终结果'
    agent_id: str | None = None; depends_on: list[str] = Field(default_factory=list)
    node_type: Literal['task', 'agent', 'crew', 'router', 'code'] = 'task'
    crew_agent_ids: list[str] = Field(default_factory=list); crew_tasks: list[CrewTaskDefinition] = Field(default_factory=list)
    crew_process: Literal['sequential', 'hierarchical'] = 'sequential'
    condition: str = ''; run_if: str = ''; routes: dict[str, list[str]] = Field(default_factory=dict)
    code_snippet: str = ''
    human_feedback: bool = False
    feedback_message: str = '请审核当前结果'
    feedback_outcomes: list[str] = Field(default_factory=lambda: ['approved', 'revise'])
    feedback_default_outcome: str | None = None
    output_variables: list[dict] = Field(default_factory=list); dependency_variables: dict[str, list[dict]] = Field(default_factory=dict)
    async_execution: bool = False; markdown: bool = False; output_file: str = ''; create_directory: bool = True
    guardrail: str = ''; guardrail_max_retries: int = 3

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
    process: Literal['sequential', 'hierarchical'] = 'sequential'
    agents: list[AgentDefinition]; tasks: list[TaskDefinition]; memory: bool = False
    inputs: list[InputDefinition] = Field(default_factory=list)
    interaction_mode: Literal['single_run', 'multi_turn'] = 'single_run'
    interaction: dict = Field(default_factory=dict)
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    model_profile_id: str | None = None
    planning: bool = False; manager_agent_id: str | None = None; manager_model_profile_id: str | None = None
    planning_model_profile_id: str | None = None; cache: bool = True
    output_log_file: str = ''; max_rpm: int | None = None; max_method_calls: int = 100
    @model_validator(mode='after')
    def validate_graph(self):
        if self.memory:
            self.memory_policy.long_term_semantic = True
        aids = {x.id for x in self.agents}; tids = {x.id for x in self.tasks}
        if len(aids) != len(self.agents) or len(tids) != len(self.tasks): raise ValueError('Agent 或任务 ID 重复')
        interactive_agents = [item for item in self.agents if item.user_interaction]
        if interactive_agents and self.interaction_mode != 'multi_turn':
            raise ValueError('ask_user 只能用于 multi_turn 应用，single_run 不允许开启 Agent 用户交互')
        if self.interaction_mode == 'multi_turn' and self.tasks and interactive_agents:
            collection_id = str(self.interaction.get('collection_task_id') or '').strip()
            collection = next((item for item in self.tasks if item.id == collection_id), None) if collection_id else None
            collection = collection or self.tasks[0]
            if collection.node_type == 'crew':
                raise ValueError('多轮 Flow 的信息收集节点必须是普通 Agent，后续 Crew 通过 depends_on 等待收集完成')
            if collection.node_type in {'task', 'agent'} and not collection.agent_id:
                raise ValueError('多轮 Flow 的信息收集节点必须绑定一个 Agent')
            if self.process == 'hierarchical':
                if not self.manager_agent_id:
                    raise ValueError('多轮层级 Crew 必须指定管理 Agent')
                if {item.id for item in interactive_agents} != {self.manager_agent_id}:
                    raise ValueError('多轮层级 Crew 只能由管理 Agent 启用 ask_user')
            elif collection.node_type in {'task', 'agent'}:
                collector = next((item for item in self.agents if item.id == collection.agent_id), None)
                if not collector or not collector.user_interaction:
                    raise ValueError('多轮应用的首个信息收集节点必须绑定启用了 ask_user 的 Agent')
        for task in self.tasks:
            if task.agent_id and task.agent_id not in aids: raise ValueError(f'任务 {task.name} 引用了不存在的 Agent')
            if task.node_type in {'task', 'agent'} and not task.agent_id and self.process != 'hierarchical': raise ValueError(f'任务 {task.name} 必须选择 Agent')
            if task.node_type == 'crew' and not task.crew_agent_ids: raise ValueError(f'Crew 节点 {task.name} 至少需要一个 Agent')
            if task.node_type == 'crew' and not task.crew_tasks: raise ValueError(f'Crew 节点 {task.name} 至少需要一个内部 Task')
            if task.node_type == 'crew' and task.crew_process == 'hierarchical':
                if len(task.crew_agent_ids) < 2:
                    raise ValueError(f'层级 Crew 节点 {task.name} 至少需要一个管理 Agent和一个执行 Agent')
                manager_id = task.crew_agent_ids[0]
                invalid_interactive = [
                    agent.id for agent in self.agents
                    if agent.id in task.crew_agent_ids and agent.user_interaction and agent.id != manager_id
                ]
                if invalid_interactive:
                    raise ValueError(f'层级 Crew 节点 {task.name} 只能由第一个管理 Agent 启用 ask_user')
            if any(x not in aids for x in task.crew_agent_ids): raise ValueError(f'Crew 节点 {task.name} 存在无效 Agent')
            if task.node_type == 'crew':
                nested_ids = {item.id for item in task.crew_tasks}
                if len(nested_ids) != len(task.crew_tasks): raise ValueError(f'Crew 节点 {task.name} 的内部 Task ID 重复')
                for nested in task.crew_tasks:
                    if nested.agent_id and nested.agent_id not in task.crew_agent_ids:
                        raise ValueError(f'Crew 节点 {task.name} 的内部 Task 引用了未加入 Crew 的 Agent')
                    if any(key not in nested_ids for key in nested.depends_on):
                        raise ValueError(f'Crew 节点 {task.name} 的内部 Task 存在无效依赖')
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
        return self
