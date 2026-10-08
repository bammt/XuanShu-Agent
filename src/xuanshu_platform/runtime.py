import json
import copy
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from crewai import Agent, Crew, Process, Task
from crewai.events.stream_context import add_stream_sink, reset_stream_sinks
from crewai.events.types.agent_events import (
    AgentExecutionCompletedEvent,
    AgentExecutionErrorEvent,
    AgentExecutionStartedEvent,
)
from crewai.events.types.llm_events import LLMStreamChunkEvent, LLMThinkingChunkEvent
from crewai.events.types.tool_usage_events import (
    ToolUsageErrorEvent,
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
)
from crewai.flow.flow import Flow, start
from pydantic import BaseModel, Field, create_model
from .config import settings
from .node_execution import bind_node_inputs, code_program, evaluate_router, parse_code_result
from .db import Application
from .schemas import ApplicationDefinition
from .services import (
    app_root_dir, app_runtime_dir, app_session_dir, application_execution_skill_roots,
    application_skill_roots, materialize_application_resources, safe_relative_path,
)
from .memory import persistent_memory
from .model_runtime import profile_llm, profile_llm_kwargs
from .tools.builtin import (AskUserRequestStore, AskUserTool, ExecutionReceiptStore, builtin_tools,
                            configured_capabilities, execution_idempotency_scope)
from .conversation import estimate_tokens
from .knowledge import build_knowledge
from .contracts import (ROUTE_REGEX_MAX_TEXT, ensure_fixed_output_contracts,
                        ensure_executable_contract, ensure_variable_contract, execution_order, unsafe_route_regex,
                        upstream_node_ids)
from .state_machine import NodeStatus, RunStatus, RuntimeCheckpoint


RuntimeEventCallback = Callable[[dict[str, Any]], None]
_runtime_event_callback: ContextVar[RuntimeEventCallback | None] = ContextVar(
    'xuanshu_runtime_event_callback', default=None,
)
_runtime_activity_context: ContextVar[dict[str, str]] = ContextVar(
    'xuanshu_runtime_activity_context', default={},
)


_runtime_agent_ids: ContextVar[dict[str, str]] = ContextVar('runtime_agent_ids', default={})
_runtime_emitted_tool_events: ContextVar[frozenset[str]] = ContextVar(
    'xuanshu_runtime_emitted_tool_events', default=frozenset(),
)


def _tool_event_key(payload: dict[str, Any]) -> str:
    """Return an idempotency key for one SDK/platform tool lifecycle event."""
    if payload.get('type') not in {'tool.started', 'tool.completed', 'tool.failed'}:
        return ''
    call_id = payload.get('tool_call_id') or payload.get('call_id')
    event_id = payload.get('sdk_event_id') or payload.get('event_id')
    identity = str(call_id or event_id or '').strip()
    if not identity:
        return ''
    return ':'.join((str(payload.get('type')), str(payload.get('node_id') or ''), identity))


def _claim_tool_event(payload: dict[str, Any]) -> bool:
    key = _tool_event_key(payload)
    if not key:
        return True
    seen = _runtime_emitted_tool_events.get()
    if key in seen:
        return False
    _runtime_emitted_tool_events.set(seen | {key})
    return True


def _forward_crewai_activity(event_type: str, event: Any, **extra: Any) -> None:
    """Translate CrewAI lifecycle events into safe, user-facing run events."""
    callback = _runtime_event_callback.get()
    if callback is None:
        return
    context = dict(_runtime_activity_context.get() or {})
    call_type = str(getattr(event, 'call_type', '') or '')
    if event_type == 'llm.delta' and call_type and call_type not in {'LLMCallType.LLM_CALL', 'llm_call'}:
        return
    payload = {
        'type': event_type,
        **context,
        'agent_id': _runtime_agent_ids.get().get(
            str(getattr(event, 'agent_id', '') or ''),
            str(getattr(event, 'agent_id', '') or context.get('agent_id', '')),
        ),
        'agent_role': str(getattr(event, 'agent_role', '') or context.get('agent_role', '')),
        'task_name': str(getattr(event, 'task_name', '') or ''),
        **extra,
    }
    if not _claim_tool_event(payload):
        return
    try:
        callback(payload)
    except Exception:
        # Observability must never interrupt an Agent or Tool execution.
        return


def _on_agent_started(_source: Any, event: AgentExecutionStartedEvent) -> None:
    subject = (getattr(event, 'task_name', None) or getattr(event, 'agent_role', None)
               or '当前任务')
    _forward_crewai_activity('agent.started', event, detail=f'正在理解：{subject}…')


def _on_agent_completed(_source: Any, event: AgentExecutionCompletedEvent) -> None:
    subject = (getattr(event, 'task_name', None) or getattr(event, 'agent_role', None)
               or '当前任务')
    _forward_crewai_activity('agent.completed', event, detail=f'已完成理解：{subject}')


def _on_agent_failed(_source: Any, event: AgentExecutionErrorEvent) -> None:
    error = str(getattr(event, 'error', '') or '')
    _forward_crewai_activity(
        'agent.retrying', event,
        detail=(f'模型调用失败，CrewAI 正在重试当前任务…{error[:180]}'
                if error else '模型调用失败，CrewAI 正在重试当前任务…'),
    )
    _forward_crewai_activity('agent.failed', event, detail='智能体执行失败')


# An immutable stack belongs to the emitting execution context, not to the
# process or agent name. Copied parallel contexts cannot consume each other's
# calls. The synchronous stream sink observes starts before tool execution.
_runtime_tool_calls: ContextVar[tuple] = ContextVar('runtime_tool_calls', default=())


def _tool_event_identity(source: Any, event: Any, *, starting: bool) -> str:
    key = (id(source), str(getattr(event, 'tool_name', '') or ''),
           str(getattr(event, 'task_id', '') or ''))
    calls = _runtime_tool_calls.get()
    if starting:
        call_id = str(getattr(event, 'event_id', '') or '')
        _runtime_tool_calls.set((*calls, (key, call_id)))
        return call_id
    # The SDK's general scope stack can refer to an inner/non-tool event.
    # Match the actual emitting source in this execution's tool-only stack.
    for index in range(len(calls) - 1, -1, -1):
        if calls[index][0] == key:
            _runtime_tool_calls.set(calls[:index] + calls[index + 1:])
            return calls[index][1]
    return str(getattr(event, 'started_event_id', '') or getattr(event, 'event_id', '') or '')


def _on_tool_started(_source: Any, event: ToolUsageStartedEvent) -> None:
    tool_call_id = _tool_event_identity(_source, event, starting=True)
    _forward_crewai_activity(
        'tool.started', event,
        tool_name=str(getattr(event, 'tool_name', '') or '工具'),
        tool_call_id=tool_call_id,
        sdk_event_id=str(getattr(event, 'event_id', '') or ''),
        sdk_started_event_id=str(getattr(event, 'started_event_id', '') or ''),
        detail=f"正在调用 {getattr(event, 'tool_name', '') or '工具'}…",
    )


def _on_tool_finished(_source: Any, event: ToolUsageFinishedEvent) -> None:
    tool_call_id = _tool_event_identity(_source, event, starting=False)
    _forward_crewai_activity(
        'tool.completed', event,
        tool_name=str(getattr(event, 'tool_name', '') or '工具'),
        tool_call_id=tool_call_id,
        sdk_event_id=str(getattr(event, 'event_id', '') or ''),
        sdk_started_event_id=str(getattr(event, 'started_event_id', '') or ''),
        from_cache=bool(getattr(event, 'from_cache', False)),
        detail=(f"已读取缓存结果：{getattr(event, 'tool_name', '') or '工具'}"
                if getattr(event, 'from_cache', False)
                else f"已完成 {getattr(event, 'tool_name', '') or '工具'}"),
    )


