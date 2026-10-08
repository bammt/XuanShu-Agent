"""When Agent and Task switches turn on.

Every switch is decided from structure the platform can verify (bound
resources, the data-flow graph, the interaction mode, the model profile), from
an explicit choice by the model or the user, or from an explicit deliverable
in the task contract (a file to export, code to generate and run, an object
to return).

Code execution (``allow_code_execution``) is ON when any of these holds:
  * it was set explicitly (model decision in the current turn, confirmed card,
    canvas toggle);
  * a bound Skill contains code (script files, code files or code blocks in
    its instructions), which can only run in the sandbox;
  * a task the Agent executes must generate/run code or produce a file
    (Word/Excel/PDF/PPT/CSV/image...).  Merely reading uploads does not count.
It is OFF for a hierarchical manager, which never executes work.  Reading
uploaded documents and spreadsheets does NOT need it: every Agent has the
built-in ``read_document`` and ``read_spreadsheet`` tools.  HTTP/Python
workspace tools run on their own and do not need it either.

Structured output (``output_mode="json"``) is ON for a node whose ``object``
output another node reads through a placeholder (``{node.object}`` or
``{node.object.field}``), an input binding or a router condition, and for a
node whose expected output explicitly is a JSON/structured object.  JSON and
Markdown are exclusive: a JSON node always has ``markdown=False``.  An
intermediate node that nobody reads structurally is switched back to text,
because JSON there only makes the text worse.  The final node keeps what was
chosen: an API caller may want the object.  ``ask_user`` Agents may be JSON nodes;
they never use provider JSON mode (the runtime uses prompt JSON for them).

Interaction (``user_interaction``) is ON only when all of these hold:
  * the app is ``multi_turn``;
  * it was chosen explicitly: by the model in the current design turn, or by
    the user (canvas toggle / confirmed card).  When they conflict, the user's
    latest request wins; an old value is never re-enabled by a merge;
  * the Agent owns an executable language task: an agent/task node or a member
    of a sequential Crew.  An Agent without a task never asks the user.
It is OFF for every member of a hierarchical Crew, manager included: CrewAI
managers cannot hold any tool, so they cannot use ``ask_user``, and workers
report missing information to the manager.  A Flow that needs to ask the user
puts an interactive Agent node before the hierarchical Crew node.

Multimodal (``multimodal``) is ON only for Agents in an app with image or file
inputs; the runtime additionally requires a vision-capable model profile.

Reasoning, memory and planning stay explicit-only; the defaults are off.
"""
import re
from typing import Any

CODE_FILE_SUFFIXES = ('.py', '.sh', '.bash', '.js', '.mjs', '.cjs', '.ts', '.rb', '.pl', '.ps1', '.r', '.ipynb')
_CODE_BLOCK = re.compile(r'```\s*(python|py|bash|sh|shell|zsh|javascript|js|node|typescript|ts)\b', re.I)
_CODE_WORK = re.compile(
    r'(生成|编写|执行|运行|调用)\s*(python|py)?\s*(代码|脚本|程序)|(执行|运行)\s*python|python\s*(代码|脚本)', re.I)
_FILE_DELIVERY = re.compile(
    r'(导出|生成|输出|交付|保存|创建|制作|排版为|转换为)[^，。；\n]{0,14}'
    r'(word|docx|doc|excel|xlsx|xls|csv|pdf|ppt|pptx|png|jpe?g|图片|图表|压缩包|zip)'
    r'|(word|docx|excel|xlsx|pdf|pptx?)\s*(文件|文档)', re.I)
_NEGATED_DELIVERY = re.compile(
    r'(不|无需|无须|不需要|不含|不包含|不得|禁止|不要|不再)[^，。；\n]{0,10}'
    r'(word|docx|excel|xlsx|pdf|pptx?|文件|代码|脚本)(文件|文档)?', re.I)
_OBJECT_EXPECTED = re.compile(r'json|结构化的?(对象|数据|结果)', re.I)
_NEGATED_OBJECT = re.compile(r'(不|无需|不要|禁止|非|不含)[^，。；\n]{0,6}(json|结构化)', re.I)


def expects_object(expected_output: Any) -> bool:
    """The task contract explicitly asks for a JSON / structured object."""
    text = _NEGATED_OBJECT.sub('', str(expected_output or ''))
    return bool(_OBJECT_EXPECTED.search(text))


def task_requires_code(task: dict | None) -> bool:
    """The task must generate/run code or deliver a real file."""
    task = task or {}
    text = ' '.join(str(task.get(key) or '') for key in ('name', 'description', 'expected_output'))
    text = _NEGATED_DELIVERY.sub('', text)
    return bool(_CODE_WORK.search(text) or _FILE_DELIVERY.search(text))


