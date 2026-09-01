import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from crewai import Agent, Crew, Process, Task
from crewai.flow.flow import Flow, start
from pydantic import BaseModel, Field
from .config import settings
from .db import Application
from .schemas import ApplicationDefinition
from .services import (
    app_root_dir, app_runtime_dir, application_execution_skill_roots, application_skill_roots,
    materialize_application_resources,
)
from .memory import persistent_memory
from .model_runtime import profile_llm, profile_llm_kwargs
from .tools.builtin import (AskUserRequestStore, AskUserTool, ExecutionReceiptStore, builtin_tools,
                            configured_capabilities, execution_idempotency_scope)
from .conversation import estimate_tokens
from .knowledge import build_knowledge
from .contracts import ensure_variable_contract, execution_order
from .state_machine import NodeStatus, RunStatus, RuntimeCheckpoint

def render(text:str,inputs:dict,outputs:dict)->str:
    for key,value in {**inputs,**outputs}.items(): text=text.replace('{'+key+'}',str(value))
    return text
def ordered_tasks(spec:ApplicationDefinition,outputs:dict):
    by_id = {item.id: item for item in spec.tasks}
    return [by_id[item_id] for item_id in execution_order(spec.model_dump(mode='json'))]
def should_enable_agent_reasoning(spec:ApplicationDefinition, agent_id:str)->bool:
    """Require both explicit approval and an actual multi-step assignment."""
    agent=next(item for item in spec.agents if item.id==agent_id)
    assigned=sum(1 for item in spec.tasks if item.agent_id==agent_id)
    return bool(agent.reasoning and assigned > 1)


def interactive_agent_ids(spec: ApplicationDefinition) -> set[str]:
    """Return only explicitly interactive Agents in a chat-capable app."""
    if spec.interaction_mode != 'multi_turn' or not spec.tasks:
        return set()
    return {str(item.id) for item in spec.agents if bool(item.user_interaction)}


def bound_ask_user_agent_ids(agents: dict[str, Agent]) -> set[str]:
    """Resolve interaction from materialized tools, not generated prompt text."""
    return {
        str(agent_id) for agent_id, agent in agents.items()
        if any(str(getattr(tool, 'name', '')) == 'ask_user'
               for tool in (getattr(agent, 'tools', None) or []))
    }


def interactive_input_schema(spec: ApplicationDefinition) -> dict[str, dict[str, Any]]:
    """Expose optional field hints without binding ``ask_user`` to run inputs."""
    return {
        str(item.name): {
            'input_type': item.input_type,
            'required': bool(item.required),
            'multiple': bool(item.multiple),
            'label': item.label or item.name,
        }
        for item in spec.inputs
    }


def build_runtime(row,spec,profiles,resources=None,execution_scope='legacy'):
    resources=resources or {}; shared=None
    selected_skill_ids={str(skill_id) for agent in spec.agents for skill_id in agent.skills}
    include_code=any(agent.allow_code_execution for agent in spec.agents)
    root=app_root_dir(row.workspace_id,row.id,row.kind)
    manifest=materialize_application_resources(
        root,resources.get('skills',{}),selected_skill_ids,include_code=include_code,refresh=False,
    )
    receipt_store=ExecutionReceiptStore()
    ask_user_agents=interactive_agent_ids(spec)
    # Keep one request bridge per Agent.  A shared bridge would let a request
    # from one interactive node be consumed by another node in the same run.
    ask_user_stores = {
        agent_id: AskUserRequestStore() for agent_id in ask_user_agents
    }
    ask_user_inputs = interactive_input_schema(spec)
    all_skill_roots=application_execution_skill_roots(
        root, sorted(selected_skill_ids, key=str), include_platform=False,
    )
    evidence={
        'receipts': receipt_store.items,
        'receipt_store': receipt_store,
        'skill_roots': all_skill_roots,
    }
    if spec.memory_policy.long_term_semantic or spec.memory or any(x.memory for x in spec.agents):
        path=app_runtime_dir(row.workspace_id,row.id,row.kind)/'memory'; path.mkdir(parents=True,exist_ok=True)
        default=(profiles or {}).get(str(spec.model_profile_id)) or (profiles or {}).get('default') or {}
        memory_llm=profile_llm(default, settings.openai_model)
        shared=persistent_memory(path,memory_llm,f'/workspace/{row.workspace_id}/app/{row.id}')
    agents={}
    for item in spec.agents:
        profile=(profiles or {}).get(str(item.model_profile_id or item.model_id)) or (profiles or {}).get(item.model_id) or (profiles or {}).get(str(spec.model_profile_id)) or (profiles or {}).get('default') or {}
        llm=profile_llm_kwargs(profile, settings.openai_model)
        # Never infer or silently enable a planner from prompt text. A confirmed
        # reasoning flag is ignored for a single bounded task, where the planner
        # only adds redundant plan/observe cycles.
        reasoning=should_enable_agent_reasoning(spec,item.id)
        skill_packages,skill_entries=application_skill_roots(
            root,[str(skill_id) for skill_id in item.skills],include_platform=item.allow_code_execution,
        )
        skill_roots=application_execution_skill_roots(
            root,[str(skill_id) for skill_id in item.skills],include_platform=False,
        )
        bound_entries={str(skill_id):skill_entries[str(skill_id)] for skill_id in item.skills if str(skill_id) in skill_entries}
        selected_plugins=[resources.get('plugins',{}).get(str(plugin_id)) for plugin_id in item.plugins]
        selected_knowledge=[resources.get('knowledge',{}).get(str(source_id)) for source_id in item.knowledge_base_ids]
        agent_knowledge=build_knowledge(row.workspace_id,[x for x in selected_knowledge if x])
        configured,mcps,apps=configured_capabilities(
            row.workspace_id,row.id,[x for x in selected_plugins if x],app_kind=row.kind,
            execution_scope=execution_scope,skill_roots=skill_roots,receipt_store=receipt_store,
        )
        agent_tools=builtin_tools(
            row.workspace_id,row.id,app_kind=row.kind,include_code=item.allow_code_execution,
            execution_scope=execution_scope,
            skill_entries=bound_entries,skill_roots=skill_roots,receipt_store=receipt_store,
            ask_user_store=ask_user_stores.get(str(item.id)),
            ask_user_inputs=ask_user_inputs,
        )+configured
        function_profile=(profiles or {}).get(str(item.function_calling_model_profile_id)) if item.function_calling_model_profile_id else None
        function_llm=None
        if function_profile:
            function_llm=profile_llm(function_profile)
        runtime_backstory = (
            interactive_agent_backstory(item.backstory)
            if str(item.id) in ask_user_agents else item.backstory
        )
        agents[item.id]=Agent(role=item.role,goal=item.goal,backstory=runtime_backstory,llm=llm,
            function_calling_llm=function_llm,tools=agent_tools,knowledge=agent_knowledge,
            skills=skill_packages or None,mcps=mcps or None,apps=apps or None,
            memory=shared.scope(f'/agent/{item.id}') if shared and item.memory else False,verbose=False,
            max_iter=item.max_iter,max_rpm=item.max_rpm,max_execution_time=item.max_execution_time,
            max_retry_limit=item.max_retry_limit,reasoning=reasoning,
            max_reasoning_attempts=(item.max_reasoning_attempts or 1) if reasoning else None,allow_delegation=item.allow_delegation,
            respect_context_window=item.respect_context_window,multimodal=item.multimodal,
            inject_date=item.inject_date,date_format=item.date_format,use_system_prompt=item.use_system_prompt)
    evidence['ask_user_stores'] = ask_user_stores
    # Compatibility for callers that only inspect whether the capability was
    # materialized; runtime execution uses the per-Agent mapping below.
    evidence['ask_user_store'] = next(iter(ask_user_stores.values()), None)
    return agents,shared,evidence