def _on_llm_chunk(_source: Any, event: LLMStreamChunkEvent) -> None:
    """Forward provider token deltas so the UI can render a live answer."""
    chunk = str(getattr(event, 'chunk', '') or '')
    if not chunk:
        return
    _forward_crewai_activity(
        'llm.delta', event,
        text=chunk,
        **({'llm_call_id': str(event.call_id)} if getattr(event, 'call_id', None) else {}),
        detail='正在生成回复…',
    )


def _on_llm_thinking_chunk(_source: Any, event: LLMThinkingChunkEvent) -> None:
    """Show progress while a provider exposes reasoning tokens, without saving them."""
    if not getattr(event, 'chunk', None):
        return
    _forward_crewai_activity('llm.thinking', event, detail='正在整理思路…')


def _on_tool_failed(source: Any, event: ToolUsageErrorEvent) -> None:
    _forward_crewai_activity(
        'tool.failed', event,
        tool_name=event.tool_name,
        tool_call_id=_tool_event_identity(source, event, starting=False),
        sdk_event_id=event.event_id,
        sdk_started_event_id=event.started_event_id or '',
        detail=f'工具执行失败：{event.tool_name}',
    )


@contextmanager
def runtime_event_stream():
    """Observe SDK emissions before asynchronous listeners can reorder them."""
    handlers = {
        AgentExecutionStartedEvent: _on_agent_started,
        AgentExecutionCompletedEvent: _on_agent_completed,
        AgentExecutionErrorEvent: _on_agent_failed,
        ToolUsageStartedEvent: _on_tool_started,
        ToolUsageFinishedEvent: _on_tool_finished,
        ToolUsageErrorEvent: _on_tool_failed,
        LLMStreamChunkEvent: _on_llm_chunk,
        LLMThinkingChunkEvent: _on_llm_thinking_chunk,
    }
    def sink(source: Any, event: Any) -> None:
        handler = handlers.get(type(event))
        if handler is not None:
            handler(source, event)
    calls_token = _runtime_tool_calls.set(())
    sink_token = add_stream_sink(sink)
    try:
        yield
    finally:
        reset_stream_sinks(sink_token)
        _runtime_tool_calls.reset(calls_token)


@contextmanager
def runtime_activity_scope(**context: str):
    callback_token = _runtime_activity_context.set({
        key: str(value or '') for key, value in context.items() if value is not None
    })
    try:
        yield
    finally:
        _runtime_activity_context.reset(callback_token)

_RENDER_PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_.]*)\}')


def render(text:str,inputs:dict,outputs:dict)->str:
    """Substitute ``{name}`` placeholders in a single pass.

    Substituted values are never scanned again, so user text or an upstream
    node output that happens to contain ``{other_node}`` cannot pull another
    node's output into the prompt.  Unknown placeholders are left intact.
    """
    values = {**inputs, **outputs}

    def lookup(key: str) -> tuple[bool, Any]:
        if key in values:
            return True, values[key]
        # ``{node.object.field.sub}`` reads one field of a structured output.
        # The node's ``object`` is the only structured namespace; a missing
        # field renders empty, like a missing field in an input binding.
        head, sep, path = key.partition('.object.')
        if not sep or f'{head}.object' not in values:
            return False, None
        current = values[f'{head}.object']
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except (TypeError, json.JSONDecodeError):
                return True, ''
        for part in path.split('.'):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return True, ''
        return True, current

    def substitute(match: re.Match) -> str:
        found, value = lookup(match.group(1))
        if not found:
            return match.group(0)
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)

    return _RENDER_PLACEHOLDER.sub(substitute, str(text or ''))


def delegation_scoped_agent(agent: Any, enabled: bool) -> Any:
    """Keep delegation local to one Crew, including shared Agent definitions."""
    if hasattr(agent, 'model_copy'):
        return agent.model_copy(update={'allow_delegation': enabled})
    scoped = copy.copy(agent)
    scoped.allow_delegation = enabled
    return scoped


def manager_scoped_agent(agent: Any) -> Any:
    """CrewAI hierarchical managers must not carry task tools."""
    updates = {
        'allow_delegation': True,
        'tools': [],
        'mcps': [],
        'apps': [],
        'skills': None,
    }
    if hasattr(agent, 'model_copy'):
        return agent.model_copy(update=updates)
    scoped = copy.copy(agent)
    for key, value in updates.items():
        try:
            setattr(scoped, key, value)
        except Exception:
            pass
    return scoped
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
        memory_llm=profile_llm(default, settings.openai_model, stream=False)
        shared=persistent_memory(path,memory_llm,f'/workspace/{row.workspace_id}/app/{row.id}')
    agents={}
    for item in spec.agents:
        profile=(profiles or {}).get(str(item.model_profile_id or item.model_id)) or (profiles or {}).get(item.model_id) or (profiles or {}).get(str(spec.model_profile_id)) or (profiles or {}).get('default') or {}
        llm_profile = dict(profile)
        # DeepSeek-compatible thinking requests cannot be combined with the
        # tool_choice payload used by CrewAI. Agents with any platform/tool
        # surface use ordinary tool-calling mode; plain text Agents retain the
        # configured thinking mode.
        if (item.skills or item.plugins or item.allow_code_execution or
                item.user_interaction or item.function_calling_model_profile_id):
            llm_profile['thinking_mode'] = 'disabled'
            llm_profile['thinking_effort'] = None
        llm=profile_llm_kwargs(llm_profile, settings.openai_model, stream=True)
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
            function_profile = dict(function_profile)
            function_profile['thinking_mode'] = 'disabled'
            function_profile['thinking_effort'] = None
            function_llm=profile_llm(function_profile, stream=True)
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
            respect_context_window=item.respect_context_window,
            multimodal=bool(item.multimodal and profile.get('supports_vision', False)),
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
    """Keep one lifecycle event when a retry or SDK adapter replays it."""
    result=[]; seen=set()
    for item in events:
        if item.get('type') in {'tool.started', 'tool.completed', 'tool.failed'}:
            identity = item.get('tool_call_id') or item.get('call_id') or item.get('sdk_event_id')
            if identity:
                fingerprint = (item.get('type'), item.get('node_id'), str(identity), item.get('run_attempt', 0))
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
        elif item.get('type') in {
            'node.started', 'node.completed', 'node.skipped',
            'node.waiting_input', 'run.waiting_input', 'approval.required',
        }:
            node_id = item.get('node_id')
            checkpoint = item.get('checkpoint') or {}
            node_checkpoint = (checkpoint.get('nodes') or {}).get(str(node_id), {}) if isinstance(checkpoint, dict) else {}
            # A retried ask_user/node has the same node_id but a new checkpoint
            # attempt. Keep that new event so an SSE client whose cursor is at
            # the previous pause can render the new question and resume state.
            attempt = item.get('attempt') or node_checkpoint.get('attempt')
            fingerprint=(item.get('type'), node_id, attempt, item.get('run_attempt', 0))
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
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value or '')
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