def agents_requiring_code(definition: dict) -> set[str]:
    """Agents that execute a task whose contract needs the code sandbox."""
    tasks = [item for item in definition.get('tasks', []) or [] if isinstance(item, dict)]
    agent_ids = [str(item.get('id')) for item in definition.get('agents', []) or []
                 if isinstance(item, dict) and item.get('id')]
    top_hierarchical = (definition.get('kind') == 'crew'
                        and (definition.get('process') or definition.get('recommended_process')) == 'hierarchical')
    top_manager = str(definition.get('manager_agent_id') or '')
    result: set[str] = set()
    for task in tasks:
        node_type = str(task.get('node_type') or 'task')
        if node_type in {'task', 'agent'}:
            if not task_requires_code(task):
                continue
            if task.get('agent_id'):
                result.add(str(task['agent_id']))
            elif top_hierarchical:
                result |= {item for item in agent_ids if item != top_manager}
        elif node_type == 'crew':
            nested = [item for item in task.get('crew_tasks', []) or [] if isinstance(item, dict)]
            members = {str(item) for item in task.get('crew_agent_ids', []) or [] if item}
            manager = str(task.get('crew_manager_agent_id') or '')
            workers = members - {manager}
            hierarchical = str(task.get('crew_process') or 'sequential') == 'hierarchical'
            hit = False
            for item in nested:
                if task_requires_code(item):
                    hit = True
                    if item.get('agent_id') and not hierarchical:
                        result.add(str(item['agent_id']))
                    else:
                        result |= workers
            if not hit and task_requires_code(task):
                last = str((nested[-1] if nested else {}).get('agent_id') or '')
                if hierarchical or not last:
                    result |= workers
                else:
                    result.add(last)
    return result

STRUCTURED_FIELDS = {'object'}
LANGUAGE_NODE_TYPES = {'task', 'agent', 'crew'}


def skill_requires_code(skill: dict | None) -> bool:
    """A Skill needs the sandbox whenever it contains any code."""
    for item in (skill or {}).get('files', []) or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get('path') or item.get('name') or '').lower()
        kind = str(item.get('kind') or item.get('type') or '').lower()
        if (item.get('executable') or path.startswith('scripts/') or '/scripts/' in path
                or path.endswith(CODE_FILE_SUFFIXES) or kind in {'script', 'code'}):
            return True
    if _CODE_BLOCK.search(str((skill or {}).get('instructions') or '')):
        return True
    return bool((skill or {}).get('has_scripts'))


def _skill_details(resources: dict | None) -> dict[str, dict]:
    """Merge Skill metadata from every place a caller may provide it."""
    resources = resources or {}
    merged: dict[str, dict] = {}
    for source in (resources.get('skills'), (resources.get('selected_resource_details') or {}).get('skills')):
        items = source.values() if isinstance(source, dict) else source or []
        for item in items:
            if isinstance(item, dict) and item.get('id') is not None:
                merged.setdefault(str(item['id']), {}).update(item)
    return merged


def agent_requires_code(agent: dict, resources: dict | None = None, *, is_manager: bool = False) -> bool:
    if is_manager:
        return False
    if agent.get('allow_code_execution'):
        return True
    skills = _skill_details(resources)
    return any(skill_requires_code(skills.get(str(skill_id))) for skill_id in agent.get('skills', []) or [])


def _placeholder_sources(text: str) -> set[str]:
    """Node IDs whose ``object`` field a prompt reads, e.g. ``{review.object.risk}``."""
    sources = set()
    start = text.find('{')
    while start != -1:
        end = text.find('}', start + 1)
        if end == -1:
            break
        parts = text[start + 1:end].split('.')
        if len(parts) >= 2 and parts[1] in STRUCTURED_FIELDS and parts[0]:
            sources.add(parts[0])
        start = text.find('{', start + 1)
    return sources


def _binding_sources(bindings: Any) -> set[str]:
    sources = set()
    for binding in (bindings or {}).values() if isinstance(bindings, dict) else []:
        if isinstance(binding, dict) and binding.get('source') == 'node' and binding.get('node_id'):
            path = str(binding.get('variable') or '')
            if path.split('.', 1)[0] in STRUCTURED_FIELDS:
                sources.add(str(binding['node_id']))
    return sources


def _condition_sources(expression: Any) -> set[str]:
    if not isinstance(expression, dict):
        return set()
    if expression.get('type') == 'group':
        return set().union(*(_condition_sources(child) for child in expression.get('conditions') or []))
    return _binding_sources({'value': expression})


def structured_consumers(tasks: list[dict]) -> set[str]:
    """IDs of nodes whose structured ``object`` output is read downstream."""
    sources: set[str] = set()
    for task in tasks:
        for field in ('description', 'expected_output', 'objective'):
            sources |= _placeholder_sources(str(task.get(field) or ''))
        sources |= _binding_sources(task.get('input_bindings'))
        for rule in task.get('router_rules') or []:
            if isinstance(rule, dict):
                sources |= _condition_sources(rule.get('expression'))
        for nested in task.get('crew_tasks') or []:
            if isinstance(nested, dict):
                for field in ('description', 'expected_output'):
                    sources |= _placeholder_sources(str(nested.get(field) or ''))
    return sources