def agent_ask_user_store(stores, agent_id: str | None = None):
    """Resolve the request bridge for one Agent without widening its scope."""
    if isinstance(stores, dict):
        return stores.get(str(agent_id or ''))
    return stores
def event(item,agent,output,task_output=None):
    role=getattr(task_output,'agent',None) or getattr(agent,'role',None) or '由管理 Agent 分配'
    return {'type':'node.completed','node_id':item.id,'node_name':item.name,'agent_id':item.agent_id,'agent_role':role,'output':output}

def unique_events(events:list[dict[str,Any]])->list[dict[str,Any]]:
    """Keep one terminal node event when a retry replays persisted runtime state."""
    result=[]; seen=set()
    for item in events:
        if item.get('type') in {
            'node.started', 'node.completed', 'node.skipped',
            'run.waiting_input', 'approval.required',
        }:
            fingerprint=(item.get('type'),item.get('node_id'))
            if fingerprint in seen: continue
            seen.add(fingerprint)
        result.append(item)
    return result

PLANNER_FIELDS = (
    {'plan', 'steps', 'ready'},
    {'goal_already_achieved', 'remaining_plan_still_valid', 'suggested_refinements'},
)
COLLECTION_COMPLETE_MARKER = '<XUANSHU_COLLECTION_COMPLETE>'
LOCAL_ARTIFACT_LINK = re.compile(
    r'\[[^\]\r\n]*\]\(\s*<?(?:file://)?/var/lib/xuanshu/workspaces/[^\r\n>)]*>?\s*\)'
)