def _parse_structured_output(value: Any) -> dict[str, Any] | None:
    """Parse a structured task result, including Markdown JSON fences.

    Providers frequently wrap valid JSON in `````json`` fences even when
    CrewAI's structured-output adapter is enabled. The fence is transport
    formatting, not business content, so remove it before decoding.
    """
    if isinstance(value, dict):
        return value
    text = str(value or '').strip()
    if text.startswith(COLLECTION_COMPLETE_MARKER):
        text = text[len(COLLECTION_COMPLETE_MARKER):].lstrip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_node_output(item, value: Any) -> str:
    """Normalize a language result into the fixed internal envelope.

    ``output_mode=text`` keeps the model's final answer verbatim in ``text``;
    ``output_mode=json`` parses the business object. The envelope is an
    internal data contract and is never an instruction for text-mode tasks.
    """
    raw = user_visible_output(value)
    structured = str(getattr(item, 'output_mode', 'text') or 'text') == 'json'
    parsed = _parse_structured_output(value if isinstance(value, dict) else raw) if structured else None
    if not structured:
        # Be tolerant when a provider ignores the text-mode instruction and
        # still emits the platform envelope. Keep ordinary JSON documents
        # untouched; unwrap only objects that clearly contain the reserved
        # ``text`` field alongside the internal envelope fields.
        candidate = _parse_structured_output(raw)
        if isinstance(candidate, dict) and isinstance(candidate.get('text'), str) and (
            'object' in candidate or 'file' in candidate or 'output' in candidate
        ):
            raw = candidate['text']
    if structured and parsed is None:
        raise ValueError(f'任务 {getattr(item, "name", "")} 的结构化 JSON 输出无法解析')
    if structured and not isinstance(parsed, dict):
        raise ValueError('结构化输出必须是 JSON 对象')
    # JSON mode is a user-defined business object. Preserve every field. Keep
    # one compatibility path for old models that returned exactly the legacy
    # {object, text} envelope; new business JSON is never wrapped.
    legacy_envelope = (
        structured and isinstance(parsed.get('object'), dict)
        and set(parsed).issubset({'object', 'text', 'file', 'files', 'generated_files'})
    )
    data_object = parsed.get('object', {}) if legacy_envelope else (parsed if structured else {})
    # In JSON mode ``text`` is the complete model response. The parsed
    # business object is exposed separately through ``object``; do not throw
    # away the JSON envelope by replacing text with its nested text field.
    text = raw if structured else ''
    if not structured and isinstance(parsed, dict) and isinstance(parsed.get('text'), str):
        text = parsed['text']
    elif not structured and parsed is None:
        text = raw
    if text.startswith(COLLECTION_COMPLETE_MARKER):
        text = text[len(COLLECTION_COMPLETE_MARKER):].lstrip()
    return json.dumps({'object': data_object, 'file': [], 'text': text}, ensure_ascii=False)

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
    if str(getattr(item, 'output_mode', 'text') or 'text') == 'json':
        output_contract = (
            '本任务启用了结构化 JSON 输出。请直接返回符合 expected_output 描述的业务 JSON 对象，'
            '不要再包装 object、file、text 等平台字段，不要使用 Markdown 代码围栏，'
            '不要在 JSON 外添加解释。file 由平台依据实际执行回执填写。'
        )
    else:
        output_contract = (
            '本任务使用普通文本输出。请直接返回面向用户的最终正文，'
            '不要使用 object/file/text 包装对象或内部控制字段包裹正文；业务内容需要时可使用 Markdown。'
            'file 由平台依据实际执行回执填写，不要在正文中伪造文件链接。'
        )
    return (f'{rendered}{upstream}\n\n输出要求：{output_requirement}\n{output_contract}\n'
            '只返回任务要求的最终结果。不要输出 reasoning plan、复盘状态或重复执行说明。'
            '生成文件由平台作为下载对象展示，不要输出 /var/lib/xuanshu/workspaces 本地路径，'
            '也不要为本地文件构造 Markdown 下载链接。代码中必须从环境变量 XUANSHU_WORKSPACE 读取工作目录，'
            '不要硬编码 /workspace 或宿主机绝对路径。')


def node_artifacts_from_receipts(receipts: list[dict[str, Any]], offset: int) -> list[str]:
    """Observe files actually created by the current node without requiring one.

    A script can create a valid deliverable and then return non-zero while
    performing an optional post-check (for example, a missing ``fc-list``
    utility). A file explicitly returned by the executor is still a concrete
    artifact; keep it available for downstream Crew tasks and let the final
    node decide whether to replace it.
    """
    produced = []
    for receipt in receipts[offset:]:
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
    files = [Path(str(item)).name for item in (inputs.get('files') or []) if str(item).strip()]
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
        '你已绑定 ask_user 工具，负责在执行工作前补齐确实无法推断的必要信息。'
        '先完整阅读本轮消息、会话历史、已收集字段、附件和上游结果；用户在任一轮明确给出的答案与偏好均视为有效，'
        '同一事项以用户最新答复为准；简短答复应结合上一轮问题理解，历史助手提问不代表该事项仍未回答。'
        '不得重复询问或要求再次确认。用户明确要求自行编写、随机拟定或采用常见场景默认值时，应据此生成示例内容并继续，明确标注为示例；'
        '用户要求留空的项目必须留空。只有缺少无法合理推断且完成任务必需的信息，或现有要求互相矛盾时，才必须调用 ask_user；'
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
        '本节点必须先核对本轮消息、完整会话历史、已收集字段、附件和上游结果是否足以完成业务。'
        '用户在任一轮明确给出的答案与偏好均视为有效；同一事项以用户最新答复为准，简短答复应结合上一轮问题理解。'
        '历史助手提问不代表该事项仍未回答；不得重复询问已答事项，不得要求用户再次确认已表达的决定。'
        '用户明确要求自行编写、随机拟定或采用常见场景默认值时，应据此生成示例内容并继续，明确标注为示例；'
        '用户要求留空的项目必须留空。只有缺少无法合理推断且完成任务必需的信息，或现有要求互相矛盾时，才调用 ask_user 工具；'
        '向用户提问不能直接输出文本，不能作为 Final Answer、expected_output 或节点结果返回。'
        '即使下方要求“只返回最终结果”或描述了结构化交付物，仍须先解决真正必需且未回答的信息；'
        '不得用重复确认代替判断。调用 ask_user 后立即停止当前节点并等待用户回复。'
        '只有所需信息完整且无歧义时，才可跳过 ask_user 并完成业务输出；此时必须在业务结果第一行'
        f'输出精确标记 {COLLECTION_COMPLETE_MARKER}。平台只有检测到该标记才允许进入下一节点，'
        '并会在展示及传递结果前移除标记。\n'
        f'{ask_user_contract_guidance(allowed_inputs)}'
        '\n\n【当前轮次信息】\n'
        f'{current_turn_context(inputs)}\n\n'
        '【当前节点业务任务】\n'
        f'{prompt}\n\n'
        '【进入下一节点前强制检查】先检查完整历史和已收集字段；不要重问已经回答的内容。'
        '只有仍缺少无法合理推断且完成任务必需的信息，或存在互相矛盾的要求时才必须调用 ask_user；'
        f'只有信息收集已经完成并输出 {COLLECTION_COMPLETE_MARKER}，平台才允许进入下一节点。'
    )


def completed_collection_output(raw: str) -> str:
    """Require an explicit completion signal before leaving an interactive node."""
    text = str(raw or '').strip()
    document = _parse_structured_output(text)
    if isinstance(document, dict) and isinstance(document.get('text'), str):
        content = document['text'].strip()
        if content.startswith(COLLECTION_COMPLETE_MARKER):
            content = content[len(COLLECTION_COMPLETE_MARKER):].lstrip(' \t\r\n:：-')
            if not content and not document.get('object'):
                raise RuntimeError('信息收集节点声明完成但没有返回可传递的业务结果，本次运行已停止。')
            document['text'] = content
            return json.dumps(document, ensure_ascii=False)
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