def _final_task_ids(tasks: list[dict]) -> set[str]:
    depended_on = {str(dep) for task in tasks for dep in task.get('depends_on', []) or []}
    return {str(task.get('id')) for task in tasks if str(task.get('id')) not in depended_on}


def interaction_capable_agents(definition: dict) -> set[str]:
    """Agent IDs that may structurally hold ``ask_user``."""
    if definition.get('interaction_mode') != 'multi_turn':
        return set()
    kind = str(definition.get('kind') or definition.get('recommended_kind') or '')
    process = str(definition.get('process') or definition.get('recommended_process') or '')
    if kind == 'crew' and process == 'hierarchical':
        return set()
    owners: set[str] = set()
    for task in definition.get('tasks', []) or []:
        if not isinstance(task, dict):
            continue
        node_type = str(task.get('node_type') or 'task')
        if node_type in {'task', 'agent'} and task.get('agent_id'):
            owners.add(str(task['agent_id']))
        elif node_type == 'crew' and str(task.get('crew_process') or 'sequential') != 'hierarchical':
            owners |= {str(value) for value in task.get('crew_agent_ids', []) or [] if value}
            owners |= {str(item.get('agent_id')) for item in task.get('crew_tasks', []) or []
                       if isinstance(item, dict) and item.get('agent_id')}
    return owners


def apply_interaction_policy(definition: dict) -> dict:
    """Keep an explicit ``user_interaction`` only where it can work."""
    allowed = interaction_capable_agents(definition)
    for agent in definition.get('agents', []) or []:
        if isinstance(agent, dict):
            agent['user_interaction'] = bool(agent.get('user_interaction')) and str(agent.get('id') or '') in allowed
    return definition


def apply_output_mode_policy(tasks: list[dict], interactive_agent_ids: set[str] | None = None) -> list[dict]:
    consumed = structured_consumers(tasks)
    finals = _final_task_ids(tasks)
    interactive = interactive_agent_ids or set()
    for task in tasks:
        task_id = str(task.get('id') or '')
        if str(task.get('node_type') or 'task') not in LANGUAGE_NODE_TYPES:
            continue
        # ask_user nodes can be JSON nodes too: the runtime uses prompt JSON
        # for them instead of provider JSON mode.
        if task_id in consumed or expects_object(task.get('expected_output')):
            task['output_mode'] = 'json'
            expected = str(task.get('expected_output') or '')
            if 'json' not in expected.casefold():
                task['expected_output'] = (expected or '完成结构化结果') + ' 必须返回 JSON 对象，并明确 object 字段。'
        elif task_id not in finals:
            task['output_mode'] = 'text'
        for nested in task.get('crew_tasks', []) or []:
            if isinstance(nested, dict) and expects_object(nested.get('expected_output')):
                nested['output_mode'] = 'json'
            if isinstance(nested, dict) and nested.get('output_mode') == 'json':
                nested['markdown'] = False
        if task.get('output_mode') == 'json':
            # JSON and Markdown are exclusive; the runtime ignores Markdown
            # for JSON nodes, so the saved switch must say the same.
            task['markdown'] = False
    return tasks


def apply_capability_policy(definition: dict, resources: dict | None = None) -> dict:
    """Decide every switch on an Agent/Task definition in one place."""
    agents = [item for item in definition.get('agents', []) or [] if isinstance(item, dict)]
    tasks = [item for item in definition.get('tasks', []) or [] if isinstance(item, dict)]
    multi_turn = definition.get('interaction_mode') == 'multi_turn'
    hierarchical_crew = definition.get('kind') == 'crew' and (
        definition.get('process') or definition.get('recommended_process')) == 'hierarchical'
    manager_id = str(definition.get('manager_agent_id') or '') if hierarchical_crew else ''
    has_media_inputs = any(
        str(item.get('input_type') or item.get('type') or '') in {'file', 'image'}
        for item in definition.get('inputs', []) or [] if isinstance(item, dict)
    )
    task_code_agents = agents_requiring_code(definition)
    for agent in agents:
        agent_id = str(agent.get('id') or '')
        is_manager = bool(manager_id) and agent_id == manager_id
        agent['allow_code_execution'] = (not is_manager) and (
            agent_requires_code(agent, resources, is_manager=is_manager) or agent_id in task_code_agents)
        agent['multimodal'] = has_media_inputs
    apply_interaction_policy(definition)
    interactive = {str(agent.get('id')) for agent in agents if agent.get('user_interaction')}
    apply_output_mode_policy(tasks, interactive)
    return definition