def strip_local_artifact_references(value: Any) -> str:
    """Remove host paths that are represented by separate MinIO file objects."""
    text = LOCAL_ARTIFACT_LINK.sub('', str(value or ''))
    text = re.sub(r'(?m)^[ \t]*(?:[-*]\s*)?$', '', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def user_visible_output(value: Any) -> str:
    """Remove CrewAI planner envelopes while preserving the agent's answer."""
    text = str(value or '')
    decoder = json.JSONDecoder()
    parts: list[str] = []
    cursor = 0
    while cursor < len(text):
        opening = text.find('{', cursor)
        if opening < 0:
            parts.append(text[cursor:])
            break
        parts.append(text[cursor:opening])
        try:
            document, consumed = decoder.raw_decode(text[opening:])
        except json.JSONDecodeError:
            parts.append(text[opening])
            cursor = opening + 1
            continue
        if isinstance(document, dict):
            if any(fields <= document.keys() for fields in PLANNER_FIELDS):
                cursor = opening + consumed
                continue
        parts.append(text[opening:opening + consumed])
        cursor = opening + consumed
    return strip_local_artifact_references(''.join(parts))

def normalize_workspace_paths(text: str) -> str:
    return re.sub(
        r'(?<![\w$])/workspace(?:/([^\s，。；：,;]+))?',
        lambda match: ('$XUANSHU_WORKSPACE/' + match.group(1)) if match.group(1) else '$XUANSHU_WORKSPACE',
        text,
    )


def task_description(item, rendered: str, prior: str = '', expected_output: str | None = None) -> str:
    rendered = normalize_workspace_paths(rendered)
    upstream=f'\n\n已完成的上游结果：\n{prior}' if prior else ''
    output_requirement = normalize_workspace_paths(expected_output if expected_output is not None else item.expected_output)
    return (f'{rendered}{upstream}\n\n输出要求：{output_requirement}\n'
            '只返回面向用户的最终结果。不要输出 reasoning plan、复盘状态、JSON 控制对象或重复执行说明。'
            '生成文件由平台作为下载对象展示，不要输出 /var/lib/xuanshu/workspaces 本地路径，'
            '也不要为本地文件构造 Markdown 下载链接。代码中必须从环境变量 XUANSHU_WORKSPACE 读取工作目录，'
            '不要硬编码 /workspace 或宿主机绝对路径。')


def node_artifacts_from_receipts(receipts: list[dict[str, Any]], offset: int) -> list[str]:
    """Observe files actually created by the current node without requiring one."""
    produced = []
    for receipt in receipts[offset:]:
        if receipt.get('exit_code') != 0:
            continue
        produced.extend(str(path) for path in receipt.get('files', []) if str(path).strip())
    return list(dict.fromkeys(produced))


def dependency_context(item, outputs: dict, node_artifacts: dict[str, list[str]]) -> str:
    """Pass upstream text and relative artifact names without exposing host paths."""
    parts = []
    for dependency in item.depends_on:
        text = str(outputs.get(dependency, '') or '').strip()
        files = [str(name) for name in node_artifacts.get(dependency, []) if str(name).strip()]
        detail = f'{dependency}: {text}'
        if files:
            detail += (
                '\n该上游节点实际交付的工作目录文件：' + '、'.join(files)
                + '。需要核验内容时必须使用这些准确的相对文件名，不要自行猜测文件名。'
            )
        parts.append(detail)
    return '\n\n'.join(parts)


def conversation_history_context(spec: ApplicationDefinition, inputs: dict) -> str:
    """Make persisted conversation context visible to the first task prompt.

    Conversation history is a session concern, not an interaction-mode
    concern.  A published single-run Crew can still receive several messages
    in one persistent conversation, so restricting this helper to Flow apps
    made later turns look stateless.
    """
    if not spec.memory_policy.conversation_history:
        return ''
    history = inputs.get('conversation_history') or []
    if not history:
        return ''
    lines = []
    for item in history:
        if isinstance(item, dict) and item.get('summary'):
            lines.append(f"历史摘要：{item['summary']}")
            continue
        if not isinstance(item, dict):
            continue
        user = str(item.get('user') or '').strip()
        assistant = str(item.get('assistant') or '').strip()
        if user or assistant:
            lines.append(f'用户：{user}\n助手：{assistant}')
    if not lines:
        return ''
    return ('\n\n平台提供的同一会话历史（仅用于理解上下文，不要把它当作新的运行输入）：\n'
            + '\n\n'.join(lines))


def current_turn_context(inputs: dict) -> str:
    """Describe the reply resuming an interactive node without mutating fields."""
    message = str(inputs.get('message') or '').strip()
    files = [str(item) for item in (inputs.get('files') or []) if str(item).strip()]
    if not message and not files:
        return '本轮用户未提供文本或附件。'
    parts = [f'本轮用户消息：{message}' if message else '本轮用户未提供文本。']
    if files:
        parts.append('当前会话已可用的附件：' + '、'.join(files))
    return '\n'.join(parts)


def ask_user_contract_guidance(allowed_inputs: dict[str, dict[str, Any]]) -> str:
    declared = [
        {
            'input_name': name,
            'input_type': item.get('input_type', 'text'),
            'label': item.get('label') or name,
            'required': bool(item.get('required')),
        }
        for name, item in allowed_inputs.items()
    ]
    return (
        '当前应用允许 ask_user 使用的运行输入契约如下：'
        f'{json.dumps(declared, ensure_ascii=False)}。'
        'ask_user.input_name 只能从上面 input_name 的值中原样选择；'
        'text、long_text、file、image、number、boolean、json 是 input_type，绝不能填入 input_name。'
        '只有问题明确要求用户补充某个已声明字段时才填写 input_name，并同时填写对应 input_type；'
        '询问范围、偏好、阈值、确认意见等通用业务信息时必须省略 input_name。'
    )


def interactive_agent_backstory(backstory: str) -> str:
    """Put the generic interaction contract in CrewAI's Agent context."""
    return (
        '【平台强制交互协议｜最高优先级】\n'
        '你已绑定 ask_user 工具，负责在执行工作前收集并确认用户信息。'
        '只要当前节点还需要从用户处收集任何其他信息，就必须调用 ask_user；'
        '绝对禁止把问题、待确认项或“请补充……”作为普通文本、Final Answer 或节点业务结果输出。'
        '调用 ask_user 后必须立即停止当前节点，等待平台恢复。'
        f'只有信息已经完整且无歧义时，才允许生成业务结果，并在结果第一行输出精确标记 '
        f'{COLLECTION_COMPLETE_MARKER}；该标记由平台移除。\n'
        '【原始 Agent 背景】\n'
        f'{backstory}'
    )


def interactive_agent_prompt(prompt: str, inputs: dict,
                             allowed_inputs: dict[str, dict[str, Any]]) -> str:
    """Inject the platform pause contract for an Agent bound to ``ask_user``."""
    return (
        '【平台强制交互协议｜当前节点是信息收集节点｜优先于下方所有业务要求】\n'
        '本节点必须先核对本轮消息、会话历史、附件和上游结果是否足以完成业务。'
        '只要当前节点还需要从用户处收集任何其他信息，就必须调用 ask_user 工具；'
        '向用户提问不能直接输出文本，不能作为 Final Answer、expected_output 或节点结果返回。'
        '即使下方要求“只返回最终结果”或描述了结构化交付物，信息未收集完整时也不得提前交付、'
        '不得采用默认值补齐、不得继续执行下游工作。调用 ask_user 后立即停止当前节点并等待用户回复。'
        '只有所需信息完整且无歧义时，才可跳过 ask_user 并完成业务输出；此时必须在业务结果第一行'
        f'输出精确标记 {COLLECTION_COMPLETE_MARKER}。平台只有检测到该标记才允许进入下一节点，'
        '并会在展示及传递结果前移除标记。\n'
        f'{ask_user_contract_guidance(allowed_inputs)}'
        '\n\n【当前轮次信息】\n'
        f'{current_turn_context(inputs)}\n\n'
        '【当前节点业务任务】\n'
        f'{prompt}\n\n'
        '【进入下一节点前强制检查】只要还需要从用户处收集任何其他信息，就必须调用 ask_user；'
        f'只有信息收集已经完成并输出 {COLLECTION_COMPLETE_MARKER}，平台才允许进入下一节点。'
    )


def completed_collection_output(raw: str) -> str:
    """Require an explicit completion signal before leaving an interactive node."""
    text = str(raw or '').strip()
    if not text.startswith(COLLECTION_COMPLETE_MARKER):
        raise RuntimeError(
            '信息收集节点未调用 ask_user，也未声明信息收集完成；为避免在信息不完整时执行下游，'
            '本次运行已停止。'
        )
    result = text[len(COLLECTION_COMPLETE_MARKER):].lstrip(' \t\r\n:：-')
    if not result:
        raise RuntimeError('信息收集节点声明完成但没有返回可传递的业务结果，本次运行已停止。')
    return result


def ask_user_failure_event(request_store: AskUserRequestStore | None, node_id: str,
                           node_name: str) -> dict[str, Any] | None:
    failure = request_store.consume_failure() if request_store else None
    if not failure:
        return None
    return {
        'type': 'tool.failed',
        'node_id': str(node_id),
        'node_name': str(node_name),
        'tool_name': failure.get('tool_name') or 'ask_user',
        'arguments': failure.get('arguments') or {},
        'error': failure.get('error') or '工具调用失败',
    }


def normalize_legacy_interaction_switch(definition: dict) -> dict:
    """Normalize a definition without inferring user interaction.

    ``user_interaction`` is an explicit per-Agent capability.  Older
    multi-turn definitions that do not contain the field must remain ordinary
    runs until an operator enables the switch; silently attaching ``ask_user``
    to their first Agent was the source of the old whole-run binding bug.
    """
    result = json.loads(json.dumps(definition or {}, ensure_ascii=False))
    return result

def task_options(row: Application, item) -> dict:
    options = {
        'markdown': bool(item.markdown),
        'async_execution': bool(getattr(item, 'async_execution', False)),
        'guardrail_max_retries': int(getattr(item, 'guardrail_max_retries', 3)),
    }
    if item.guardrail:
        options['guardrail'] = item.guardrail
    # CrewAI's output_file persists the task's text response. Business files
    # are created by the isolated execution tools and registered separately.
    # In CrewAI 1.15 an absolute output_file is stripped to a relative path,
    # which would turn /var/lib/... into /app/var/lib/... inside the worker.
    return options

def mapped_task_inputs(item, inputs: dict, outputs: dict,
                       node_artifacts: dict[str, list[str]] | None = None,
                       task_by_id: dict[str, Any] | None = None) -> dict:
    values = dict(inputs)
    node_artifacts = node_artifacts or {}
    task_by_id = task_by_id or {}
    for dependency, mappings in getattr(item, 'dependency_variables', {}).items():
        raw = outputs.get(dependency, '')
        parsed = None
        for mapping in mappings:
            source = mapping.get('source_variable', 'result')
            target = mapping.get('target_variable', 'context')
            value = raw
            dependency_task = task_by_id.get(str(dependency))
            output_fields = getattr(dependency_task, 'output_variables', []) if dependency_task else []
            source_type = next(
                (field.get('value_type') for field in output_fields if field.get('name') == source),
                None,
            )
            if source_type == 'file':
                value = list(node_artifacts.get(str(dependency), []))
            elif source not in {'result', '$raw'}:
                if parsed is None:
                    try: parsed = json.loads(raw)
                    except (TypeError, json.JSONDecodeError): parsed = {}
                value = parsed.get(source, '') if isinstance(parsed, dict) else ''
            values[target] = value
    return values

def should_run(item, outputs: dict, runtime_state: dict) -> bool:
    expected = str(getattr(item, 'run_if', '') or '').strip()
    if not expected:
        return True
    candidates = [str(outputs.get(dependency, '')).strip() for dependency in item.depends_on]
    decision = runtime_state.get('decision') or {}
    if decision.get('outcome') and runtime_state.get('pending_node') in item.depends_on:
        candidates.append(str(decision['outcome']).strip())
    return expected in candidates


def prepare_feedback_resume(
    checkpoint: RuntimeCheckpoint,
    runtime_state: dict,
    outputs: dict,
) -> tuple[str, str]:
    """Re-open the approved node when Flow human feedback asks for changes.

    An approval resumes after a completed node. A human-feedback revision is
    different: the current node must become runnable again, and its old output
    must not be passed to downstream nodes or reused by executor idempotency.
    """
    decision = dict(runtime_state.get('decision') or {})
    outcome = str(decision.get('outcome') or '').strip().lower()
    if not decision or outcome in {'approved', 'approve', 'accepted', 'accept'}:
        return '', ''
    pending_node = str(
        runtime_state.get('pending_node')
        or checkpoint.current_node
        or (checkpoint.waiting_approval or {}).get('node_id')
        or ''
    ).strip()
    if not pending_node:
        return '', ''
    node = checkpoint.node(pending_node)
    node.status = NodeStatus.PENDING
    node.output = ''
    node.error = ''
    node.completed_at = None
    checkpoint.outputs.pop(pending_node, None)
    checkpoint.waiting_approval = None
    checkpoint.current_node = None
    outputs.pop(pending_node, None)
    feedback = str(decision.get('feedback') or '').strip()
    return pending_node, feedback or '用户要求重新生成当前步骤。'

def final_output(ordered, outputs: dict) -> str:
    return next((str(outputs[item.id]) for item in reversed(ordered) if outputs.get(item.id)), '')

def crew_options(spec, agents: dict, profiles: dict) -> tuple[list, dict]:
    process=Process.hierarchical if spec.process=='hierarchical' else Process.sequential
    participants=list(agents.values())
    kwargs={'process':process,'memory':False,'verbose':False,'cache':spec.cache,
            'max_rpm':spec.max_rpm,
            'output_log_file':spec.output_log_file or None}
    if process==Process.hierarchical:
        if spec.manager_agent_id:
            manager=agents[spec.manager_agent_id]
            participants=[agent for key,agent in agents.items() if key!=spec.manager_agent_id]
            kwargs['manager_agent']=manager
        else:
            manager_profile=(profiles or {}).get(str(spec.manager_model_profile_id)) or (profiles or {}).get('default') or {}
            kwargs['manager_llm']=(profile_llm(manager_profile)
                                   if manager_profile.get('model') else next(iter(agents.values())).llm)
    if spec.planning:
        planning_profile=(profiles or {}).get(str(spec.planning_model_profile_id)) or (profiles or {}).get('default') or {}
        kwargs.update({'planning':True,'planning_llm':profile_llm(planning_profile, settings.openai_model)})
    return participants,kwargs

RuntimeEventCallback = Callable[[dict[str, Any]], None]

def emit_runtime_event(callback: RuntimeEventCallback | None, item: dict[str, Any]) -> None:
    item.setdefault('at', datetime.now(UTC).isoformat())
    if callback:
        callback(item)

def execute_crew(row,spec,agents,shared,inputs,outputs,profiles,runtime_state,
                 ask_user_store: AskUserRequestStore | dict[str, AskUserRequestStore] | None = None,
                 receipt_store: ExecutionReceiptStore | None = None,
                 event_callback:RuntimeEventCallback|None=None,
                 _single_interactive_task: bool = False):
    checkpoint = RuntimeCheckpoint.from_resume(runtime_state)
    checkpoint.history_summary = str(runtime_state.get('history_summary') or '')
    checkpoint.history_tokens = int(runtime_state.get('history_tokens') or 0)
    feedback_node, feedback = prepare_feedback_resume(checkpoint, runtime_state, outputs)
    outputs.update(checkpoint.outputs)
    node_artifacts = {
        str(node_id): [str(name) for name in names]
        for node_id, names in dict(runtime_state.get('node_artifacts') or {}).items()
        if isinstance(names, list)
    }
    ordered=ordered_tasks(spec,outputs)
    task_by_id = {task.id: task for task in ordered}
    ask_user_agents = bound_ask_user_agent_ids(agents)
    events=[]
    execution_id = str(runtime_state.get('execution_id') or runtime_state.get('run_id') or 'run')
    for item in ordered:
        if checkpoint.completed(item.id):
            continue
        if not should_run(item, outputs, runtime_state):
            outputs[item.id] = ''
            checkpoint.node(item.id).status = NodeStatus.SKIPPED
            checkpoint.outputs[item.id] = ''
            skipped={'type':'node.skipped','node_id':item.id,'node_name':item.name,
                     'run_if':item.run_if,'checkpoint':checkpoint.dump()}
            events.append(skipped); emit_runtime_event(event_callback, skipped)
            continue
        prior=dependency_context(item, outputs, node_artifacts)
        mapped_inputs=mapped_task_inputs(item,inputs,outputs,node_artifacts,task_by_id)
        description=task_description(
            item, render(item.description,mapped_inputs,outputs), prior,
            render(item.expected_output, mapped_inputs, outputs),
        )
        history_context = conversation_history_context(spec, inputs)
        if history_context and not item.depends_on and '{conversation_history}' not in item.description:
            description = history_context + '\n\n' + description
        if feedback and feedback_node == item.id:
            description += f'\n\n用户对上一版的修改意见：{feedback}\n请按该意见重新生成当前步骤，并保留原任务目标。'
        assigned=agents.get(item.agent_id)
        interactive_owner = (
            str(spec.manager_agent_id or '')
            if spec.process == 'hierarchical' and spec.manager_agent_id
            else str(item.agent_id or '')
        )
        if interactive_owner in ask_user_agents:
            description = interactive_agent_prompt(
                description, inputs, interactive_input_schema(spec),
            )
        checkpoint_inputs = dict(mapped_inputs)
        if feedback and feedback_node == item.id:
            checkpoint_inputs['_human_feedback'] = feedback
        node_checkpoint = checkpoint.start_node(item.id, checkpoint_inputs, {
            dependency: outputs.get(dependency, '') for dependency in item.depends_on
        })
        idempotency_key = f'{execution_id}:{item.id}:{node_checkpoint.input_hash}'
        started = {'type':'node.started','node_id':item.id,'node_name':item.name,
                   'attempt':node_checkpoint.attempt,'idempotency_key':idempotency_key,
                   'checkpoint':checkpoint.dump()}
        events.append(started); emit_runtime_event(event_callback, started)
        task=Task(name=item.name,description=description,expected_output=render(item.expected_output, mapped_inputs, outputs),
                  agent=assigned,context=None,**task_options(row,item))
        participants,kwargs=crew_options(spec,agents,profiles)
        kwargs['memory']=shared if spec.memory_policy.long_term_semantic else False
        receipt_offset = len(receipt_store.items) if receipt_store is not None else 0
        with execution_idempotency_scope(idempotency_key):
            result=Crew(agents=participants,tasks=[task],**kwargs).kickoff()
        request_store = agent_ask_user_store(ask_user_store, interactive_owner)
        waiting_input = request_store.consume() if request_store else None
        if not waiting_input and request_store:
            failure = ask_user_failure_event(request_store, item.id, item.name)
            if failure:
                events.append(failure)
                emit_runtime_event(event_callback, failure)
                raise RuntimeError(str(failure['error']))
        if waiting_input:
            checkpoint.pause_for_input(item.id, waiting_input)
            paused={'type':'run.waiting_input','node_id':item.id,'node_name':item.name,
                    'question':waiting_input['question'],'waiting_input':waiting_input,
                    'checkpoint':checkpoint.dump()}
            events.append(paused); emit_runtime_event(event_callback,paused)
            return {'status':'waiting_input','output':waiting_input['question'],'outputs':outputs,'events':events,
                    'pending_node':item.id,'waiting_input':waiting_input,
                    'node_artifacts': node_artifacts, 'checkpoint':checkpoint.dump()}
        task_outputs=list(getattr(result,'tasks_output',[]) or [])
        task_output = task_outputs[-1] if task_outputs else result
        raw=user_visible_output(getattr(task_output,'raw',task_output))
        if interactive_owner in ask_user_agents:
            raw = completed_collection_output(raw)
        delivered = node_artifacts_from_receipts(
            receipt_store.items if receipt_store is not None else [], receipt_offset,
        )
        outputs[item.id]=raw
        checkpoint.complete_node(item.id, raw)
        completed_event = event(item,agents.get(item.agent_id),raw,task_output)
        inherited = list(dict.fromkeys(
            name for dependency in item.depends_on
            for name in node_artifacts.get(str(dependency), [])
        ))
        carried_files = list(dict.fromkeys([*inherited, *delivered]))
        if carried_files:
            completed_event['files'] = carried_files
            node_artifacts[item.id] = carried_files
        completed_event['checkpoint'] = checkpoint.dump()
        events.append(completed_event)
        emit_runtime_event(event_callback, completed_event)
        # Crew tasks never pause for platform approval. Conversational input
        # belongs to an explicitly enabled Agent's ``ask_user`` capability;
        # stepwise review belongs to Flow's ``human_feedback`` nodes below.
    checkpoint.finish(final_output(ordered,outputs))
    return {'status':'completed','output':final_output(ordered,outputs),'outputs':outputs,'events':events,
            'node_artifacts': node_artifacts,
            'checkpoint': checkpoint.dump()}

class RuntimeState(BaseModel):
    outputs:dict[str,str]=Field(default_factory=dict)
    events:list[dict[str,Any]]=Field(default_factory=list)
    status:str='running'
    output:str=''
    pending_node:str|None=None
    waiting_input:dict[str,Any]|None=None
    checkpoint:dict[str,Any]=Field(default_factory=dict)
    history_summary:str=''
    history_tokens:int=0
def route_value(condition:str,text:str)->str:
    condition=(condition or '').strip()
    if condition.startswith('contains:'): return 'true' if condition[9:].strip().lower() in text.lower() else 'false'
    if condition.startswith('equals:'): return 'true' if text.strip()==condition[7:].strip() else 'false'
    match=re.match(r'^regex:(.+)$',condition)
    if match: return 'true' if re.search(match.group(1),text) else 'false'
    return text.strip()

def execute_flow_crew(row,item,agents,node_prompt,inputs,outputs,
                      ask_user_store: AskUserRequestStore | dict[str, AskUserRequestStore] | None = None):
    hierarchical = item.crew_process == 'hierarchical'
    manager_id = str(item.crew_agent_ids[0]) if hierarchical else ''
    participants=[agents[x] for x in item.crew_agent_ids if str(x) != manager_id]
    native={}; tasks=[]
    nested_by_id = {spec.id: spec for spec in item.crew_tasks}
    nested_order = execution_order({'tasks': [spec.model_dump(mode='json') for spec in item.crew_tasks]})
    for nested_id in nested_order:
        spec = nested_by_id[nested_id]
        context=[native[x] for x in spec.depends_on if x in native]
        nested_inputs = dict(inputs)
        nested_inputs.update(outputs)
        # CrewAI resolves internal dependencies through Task.context, after the
        # dependent Task has already been constructed. Replace declared mapping
        # targets with an explicit context reference so placeholders never leak
        # into kickoff while the actual upstream output still arrives natively.
        for dependency_id, raw_mappings in spec.dependency_variables.items():
            for mapping in raw_mappings:
                source = str(mapping.get('source_variable') or 'result')
                target = str(mapping.get('target_variable') or '').strip()
                if not target:
                    continue
                nested_inputs[target] = (
                    f'内部上游任务“{dependency_id}”的完整 context 输出'
                    if source in {'result', '$raw'}
                    else f'内部上游任务“{dependency_id}”context 输出中的“{source}”字段'
                )
        rendered = render(spec.description, nested_inputs, {})
        task=Task(name=spec.name,
                  description=f'Crew 节点目标：\n{node_prompt}\n\n当前内部任务：\n{rendered}',
                  expected_output=render(spec.expected_output, nested_inputs, {}),
                  agent=(None if hierarchical else agents[spec.agent_id] if spec.agent_id else participants[0]),
                  context=context or None,
                  **task_options(row,spec))
        native[spec.id]=task; tasks.append(task)
    process=Process.hierarchical if hierarchical else Process.sequential
    kwargs={'manager_agent': agents[manager_id]} if hierarchical else {}
    result=Crew(agents=participants,tasks=tasks,process=process,verbose=False,**kwargs).kickoff()
    for agent_id in item.crew_agent_ids:
        request_store = agent_ask_user_store(ask_user_store, agent_id)
        if request_store:
            failure = ask_user_failure_event(request_store, item.id, item.name)
            if failure:
                return '', None, failure
            if request_store.request:
                return '', request_store.consume(), None
    return user_visible_output(result.raw), None, None

def execute_flow(
    row,
    spec,
    agents,
    inputs,
    outputs,
    runtime_state,
    event_callback:RuntimeEventCallback|None=None,
    skill_roots: list[str] | None = None,
    ask_user_store: AskUserRequestStore | dict[str, AskUserRequestStore] | None = None,
    receipt_store: ExecutionReceiptStore | None = None,
):
    checkpoint = RuntimeCheckpoint.from_resume(runtime_state)
    checkpoint.history_summary = str(runtime_state.get('history_summary') or '')
    checkpoint.history_tokens = int(runtime_state.get('history_tokens') or 0)
    feedback_node, feedback = prepare_feedback_resume(checkpoint, runtime_state, outputs)
    outputs.update(checkpoint.outputs)
    node_artifacts = {
        str(node_id): [str(name) for name in names]
        for node_id, names in dict(runtime_state.get('node_artifacts') or {}).items()
        if isinstance(names, list)
    }
    ordered=ordered_tasks(spec,outputs)
    task_by_id = {task.id: task for task in ordered}
    if len(ordered)>spec.max_method_calls: raise ValueError(f'Flow 节点数超过最大方法调用次数 {spec.max_method_calls}')
    collection_task_id = None
    ask_user_agents = bound_ask_user_agent_ids(agents)
    if spec.interaction_mode == 'multi_turn' and ordered:
        configured_collection = str((spec.interaction or {}).get('collection_task_id') or '').strip()
        candidate = configured_collection if configured_collection in {x.id for x in ordered} else ordered[0].id
        candidate_task = next((x for x in ordered if x.id == candidate), None)
        if candidate_task and str(candidate_task.agent_id or '') in ask_user_agents:
            collection_task_id = candidate

    def collection_prompt(prompt: str) -> str:
        return interactive_agent_prompt(prompt, inputs, interactive_input_schema(spec))

    class ApplicationFlow(Flow[RuntimeState]):
        @start()
        def execute_graph(self):
            self.state.outputs.update(outputs)
            for item in ordered:
                if checkpoint.completed(item.id):
                    self.state.outputs[item.id] = checkpoint.outputs.get(item.id, self.state.outputs.get(item.id, ''))
                    continue
                if not should_run(item,self.state.outputs,runtime_state):
                    self.state.outputs[item.id]=''
                    checkpoint.node(item.id).status = NodeStatus.SKIPPED
                    checkpoint.outputs[item.id] = ''
                    skipped={'type':'node.skipped','node_id':item.id,'node_name':item.name,'run_if':item.run_if}
                    skipped['checkpoint'] = checkpoint.dump()
                    self.state.events.append(skipped);emit_runtime_event(event_callback,skipped);continue
                context=dependency_context(item, self.state.outputs, node_artifacts)
                mapped_inputs=mapped_task_inputs(
                    item,inputs,self.state.outputs,node_artifacts,task_by_id,
                )
                prompt=task_description(
                    item, render(item.description,mapped_inputs,self.state.outputs), context,
                    render(item.expected_output, mapped_inputs, self.state.outputs),
                )
                history_context = conversation_history_context(spec, inputs)
                if history_context and not item.depends_on and '{conversation_history}' not in item.description:
                    prompt = history_context + '\n\n' + prompt
                if feedback and feedback_node == item.id:
                    prompt += f'\n\n用户对上一版的修改意见：{feedback}\n请按该意见重新生成当前步骤，并保留原任务目标。'
                if item.id == collection_task_id:
                    prompt = collection_prompt(prompt)
                elif (
                    str(item.agent_id or '') in ask_user_agents
                    or any(str(agent_id) in ask_user_agents for agent_id in item.crew_agent_ids)
                ):
                    prompt = interactive_agent_prompt(
                        prompt, inputs, interactive_input_schema(spec),
                    )
                interactive_node = (
                    item.id == collection_task_id
                    or str(item.agent_id or '') in ask_user_agents
                    or any(str(agent_id) in ask_user_agents for agent_id in item.crew_agent_ids)
                )
                checkpoint_inputs = dict(mapped_inputs)
                if feedback and feedback_node == item.id:
                    checkpoint_inputs['_human_feedback'] = feedback
                node_checkpoint = checkpoint.start_node(item.id, checkpoint_inputs, {
                    dependency: self.state.outputs.get(dependency, '') for dependency in item.depends_on
                })
                execution_id = str(runtime_state.get('execution_id') or runtime_state.get('run_id') or 'run')
                idempotency_key = f'{execution_id}:{item.id}:{node_checkpoint.input_hash}'
                started={'type':'node.started','node_id':item.id,'node_name':item.name,
                         'attempt':node_checkpoint.attempt,'idempotency_key':idempotency_key,
                         'checkpoint':checkpoint.dump()}
                self.state.events.append(started); emit_runtime_event(event_callback,started)
                receipt_offset = len(receipt_store.items) if receipt_store is not None else 0
                if item.node_type=='router':
                    raw=route_value(item.condition,context or prompt); agent_role='条件路由'
                elif item.node_type=='code':
                    tool=next(tool for tool in builtin_tools(
                        row.workspace_id,row.id,app_kind=row.kind,skill_roots=skill_roots or [],
                        execution_scope=str(runtime_state.get('execution_scope') or 'legacy'),
                        receipt_store=receipt_store,
                    ) if tool.name=='execute_python')
                    code=render(item.code_snippet,inputs,self.state.outputs)
                    if hasattr(tool,'execute'):
                        with execution_idempotency_scope(idempotency_key):
                            execution=tool.execute(code)
                        if execution.get('exit_code') != 0:
                            raise RuntimeError(execution.get('stderr') or '隔离代码执行失败')
                        raw=str(execution.get('stdout') or '').strip()
                    else:
                        raw=tool.run(code=code)
                    agent_role='隔离代码执行器'
                elif item.node_type=='crew':
                    with execution_idempotency_scope(idempotency_key):
                        raw, waiting_input, tool_failure = execute_flow_crew(
                            row,item,agents,prompt,mapped_inputs,self.state.outputs,ask_user_store,
                        )
                    if tool_failure:
                        self.state.events.append(tool_failure)
                        emit_runtime_event(event_callback, tool_failure)
                        raise RuntimeError(str(tool_failure['error']))
                    agent_role='Crew ('+', '.join(agents[x].role for x in item.crew_agent_ids)+')'
                    if waiting_input:
                        checkpoint.pause_for_input(item.id, waiting_input)
                        self.state.status='waiting_input'; self.state.output=waiting_input['question']; self.state.pending_node=item.id
                        self.state.waiting_input=waiting_input
                        paused={'type':'run.waiting_input','node_id':item.id,'node_name':item.name,
                                'question':waiting_input['question'],'waiting_input':waiting_input}
                        paused['checkpoint'] = checkpoint.dump()
                        self.state.events.append(paused); emit_runtime_event(event_callback,paused); return self.state.output
                else:
                    with execution_idempotency_scope(idempotency_key):
                        result=agents[item.agent_id].kickoff(prompt)
                    raw = user_visible_output(result.raw)
                    request_store = agent_ask_user_store(ask_user_store, item.agent_id)
                    waiting_input = request_store.consume() if request_store else None
                    if not waiting_input and request_store:
                        failure = ask_user_failure_event(request_store, item.id, item.name)
                        if failure:
                            self.state.events.append(failure)
                            emit_runtime_event(event_callback, failure)
                            raise RuntimeError(str(failure['error']))
                    if waiting_input:
                        checkpoint.pause_for_input(item.id, waiting_input)
                        self.state.status='waiting_input'; self.state.output=waiting_input['question']; self.state.pending_node=item.id
                        self.state.waiting_input=waiting_input
                        paused={'type':'run.waiting_input','node_id':item.id,'node_name':item.name,
                                'question':waiting_input['question'],'waiting_input':waiting_input}
                        paused['checkpoint'] = checkpoint.dump()
                        self.state.events.append(paused); emit_runtime_event(event_callback,paused); return self.state.output
                    agent_role=agents[item.agent_id].role
                if interactive_node:
                    raw = completed_collection_output(raw)
                delivered = node_artifacts_from_receipts(
                    receipt_store.items if receipt_store is not None else [], receipt_offset,
                )
                self.state.outputs[item.id]=raw
                checkpoint.complete_node(item.id, raw)
                completed={'type':'node.completed','node_id':item.id,'node_name':item.name,'agent_id':item.agent_id,'agent_role':agent_role,'node_type':item.node_type,'output':raw,
                           'checkpoint':checkpoint.dump()}
                inherited = list(dict.fromkeys(
                    name for dependency in item.depends_on
                    for name in node_artifacts.get(str(dependency), [])
                ))
                carried_files = list(dict.fromkeys([*inherited, *delivered]))
                if carried_files:
                    completed['files'] = carried_files
                    node_artifacts[item.id] = carried_files
                self.state.events.append(completed); emit_runtime_event(event_callback,completed)
                if item.human_feedback:
                    message = item.feedback_message
                    outcomes = item.feedback_outcomes
                    required = {'type':'approval.required','node_id':item.id,'node_name':item.name,
                                'message':message,'output':raw,'outcomes':outcomes,
                                'default_outcome':item.feedback_default_outcome,
                                'resume_any_outcome':True}
                    checkpoint.pause_for_approval(item.id, required)
                    required['checkpoint'] = checkpoint.dump()
                    self.state.status='waiting_approval'; self.state.output=raw; self.state.pending_node=item.id
                    self.state.events.append(required); emit_runtime_event(event_callback, required); return raw
            checkpoint.finish(final_output(ordered,self.state.outputs))
            self.state.status='completed'; self.state.output=final_output(ordered,self.state.outputs); return self.state.output
    flow=ApplicationFlow(); flow.kickoff(); return {'status':flow.state.status,'output':flow.state.output,'outputs':flow.state.outputs,'events':flow.state.events,
        'waiting_input': flow.state.waiting_input,
        'node_artifacts': node_artifacts,
        'checkpoint': checkpoint.dump(),
        **({'pending_node':flow.state.pending_node} if flow.state.pending_node else {})}

def execute_application(row:Application,message:str,files:list[str],resume:dict|None=None,model_profiles:dict|None=None,definition:dict|None=None,resources:dict|None=None,event_callback:RuntimeEventCallback|None=None):
    if definition is None:
        raise ValueError('运行应用必须提供数据库中的关系化编排定义')
    definition = normalize_legacy_interaction_switch(definition)
    ensure_variable_contract(definition)
    spec=ApplicationDefinition.model_validate(definition)
    runtime_state=resume or {}
    execution_scope=str(runtime_state.get('execution_scope') or runtime_state.get('conversation_id')
                        or runtime_state.get('execution_id') or f'application-{row.id}')
    runtime_state['execution_scope']=execution_scope
    outputs=dict(runtime_state.get('outputs',{}))
    agents,shared,evidence=build_runtime(
        row,spec,model_profiles or {},resources,execution_scope=execution_scope,
    )
    ask_user_store = evidence.get('ask_user_stores') or evidence.get('ask_user_store')
    evidence['receipts'].extend(runtime_state.get('skill_execution_receipts') or [])
    history = runtime_state.get('conversation_history', []) if spec.memory_policy.conversation_history else []
    summary_item = next((item for item in history if isinstance(item, dict) and item.get('summary')), {})
    runtime_state['history_summary'] = str(summary_item.get('summary') or runtime_state.get('history_summary') or '')
    runtime_state['history_tokens'] = int(runtime_state.get('history_tokens') or estimate_tokens(history)) if history else 0
    inputs={
        'message': message,
        'files': files,
        'conversation_history': history,
        **dict(runtime_state.get('inputs', {})),
    }
    try:
        result=(execute_flow(
                    row,spec,agents,inputs,outputs,runtime_state,event_callback,evidence.get('skill_roots',[]),ask_user_store,
                    evidence.get('receipt_store'),
                ) if row.kind=='flow'
                else execute_crew(
                    row,spec,agents,shared,inputs,outputs,model_profiles or {},runtime_state,
                    ask_user_store,evidence.get('receipt_store'),event_callback,
                ))
        result['skill_execution_receipts']=evidence['receipts']
        result['checkpoint'] = result.get('checkpoint') or RuntimeCheckpoint.from_resume(runtime_state).dump()
        result['events']=unique_events(result.get('events',[]))
        return result
    finally:
        if shared:
            shared.close()