def _task_output_model(item) -> type[BaseModel] | None:
    """Build CrewAI's structured output model from the platform contract.

    Language task contract has required object and text fields. File artifacts
    are always registered by runtime receipts, never fabricated by the model.
    """
    if getattr(item, 'output_mode', 'text') != 'json':
        return None
    fields = list(getattr(item, 'output_variables', []) or [])
    if not fields:
        return None
    normalized = []
    for field in fields:
        raw = field.model_dump() if hasattr(field, 'model_dump') else dict(field or {})
        name = str(raw.get('name') or '').strip()
        value_type = str(raw.get('value_type') or 'string')
        if not name:
            continue
        normalized.append((name, value_type, str(raw.get('description') or '')))
    if len(normalized) == 1 and normalized[0][:2] == ('result', 'string'):
        return None
    python_types = {
        'string': str,
        'number': float,
        'boolean': bool,
        'object': dict[str, Any],
        'array': list[Any],
        'file': list[str],
    }
    model_fields = {
        name: (python_types.get(value_type, str), Field(description=description or name))
        for name, value_type, description in normalized if value_type != 'file'
    }
    if not model_fields:
        return None
    safe_name = re.sub(r'[^A-Za-z0-9_]', '_', str(getattr(item, 'id', 'task')))
    return create_model(f'XuanShuTaskOutput_{safe_name}', **model_fields)


def _provider_uses_prompt_json(profile: dict | None) -> bool:
    """Compatible gateways should receive JSON instructions, not tool_choice."""
    provider = str((profile or {}).get('provider') or '').strip().lower()
    base_url = str((profile or {}).get('base_url') or '').lower()
    return provider in {'openai-compatible', 'ollama', 'ollama_chat', 'custom'} or (
        provider == 'openai' and base_url and 'api.openai.com' not in base_url
    )


def _agent_profile(spec: ApplicationDefinition, agent_id: str | None,
                   profiles: dict | None) -> dict:
    profiles = profiles or {}
    agent = next((item for item in (getattr(spec, 'agents', None) or [])
                  if str(item.id) == str(agent_id or '')), None)
    if agent is not None:
        for key in (agent.model_profile_id, agent.model_id):
            if key is not None and profiles.get(str(key)):
                return profiles[str(key)]
            if key is not None and profiles.get(key):
                return profiles[key]
    return profiles.get(str(getattr(spec, 'model_profile_id', '') or '')) or profiles.get('default') or {}


def task_options(row: Application, item, profile: dict | None = None) -> dict:
    options = {
        'markdown': bool(item.markdown) and getattr(item, 'output_mode', 'text') != 'json',
        'async_execution': bool(getattr(item, 'async_execution', False)),
    }
    output_model = _task_output_model(item)
    if (output_model is not None
            and str(getattr(item, 'output_mode', 'text') or 'text') == 'json'
            and not _provider_uses_prompt_json(profile)):
        options['output_json'] = output_model
    # CrewAI's output_file persists the task's text response. Business files
    # are created by the isolated execution tools and registered separately.
    # In CrewAI 1.15 an absolute output_file is stripped to a relative path,
    # which would turn /var/lib/... into /app/var/lib/... inside the worker.
    return options


def task_output_value(task_output: Any, item) -> Any:
    """Prefer CrewAI's parsed structured result when JSON mode is enabled."""
    raw = getattr(task_output, 'raw', task_output)
    if COLLECTION_COMPLETE_MARKER in str(raw):
        return raw
    if str(getattr(item, 'output_mode', 'text') or 'text') == 'json':
        parsed = getattr(task_output, 'pydantic', None)
        if parsed is not None:
            return parsed.model_dump(mode='json') if hasattr(parsed, 'model_dump') else parsed
        parsed = getattr(task_output, 'json_dict', None)
        if isinstance(parsed, dict):
            return parsed
    return getattr(task_output, 'raw', task_output)

def mapped_task_inputs(item, inputs: dict, outputs: dict,
                       node_artifacts: dict[str, list[str]] | None = None,
                       task_by_id: dict[str, Any] | None = None) -> dict:
    values = dict(inputs)
    node_artifacts = node_artifacts or {}
    task_by_id = task_by_id or {}
    # Every executed ancestor is visible. The old dependency_variables map is
    # accepted for old saved definitions, but it no longer limits data flow.
    allowed_upstream = upstream_node_ids(str(getattr(item, 'id', '') or ''), [
        {'id': str(key), 'depends_on': list(getattr(task, 'depends_on', []) or [])}
        for key, task in task_by_id.items()
    ]) if task_by_id else set(outputs)
    for dependency, raw in outputs.items():
        if str(dependency) not in allowed_upstream:
            continue
        parsed = None
        dependency_task = task_by_id.get(str(dependency))
        output_fields = [
            field.model_dump() if hasattr(field, 'model_dump') else field
            for field in getattr(dependency_task, 'output_variables', [])
        ] if dependency_task else []
        mappings = [
            {'source_variable': field.get('name'),
             'target_variable': f'{dependency}.{field.get("name")}'}
            for field in output_fields if field.get('name')
        ]
        for mapping in mappings:
            source = mapping.get('source_variable', 'result')
            target = mapping.get('target_variable', f'{dependency}.{source}')
            value = raw
            source_type = next(
                (field.get('value_type') for field in output_fields if field.get('name') == source),
                None,
            )
            if source not in {'result', '$raw'}:
                if parsed is None:
                    try: parsed = json.loads(raw)
                    except (TypeError, json.JSONDecodeError): parsed = raw
                if isinstance(parsed, dict):
                    value = parsed.get(source, [] if source_type == 'file' else '')
                elif source == 'text' and source_type == 'string':
                    value = raw
                elif source == 'object':
                    value = parsed if isinstance(parsed, dict) else {}
                else:
                    value = raw
            values[target] = value
    return values

def should_run(item, outputs: dict, runtime_state: dict) -> bool:
    expected = str(getattr(item, 'run_if', '') or '').strip()
    if not expected:
        return True
    candidates = [route_output(outputs.get(dependency, '')) for dependency in item.depends_on]
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
    for item in reversed(ordered):
        value = outputs.get(item.id)
        if not value:
            continue
        raw = str(value)
        try:
            document = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            document = None
        if isinstance(document, dict):
            if 'route' in document:
                return str(document['route'])
            if str(document.get('text') or '').strip():
                return str(document['text'])
            if document.get('object'):
                return json.dumps(document['object'], ensure_ascii=False)
            if document.get('output'):
                return json.dumps(document['output'], ensure_ascii=False)
        return raw
    return ''

def crew_options(spec, agents: dict, profiles: dict, output_log_file: str | None = None) -> tuple[list, dict]:
    process=Process.hierarchical if spec.process=='hierarchical' else Process.sequential
    runtime_agents={agent_id: (
        manager_scoped_agent(agent)
        if process == Process.hierarchical and agent_id == spec.manager_agent_id
        else delegation_scoped_agent(agent, False)
    ) for agent_id, agent in agents.items()}
    participants=list(runtime_agents.values())
    kwargs={'process':process,'memory':False,'verbose':bool(getattr(spec, 'verbose', False)),
            'cache':spec.cache,
            'output_log_file': output_log_file or spec.output_log_file or None}
    if process==Process.hierarchical:
        if spec.manager_agent_id:
            manager=runtime_agents[spec.manager_agent_id]
            participants=[agent for key,agent in runtime_agents.items() if key!=spec.manager_agent_id]
            kwargs['manager_agent']=manager
        else:
            manager_profile=(profiles or {}).get(str(spec.manager_model_profile_id)) or (profiles or {}).get('default') or {}
            kwargs['manager_llm']=(profile_llm(manager_profile, stream=True)
                                   if manager_profile.get('model') else next(iter(agents.values())).llm)
    if spec.planning:
        planning_profile=(profiles or {}).get(str(spec.planning_model_profile_id)) or (profiles or {}).get('default') or {}
        kwargs.update({'planning':True,'planning_llm':profile_llm(
            planning_profile, settings.openai_model, stream=True,
        )})
    return participants,kwargs


def runtime_output_log_path(row, spec: ApplicationDefinition, runtime_state: dict) -> str | None:
    """Place CrewAI logs in the run workspace so they are persisted as artifacts."""
    configured = str(spec.output_log_file or '').strip()
    if not configured:
        return None
    try:
        relative = safe_relative_path(configured)
    except ValueError as exc:
        raise ValueError('执行日志文件必须是应用工作区内的相对路径') from exc
    scope = str(runtime_state.get('execution_scope') or runtime_state.get('conversation_id') or 'runtime')
    target = app_session_dir(row.workspace_id, row.id, scope, row.kind) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)

def emit_runtime_event(callback: RuntimeEventCallback | None, item: dict[str, Any]) -> None:
    item.setdefault('at', datetime.now(UTC).isoformat())
    if callback and _claim_tool_event(item):
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
    output_log_file = runtime_output_log_path(row, spec, runtime_state)
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
                   'node_type':item.node_type,
                   'output_mode': getattr(item, 'output_mode', 'text'),
                   'attempt':node_checkpoint.attempt,'idempotency_key':idempotency_key,
                   'checkpoint':checkpoint.dump()}
        events.append(started); emit_runtime_event(event_callback, started)
        task=Task(name=item.name,description=description,expected_output=render(item.expected_output, mapped_inputs, outputs),
                  agent=(None if spec.process == 'hierarchical' else assigned),context=None,
                  **task_options(row, item, _agent_profile(spec, item.agent_id, profiles)))
        participants,kwargs=crew_options(spec,agents,profiles,output_log_file)
        kwargs['memory']=shared if spec.memory_policy.long_term_semantic else False
        receipt_offset = len(receipt_store.items) if receipt_store is not None else 0
        with runtime_activity_scope(
            node_id=item.id, node_name=item.name,
            output_mode=item.output_mode,
            node_type=item.node_type,
            agent_id=interactive_owner or str(item.agent_id or ''),
            agent_role=getattr(assigned, 'role', '') or '',
        ):
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
            node_waiting={'type':'node.waiting_input','node_id':item.id,'node_name':item.name,
                          'agent_id':item.agent_id,'agent_role':getattr(assigned, 'role', ''),
                          'question':waiting_input['question'],'waiting_input':waiting_input,
                          'checkpoint':checkpoint.dump()}
            events.append(node_waiting); emit_runtime_event(event_callback,node_waiting)
            events.append(paused); emit_runtime_event(event_callback,paused)
            return {'status':'waiting_input','output':waiting_input['question'],'outputs':outputs,'events':events,
                    'pending_node':item.id,'waiting_input':waiting_input,
                    'node_artifacts': node_artifacts, 'checkpoint':checkpoint.dump()}
        task_outputs=list(getattr(result,'tasks_output',[]) or [])
        task_output = task_outputs[-1] if task_outputs else result
        raw=user_visible_output(task_output_value(task_output, item))
        if interactive_owner in ask_user_agents:
            raw = completed_collection_output(raw)
        delivered = node_artifacts_from_receipts(
            receipt_store.items if receipt_store is not None else [], receipt_offset,
        )
        envelope = json.loads(normalize_node_output(item, raw))
        envelope['file'] = list(dict.fromkeys(delivered))
        raw = json.dumps(envelope, ensure_ascii=False)
        outputs[item.id]=raw
        checkpoint.complete_node(item.id, raw)
        completed_event = event(item,agents.get(item.agent_id),raw,task_output)
        completed_event['output_mode'] = getattr(item, 'output_mode', 'text')
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
    if match:
        pattern = match.group(1)
        problem = unsafe_route_regex(pattern)
        if problem:
            raise ValueError(problem)
        return 'true' if re.search(pattern, text[:ROUTE_REGEX_MAX_TEXT]) else 'false'
    return text.strip()


def route_output(output: Any) -> str:
    try:
        parsed = json.loads(output) if isinstance(output, str) else output
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict) and 'route' in parsed:
        return str(parsed['route']).strip()
    return str(output or '').strip()


def router_branch_for_node(router, node_id: str, output: str) -> bool | None:
    """Return route eligibility for a node directly assigned to a CASE."""
    matching = [branch for branch, targets in (router.routes or {}).items() if node_id in targets]
    if not matching:
        return None
    return route_output(output) in matching

def execute_flow_crew(row,item,agents,node_prompt,inputs,outputs,
                      ask_user_store: AskUserRequestStore | dict[str, AskUserRequestStore] | None = None,
                      resume_waiting_input: dict[str, Any] | None = None,
                      cache: bool = True,
                      output_log_file: str | None = None,
                      profiles: dict | None = None,
                      shared_memory: Any = None,
                      execution_scope: str = 'runtime',
                      receipt_store: ExecutionReceiptStore | None = None):
    hierarchical = item.crew_process == 'hierarchical'
    configured_manager_id = str(item.crew_manager_agent_id or '').strip() if hierarchical else ''
    manager_profile_id = str(getattr(item, 'crew_manager_model_profile_id', '') or '').strip()
    # CrewAI accepts either a manager Agent or a manager_llm for hierarchical
    # crews. Keep the two choices explicit so an embedded Crew has the same
    # controls as the application-level Crew.
    if hierarchical and configured_manager_id:
        manager_id = configured_manager_id
    elif hierarchical and not manager_profile_id:
        manager_id = str(item.crew_agent_ids[0]) if item.crew_agent_ids else ''
    else:
        manager_id = ''
    crew_agents = {
        agent_id: (
            manager_scoped_agent(agents[agent_id])
            if hierarchical and agent_id == manager_id
            else delegation_scoped_agent(agents[agent_id], False)
        )
        for agent_id in item.crew_agent_ids
    }
    participants=[crew_agents[x] for x in item.crew_agent_ids if str(x) != manager_id]
    if hierarchical and not manager_id:
        participants = [crew_agents[x] for x in item.crew_agent_ids]
    nested_output_log_file = output_log_file
    configured_nested_log = str(getattr(item, 'crew_output_log_file', '') or '').strip()
    if configured_nested_log:
        try:
            relative = safe_relative_path(configured_nested_log)
        except ValueError as exc:
            raise ValueError('嵌套 Crew 执行日志文件必须是应用工作区内的相对路径') from exc
        target = app_session_dir(
            row.workspace_id, row.id, str(execution_scope or 'runtime'), row.kind,
        ) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        nested_output_log_file = str(target)
    native={}; tasks=[]
    nested_outputs={
        str(key): str(value)
        for key, value in dict((resume_waiting_input or {}).get('_nested_outputs') or {}).items()
    }
    def publish_nested_public_outputs(nested_id: str, output: str) -> None:
        """Expose nested Crew values as ``crew1.task1.field`` references."""
        public_crew = str(item.id).replace('_', '')
        public_task = str(nested_id).replace('_', '')
        try:
            envelope = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            envelope = {'text': output}
        if not isinstance(envelope, dict):
            envelope = {'text': output}
        for field in ('object', 'file', 'text'):
            if field in envelope:
                nested_outputs[f'{public_crew}.{public_task}.{field}'] = envelope[field]
    resume_nested_task = str((resume_waiting_input or {}).get('_nested_task_id') or '').strip()
    skip_until_resume = bool(resume_nested_task)
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
        rendered = render(spec.description, {**nested_inputs, **nested_outputs}, {})
        task=Task(name=spec.name,
                  description=f'Crew 节点目标：\n{node_prompt}\n\n当前内部任务：\n{rendered}',
                  expected_output=render(spec.expected_output, nested_inputs, {}),
                  agent=(None if hierarchical else crew_agents[spec.agent_id] if spec.agent_id else participants[0]),
                  context=(context or None) if hierarchical else None,
                  **task_options(row, spec, _agent_profile(spec, spec.agent_id, profiles)))
        native[spec.id]=task; tasks.append(task)

    if hierarchical:
        # Keep the manager-driven delegation semantics, but create a Crew
        # boundary for each internal task.  A single kickoff would allow later
        # tasks to run after ask_user and would give resume no inner checkpoint.
        final_output = ''
        for spec, task in zip((nested_by_id[nested_id] for nested_id in nested_order), tasks):
            if skip_until_resume and spec.id != resume_nested_task:
                continue
            skip_until_resume = False
            nested_context = mapped_task_inputs(
                spec, {**inputs, **outputs}, nested_outputs,
                task_by_id=nested_by_id,
            )
            rendered = render(spec.description, nested_context, nested_outputs)
            rendered_expected = render(spec.expected_output, nested_context, nested_outputs)
            task.description = task_description(
                spec,
                f'Crew 节点目标：\n{node_prompt}\n\n当前内部任务：\n{rendered}',
                dependency_context(spec, nested_outputs, {}),
                rendered_expected,
            )
            owner = manager_id if hierarchical else str(spec.agent_id or '')
            if owner in bound_ask_user_agent_ids(crew_agents):
                task.description = interactive_agent_prompt(task.description, inputs, {})
            task.expected_output = rendered_expected
            task.context = None
            crew_kwargs = {
                'process': Process.hierarchical,
                'verbose': bool(getattr(item, 'crew_verbose', False)),
                'cache': bool(getattr(item, 'crew_cache', cache)),
                'memory': shared_memory if getattr(item, 'crew_memory', False) and shared_memory else False,
            }
            if manager_id:
                crew_kwargs['manager_agent'] = crew_agents[manager_id]
            else:
                manager_profile = (profiles or {}).get(manager_profile_id) or (profiles or {}).get('default') or {}
                crew_kwargs['manager_llm'] = (
                    profile_llm(manager_profile, settings.openai_model, stream=True)
                    if manager_profile.get('model') else next(iter(agents.values())).llm
                )
            if getattr(item, 'crew_planning', False):
                crew_kwargs['planning'] = True
                planning_profile = (profiles or {}).get(
                    str(getattr(item, 'crew_planning_model_profile_id', '') or '')
                ) or (profiles or {}).get('default') or {}
                if planning_profile.get('model'):
                    crew_kwargs['planning_llm'] = profile_llm(
                        planning_profile, settings.openai_model, stream=True,
                    )
            if nested_output_log_file:
                crew_kwargs['output_log_file'] = nested_output_log_file
            nested_receipt_offset = (
                len(receipt_store.items) if receipt_store is not None else 0
            )
            with runtime_activity_scope(
                node_id=spec.id, node_name=spec.name,
                output_mode=spec.output_mode,
                agent_id=manager_id, agent_role=getattr(agents.get(manager_id), 'role', '') or '',
            ):
                result=Crew(agents=participants, tasks=[task], **crew_kwargs).kickoff()
            task_outputs=list(getattr(result, 'tasks_output', []) or [])
            task_output=task_outputs[-1] if task_outputs else result
            raw = user_visible_output(task_output_value(task_output, spec))
            request_store = agent_ask_user_store(ask_user_store, manager_id)
            if request_store:
                failure = ask_user_failure_event(request_store, spec.id, spec.name)
                if failure:
                    return '', None, failure
                if request_store.request:
                    waiting = request_store.consume() or {}
                    waiting['_nested_task_id'] = spec.id
                    waiting['_nested_outputs'] = dict(nested_outputs)
                    return '', waiting, None
            if manager_id in bound_ask_user_agent_ids(crew_agents):
                raw = completed_collection_output(raw)
            final_output=normalize_node_output(spec, raw)
            nested_files = node_artifacts_from_receipts(
                receipt_store.items if receipt_store is not None else [],
                nested_receipt_offset,
            )
            if nested_files:
                envelope = json.loads(final_output)
                envelope['file'] = nested_files
                final_output = json.dumps(envelope, ensure_ascii=False)
            nested_outputs[spec.id]=final_output
            publish_nested_public_outputs(spec.id, final_output)
            emit_runtime_event(_runtime_event_callback.get(), {
                'type': 'node.completed', 'node_id': spec.id, 'node_name': spec.name,
                'output_mode': spec.output_mode, 'output': final_output,
            })
        return final_output, None, None

    # A sequential nested Crew must stop at the first ask_user request. Running
    # every internal task in one kickoff lets later tasks execute after the
    # request and makes resume repeat already completed side effects.
    final_output = ''
    for spec, task in zip((nested_by_id[nested_id] for nested_id in nested_order), tasks):
        if skip_until_resume and spec.id != resume_nested_task:
            continue
        skip_until_resume = False
        # Tasks are constructed before the first kickoff for hierarchical
        # crews, but sequential crews need the outputs produced by earlier
        # internal tasks rendered into the current task's prompt.
        if not hierarchical:
            nested_context = mapped_task_inputs(
                spec, {**inputs, **outputs}, nested_outputs,
                task_by_id=nested_by_id,
            )
            rendered = render(spec.description, nested_context, nested_outputs)
            rendered_expected = render(spec.expected_output, nested_context, nested_outputs)
            task.description = task_description(
                spec,
                f'Crew 节点目标：\n{node_prompt}\n\n当前内部任务：\n{rendered}',
                dependency_context(spec, nested_outputs, {}),
                rendered_expected,
            )
            owner = manager_id if hierarchical else str(spec.agent_id or '')
            if owner in bound_ask_user_agent_ids(crew_agents):
                task.description = interactive_agent_prompt(task.description, inputs, {})
            task.expected_output = rendered_expected
            crew_kwargs = {
                'process': Process.sequential,
                'verbose': bool(getattr(item, 'crew_verbose', False)),
                'cache': bool(getattr(item, 'crew_cache', cache)),
                'memory': shared_memory if getattr(item, 'crew_memory', False) and shared_memory else False,
        }
        if getattr(item, 'crew_planning', False):
            crew_kwargs['planning'] = True
            planning_profile = (profiles or {}).get(
                str(getattr(item, 'crew_planning_model_profile_id', '') or '')
            ) or (profiles or {}).get('default') or {}
            if planning_profile.get('model'):
                crew_kwargs['planning_llm'] = profile_llm(
                    planning_profile, settings.openai_model, stream=True,
                )
        if nested_output_log_file:
            crew_kwargs['output_log_file'] = nested_output_log_file
        nested_receipt_offset = (
            len(receipt_store.items) if receipt_store is not None else 0
        )
        with runtime_activity_scope(
            node_id=spec.id, node_name=spec.name,
            output_mode=spec.output_mode,
            agent_id=str(spec.agent_id or ''),
            agent_role=getattr(crew_agents.get(spec.agent_id), 'role', '') or '',
        ):
            result=Crew(agents=participants, tasks=[task], **crew_kwargs).kickoff()
        task_outputs=list(getattr(result, 'tasks_output', []) or [])
        task_output=task_outputs[-1] if task_outputs else result
        raw = user_visible_output(task_output_value(task_output, spec))
        request_store = agent_ask_user_store(ask_user_store, spec.agent_id)
        if request_store:
            failure = ask_user_failure_event(request_store, spec.id, spec.name)
            if failure:
                return '', None, failure
            if request_store.request:
                waiting = request_store.consume() or {}
                waiting['_nested_task_id'] = spec.id
                waiting['_nested_outputs'] = dict(nested_outputs)
                return '', waiting, None
        if str(spec.agent_id or '') in bound_ask_user_agent_ids(crew_agents):
            raw = completed_collection_output(raw)
        final_output=normalize_node_output(spec, raw)
        nested_files = node_artifacts_from_receipts(
            receipt_store.items if receipt_store is not None else [],
            nested_receipt_offset,
        )
        if nested_files:
            envelope = json.loads(final_output)
            envelope['file'] = nested_files
            final_output = json.dumps(envelope, ensure_ascii=False)
        nested_outputs[spec.id]=final_output
        publish_nested_public_outputs(spec.id, final_output)
        emit_runtime_event(_runtime_event_callback.get(), {
            'type': 'node.completed', 'node_id': spec.id, 'node_name': spec.name,
            'output_mode': spec.output_mode, 'output': final_output,
        })
    return final_output, None, None

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
    resources: dict | None = None,
    model_profiles: dict | None = None,
    shared_memory: Any = None,
):
    checkpoint = RuntimeCheckpoint.from_resume(runtime_state)
    output_log_file = runtime_output_log_path(row, spec, runtime_state)
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
    ask_user_agents = bound_ask_user_agent_ids(agents)

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
                route_dependencies = [
                    dependency for dependency in item.depends_on
                    if task_by_id.get(dependency) and task_by_id[dependency].node_type == 'router'
                ]
                routed = [
                    router_branch_for_node(task_by_id[dependency], item.id,
                                           str(self.state.outputs.get(dependency) or ''))
                    for dependency in route_dependencies
                ]
                route_mismatch = any(value is False for value in routed)
                all_dependencies_skipped = bool(item.depends_on) and all(
                    checkpoint.node(dependency).status == NodeStatus.SKIPPED
                    for dependency in item.depends_on
                )
                if route_mismatch or all_dependencies_skipped or not should_run(item,self.state.outputs,runtime_state):
                    self.state.outputs[item.id]=''
                    checkpoint.node(item.id).status = NodeStatus.SKIPPED
                    checkpoint.outputs[item.id] = ''
                    reason = 'router_branch' if route_mismatch else 'inactive_dependencies' if all_dependencies_skipped else 'run_if'
                    skipped={'type':'node.skipped','node_id':item.id,'node_name':item.name,'run_if':item.run_if,'reason':reason}
                    skipped['checkpoint'] = checkpoint.dump()
                    self.state.events.append(skipped);emit_runtime_event(event_callback,skipped);continue
                mapped_inputs=mapped_task_inputs(
                    item,inputs,self.state.outputs,node_artifacts,task_by_id,
                )
                rendered_description = render(item.description,mapped_inputs,self.state.outputs)
                prompt=task_description(
                    item, rendered_description, '',
                    render(item.expected_output, mapped_inputs, self.state.outputs),
                )
                if item.node_type == 'crew':
                    prompt = rendered_description + '\n交付要求：' + render(item.expected_output, mapped_inputs, self.state.outputs)
                history_context = conversation_history_context(spec, inputs)
                if history_context and not item.depends_on and '{conversation_history}' not in item.description:
                    prompt = history_context + '\n\n' + prompt
                if feedback and feedback_node == item.id:
                    prompt += f'\n\n用户对上一版的修改意见：{feedback}\n请按该意见重新生成当前步骤，并保留原任务目标。'
                if item.node_type != 'crew' and str(item.agent_id or '') in ask_user_agents:
                    prompt = interactive_agent_prompt(
                        prompt, inputs, interactive_input_schema(spec),
                    )
                interactive_node = (
                    str(item.agent_id or '') in ask_user_agents
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
                         'node_type':item.node_type,
                         'output_mode': getattr(item, 'output_mode', 'text'),
                         'attempt':node_checkpoint.attempt,'idempotency_key':idempotency_key,
                         'checkpoint':checkpoint.dump()}
                self.state.events.append(started); emit_runtime_event(event_callback,started)
                receipt_offset = len(receipt_store.items) if receipt_store is not None else 0
                if item.node_type=='router':
                    raw=(evaluate_router(item.router_rules, inputs, self.state.outputs)
                         if item.router_rules else route_value(item.condition,rendered_description))
                    agent_role='条件路由'
                elif item.node_type=='code':
                    tool=next(tool for tool in builtin_tools(
                        row.workspace_id,row.id,app_kind=row.kind,skill_roots=skill_roots or [],
                        execution_scope=str(runtime_state.get('execution_scope') or 'legacy'),
                        receipt_store=receipt_store,
                    ) if tool.name=='execute_python')
                    code = (code_program(item.code_snippet, bind_node_inputs(item.input_bindings, mapped_inputs, self.state.outputs))
                            if item.execution_contract == 'object' else render(item.code_snippet, inputs, self.state.outputs))
                    emit_runtime_event(event_callback, {
                        'type': 'tool.started', 'node_id': item.id, 'node_name': item.name,
                        'node_type': item.node_type,
                        'tool_name': 'execute_python', 'detail': '正在执行代码…',
                    })
                    if hasattr(tool,'execute'):
                        with runtime_activity_scope(
                        node_id=item.id, node_name=item.name,
                        node_type=item.node_type,
                        agent_id=str(item.agent_id or ''), agent_role='隔离代码执行器',
                        ):
                            with execution_idempotency_scope(idempotency_key):
                                execution=tool.execute(code)
                        if execution.get('exit_code') != 0:
                            raise RuntimeError(execution.get('stderr') or '隔离代码执行失败')
                        raw=str(execution.get('stdout') or '').strip()
                        if item.execution_contract == 'object':
                            raw = json.dumps(parse_code_result(raw), ensure_ascii=False)
                    else:
                        raw=tool.run(code=code)
                    emit_runtime_event(event_callback, {
                        'type': 'tool.completed', 'node_id': item.id, 'node_name': item.name,
                        'node_type': item.node_type,
                        'output': raw,
                        'tool_name': 'execute_python', 'detail': '代码执行完成',
                    })
                    agent_role='隔离代码执行器'
                elif item.node_type == 'tool':
                    plugin = ((resources or {}).get('plugins') or {}).get(str(item.tool_id))
                    if not plugin or plugin.get('kind') not in {'http', 'python'}:
                        raise ValueError('工具节点必须选择可直接执行的 HTTP 或 Python 工具')
                    tools, _, _ = configured_capabilities(
                        row.workspace_id, row.id, [plugin], app_kind=row.kind,
                        execution_scope=str(runtime_state.get('execution_scope') or 'legacy'),
                        skill_roots=skill_roots or [], receipt_store=receipt_store,
                    )
                    if len(tools) != 1: raise ValueError('工具不存在或未启用')
                    arguments = bind_node_inputs(item.input_bindings, mapped_inputs, self.state.outputs)
                    with execution_idempotency_scope(idempotency_key):
                        value = (tools[0].execute_structured(arguments)['output']
                                 if hasattr(tools[0], 'execute_structured') else tools[0].run(arguments=arguments))
                    if isinstance(value, str):
                        if value.startswith(('HTTP 工具调用失败：', '代码执行失败：')):
                            raise RuntimeError(value)
                        try: value = json.loads(value)
                        except ValueError: pass
                    delivered_tool_files = node_artifacts_from_receipts(
                        receipt_store.items if receipt_store is not None else [], receipt_offset,
                    )
                    tool_object = value if isinstance(value, dict) else {}
                    tool_text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    raw = json.dumps({'object': tool_object, 'file': delivered_tool_files, 'text': tool_text}, ensure_ascii=False)
                    agent_role = '工具执行'
                elif item.node_type=='crew':
                    with runtime_activity_scope(
                        node_id=item.id, node_name=item.name,
                        output_mode=item.output_mode,
                        agent_id=str(item.agent_id or ''), agent_role='Crew',
                    ):
                        with execution_idempotency_scope(idempotency_key):
                            raw, waiting_input, tool_failure = execute_flow_crew(
                                row,item,agents,prompt,mapped_inputs,self.state.outputs,ask_user_store,
                                runtime_state.get('waiting_input')
                                if str(runtime_state.get('pending_node') or '') == str(item.id)
                                else None,
                                cache=spec.cache,
                                output_log_file=output_log_file,
                                profiles=model_profiles,
                                shared_memory=shared_memory,
                                execution_scope=str(runtime_state.get('execution_scope') or 'runtime'),
                                receipt_store=receipt_store,
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
                        node_waiting={'type':'node.waiting_input','node_id':item.id,'node_name':item.name,
                                      'agent_id':item.agent_id,'agent_role':agent_role,
                                      'question':waiting_input['question'],'waiting_input':waiting_input,
                                      'checkpoint':checkpoint.dump()}
                        self.state.events.append(node_waiting); emit_runtime_event(event_callback,node_waiting)
                        self.state.events.append(paused); emit_runtime_event(event_callback,paused); return self.state.output
                else:
                    with runtime_activity_scope(
                        node_id=item.id, node_name=item.name,
                        output_mode=item.output_mode,
                        agent_id=str(item.agent_id or ''),
                        agent_role=getattr(agents.get(item.agent_id), 'role', '') or '',
                    ):
                        with execution_idempotency_scope(idempotency_key):
                            kickoff_options = {}
                            if (item.output_mode == 'json'
                                    and str(item.agent_id or '') not in ask_user_agents
                                    and not _provider_uses_prompt_json(
                                        _agent_profile(spec, item.agent_id, model_profiles))):
                                kickoff_options['response_format'] = _task_output_model(item)
                            result=delegation_scoped_agent(agents[item.agent_id], False).kickoff(prompt, **kickoff_options)
                    raw = user_visible_output(task_output_value(result, item))
                    agent_role=agents[item.agent_id].role
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
                        node_waiting={'type':'node.waiting_input','node_id':item.id,'node_name':item.name,
                                      'agent_id':item.agent_id,'agent_role':agent_role,
                                      'question':waiting_input['question'],'waiting_input':waiting_input,
                                      'checkpoint':checkpoint.dump()}
                        self.state.events.append(node_waiting); emit_runtime_event(event_callback,node_waiting)
                        self.state.events.append(paused); emit_runtime_event(event_callback,paused); return self.state.output
                    agent_role=agents[item.agent_id].role
                if interactive_node and item.node_type != 'crew':
                    raw = completed_collection_output(raw)
                delivered = node_artifacts_from_receipts(
                    receipt_store.items if receipt_store is not None else [], receipt_offset,
                )
                if item.node_type == 'code':
                    try:
                        code_document = json.loads(raw) if isinstance(raw, str) else raw
                    except (TypeError, json.JSONDecodeError, AttributeError):
                        code_document = {}
                    code_output = (code_document.get('output', {})
                                   if isinstance(code_document, dict) else {})
                    code_output = code_output if isinstance(code_output, dict) else {}
                    legacy_text = raw if item.execution_contract == 'legacy' else ''
                    raw = json.dumps({
                        'output': code_output,
                        'file': list(dict.fromkeys(delivered)),
                        'text': str(code_output.get('text') or legacy_text),
                    }, ensure_ascii=False)
                elif item.node_type == 'router':
                    raw = json.dumps({'route': str(raw or '')}, ensure_ascii=False)
                elif item.node_type == 'crew':
                    envelope = json.loads(raw)
                    envelope['file'] = list(dict.fromkeys(delivered))
                    raw = json.dumps(envelope, ensure_ascii=False)
                elif item.node_type == 'tool':
                    try:
                        tool_envelope = json.loads(raw) if isinstance(raw, str) else raw
                    except (TypeError, json.JSONDecodeError):
                        tool_envelope = {'object': {}, 'file': [], 'text': raw}
                    if not isinstance(tool_envelope, dict) or 'object' not in tool_envelope:
                        tool_envelope = json.loads(normalize_node_output(item, tool_envelope))
                    tool_envelope['file'] = list(dict.fromkeys(delivered))
                    raw = json.dumps(tool_envelope, ensure_ascii=False)
                else:
                    envelope = json.loads(normalize_node_output(item, raw))
                    envelope['file'] = list(dict.fromkeys(delivered))
                    raw = json.dumps(envelope, ensure_ascii=False)
                self.state.outputs[item.id]=raw
                checkpoint.complete_node(item.id, raw)
                completed={'type':'node.completed','node_id':item.id,'node_name':item.name,'agent_id':item.agent_id,'agent_role':agent_role,'node_type':item.node_type,'output_mode':getattr(item, 'output_mode', 'text'),'output':raw,
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
    ensure_fixed_output_contracts(definition)
    ensure_executable_contract(definition)
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
    attachment_names = []
    for values in (runtime_state.get('attachment_names') or {}).values():
        if isinstance(values, list):
            attachment_names.extend(str(value) for value in values if str(value).strip())
    if not attachment_names:
        attachment_names = [Path(str(item)).name for item in files if str(item).strip()]
    message_with_files = str(message or '')
    if attachment_names:
        message_with_files = (
            f'{message_with_files}\n\n本轮及会话可用文件：{"、".join(dict.fromkeys(attachment_names))}'
            if message_with_files.strip()
            else f'本轮及会话可用文件：{"、".join(dict.fromkeys(attachment_names))}'
        )
    inputs={
        **dict(runtime_state.get('inputs', {})),
        'message': message_with_files,
        'files': files,
        'conversation_history': history,
    }
    agent_ids_token = _runtime_agent_ids.set({
        str(getattr(agent, 'id', key)): str(key) for key, agent in agents.items()
    })
    callback_token = _runtime_event_callback.set(event_callback)
    emitted_tool_events_token = _runtime_emitted_tool_events.set(frozenset())
    try:
        with runtime_event_stream():
            result=(execute_flow(
                        row,spec,agents,inputs,outputs,runtime_state,event_callback,evidence.get('skill_roots',[]),ask_user_store,
                        evidence.get('receipt_store'), resources, model_profiles or {}, shared,
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
        _runtime_emitted_tool_events.reset(emitted_tool_events_token)
        _runtime_event_callback.reset(callback_token)
        _runtime_agent_ids.reset(agent_ids_token)
        if shared:
            shared.close()
