import hashlib
import json
import re
import ast
from typing import Any


VARIABLE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
PLACEHOLDER = re.compile(r'(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}(?!\})')
BUILTIN_VARIABLES = {'message', 'files', 'conversation_history'}
TEXT_OUTPUT_NODE_TYPES = {'task', 'agent', 'crew'}

ROUTE_REGEX_MAX_PATTERN = 200
ROUTE_REGEX_MAX_TEXT = 20_000


def _has_repeated_complex_group(pattern: str) -> bool:
    """Detect a repeated group whose body can match the same text many ways.

    ``(a+)+``, ``(\\w*)*``, ``(a|aa)+`` and ``((a))+`` are the shapes behind
    catastrophic backtracking.  The standard ``re`` module has no timeout, so
    any group containing a quantifier, an alternation or a nested group may
    not itself be repeated.  Simple repeated groups such as ``(ab)+`` stay
    allowed.
    """
    stack: list[bool] = []
    index = 0
    in_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == '\\':
            index += 2
            continue
        if in_class:
            in_class = char != ']'
        elif char == '[':
            in_class = True
        elif char == '(':
            if stack:
                stack[-1] = True
            stack.append(False)
            if pattern[index + 1:index + 2] == '?':
                # Skip a group prefix such as ``(?:``, ``(?=`` or ``(?P<``;
                # its ``?`` is syntax, not a quantifier.
                index += 2
        elif char == ')' and stack:
            complex_body = stack.pop()
            following = pattern[index + 1:index + 2]
            repeated = following in {'*', '+'} or (
                following == '{' and ',' in pattern[index + 1:pattern.find('}', index + 1) + 1]
            )
            if complex_body and repeated:
                return True
            if stack and following in {'*', '+', '?', '{'}:
                stack[-1] = True
        elif char in '*+?{|' and stack:
            stack[-1] = True
        index += 1
    return False


def unsafe_route_regex(pattern: str) -> str:
    """Return a reason when a router regex is unsafe to evaluate, else ''."""
    if len(pattern) > ROUTE_REGEX_MAX_PATTERN:
        return f'路由正则长度不能超过 {ROUTE_REGEX_MAX_PATTERN} 个字符'
    if _has_repeated_complex_group(pattern):
        return '路由正则不能包含嵌套量词（例如 (a+)+），这类表达式可能导致执行卡死'
    try:
        re.compile(pattern)
    except re.error as exc:
        return f'路由正则无效：{exc}'
    return ''


# Output names and types are platform-owned contracts. Producers may have an
# empty value at runtime, but editors and consumers must see the same fields.
LANGUAGE_OUTPUTS = [
    {'name': 'object', 'value_type': 'object', 'description': '模型生成的结构化 JSON 对象'},
    {'name': 'file', 'value_type': 'file', 'description': '此节点实际生成或接收的文件列表'},
    {'name': 'text', 'value_type': 'string', 'description': '面向用户的正文文本'},
]
TOOL_OUTPUTS = [
    {'name': 'object', 'value_type': 'object', 'description': '工具的结构化返回值'},
    {'name': 'file', 'value_type': 'file', 'description': '工具实际生成的文件列表'},
    {'name': 'text', 'value_type': 'string', 'description': '工具返回的文本表示'},
]
CODE_OUTPUTS = [
    {'name': 'output', 'value_type': 'object', 'description': 'main() 返回的字典对象'},
    {'name': 'file', 'value_type': 'file', 'description': '代码节点实际生成的文件列表'},
    {'name': 'text', 'value_type': 'string', 'description': 'output.text 的文本值（若存在）'},
]
ROUTER_OUTPUTS = [
    {'name': 'route', 'value_type': 'string', 'description': '命中的分支 ID'},
]


def fixed_output_contract(node_type: str) -> list[dict[str, str]]:
    if node_type in {'task', 'agent', 'crew'}:
        return [dict(field) for field in LANGUAGE_OUTPUTS]
    if node_type == 'tool':
        return [dict(field) for field in TOOL_OUTPUTS]
    if node_type == 'code':
        return [dict(field) for field in CODE_OUTPUTS]
    if node_type == 'router':
        return [dict(field) for field in ROUTER_OUTPUTS]
    return []


def _canonical_output_name(name: str, fields: list[dict[str, Any]], node_type: str) -> str:
    if name == '$raw':
        return '$raw'
    fixed_names = {field['name'] for field in fixed_output_contract(node_type)}
    if name in fixed_names:
        return name
    field = next((item for item in fields if str(item.get('name') or '') == name), {})
    if not field and name not in {'result', 'output', 'generated_files'}:
        return name
    value_type = str(field.get('value_type') or 'string')
    if name == 'generated_files':
        return 'file'
    if not field and name == 'output' and node_type == 'tool':
        return 'object'
    if node_type == 'code':
        return 'file' if value_type == 'file' else 'text' if value_type == 'string' else 'output'
    if node_type == 'router':
        return 'route'
    if value_type == 'file':
        return 'file'
    if value_type == 'string' or (not field and name in {'result', 'output'}):
        return 'text'
    return 'object'


def _replace_placeholder(node: dict[str, Any], old: str, new: str) -> None:
    if not old or old == new:
        return
    for field in ('description', 'objective', 'expected_output'):
        value = node.get(field)
        if isinstance(value, str):
            node[field] = value.replace('{' + old + '}', '{' + new + '}')


def _migrate_node_references(node: dict[str, Any], sources: dict[str, dict[str, Any]]) -> None:
    node_type = str(node.get('node_type') or 'task')
    mappings = node.get('dependency_variables') or {}
    bindings: dict[str, dict[str, Any]] = {}
    if node_type in {'code', 'tool'}:
        raw_bindings = node.get('input_bindings')
        if isinstance(raw_bindings, dict):
            bindings = raw_bindings
        elif isinstance(raw_bindings, list):
            # Older persisted definitions used an editor-facing list while
            # the runtime/schema contract is a name-keyed mapping.
            for item in raw_bindings:
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name') or item.get('target') or '').strip()
                if name:
                    bindings[name] = item
        node['input_bindings'] = bindings
    if isinstance(mappings, dict):
        for source_id, raw_mappings in mappings.items():
            source = sources.get(str(source_id), {})
            source_type = str(source.get('node_type') or 'task')
            source_fields = [
                field for field in source.get('output_variables', []) or []
                if isinstance(field, dict)
            ]
            for mapping in _mapping_items(raw_mappings):
                if not isinstance(mapping, dict):
                    continue
                old_source = str(mapping.get('source_variable') or '$raw').strip()
                target = str(mapping.get('target_variable') or '').strip()
                canonical = _canonical_output_name(old_source, source_fields, source_type)
                if target:
                    _replace_placeholder(node, target, f'{source_id}.{canonical}')
                    if node_type in {'code', 'tool'} and target not in bindings:
                        bindings[target] = {
                            'source': 'node',
                            'node_id': str(source_id),
                            'variable': canonical,
                            'value_type': next(
                                (field['value_type'] for field in fixed_output_contract(source_type)
                                 if field['name'] == canonical),
                                'object',
                            ),
                        }
    node['dependency_variables'] = {}

    for binding in bindings.values():
        if not isinstance(binding, dict) or binding.get('source') != 'node':
            continue
        source_id = str(binding.get('node_id') or '')
        source = sources.get(source_id, {})
        path = str(binding.get('variable') or '$raw')
        if path == '$raw':
            continue
        root, separator, tail = path.partition('.')
        canonical = _canonical_output_name(
            root,
            [field for field in source.get('output_variables', []) or [] if isinstance(field, dict)],
            str(source.get('node_type') or 'task'),
        )
        binding['variable'] = canonical + (separator + tail if separator else '')

    def migrate_condition(expression: Any) -> None:
        if not isinstance(expression, dict):
            return
        if expression.get('type') == 'group':
            for child in expression.get('conditions') or []:
                migrate_condition(child)
            return
        if expression.get('source') != 'node':
            return
        source = sources.get(str(expression.get('node_id') or ''), {})
        path = str(expression.get('variable') or '$raw')
        if path == '$raw':
            return
        root, separator, tail = path.partition('.')
        canonical = _canonical_output_name(
            root,
            [field for field in source.get('output_variables', []) or [] if isinstance(field, dict)],
            str(source.get('node_type') or 'task'),
        )
        expression['variable'] = canonical + (separator + tail if separator else '')

    for rule in node.get('router_rules') or []:
        if isinstance(rule, dict):
            migrate_condition(rule.get('expression'))


def ensure_fixed_output_contracts(definition: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy references, then apply platform-owned output contracts."""
    tasks = [task for task in definition.get('tasks', []) or [] if isinstance(task, dict)]
    sources = {str(task.get('id') or ''): task for task in tasks}
    for task in tasks:
        _migrate_node_references(task, sources)
        ancestors = upstream_node_ids(str(task.get('id') or ''), tasks)
        legacy_names: dict[str, list[tuple[str, str]]] = {}
        for source_id in ancestors:
            source = sources[source_id]
            source_type = str(source.get('node_type') or 'task')
            default_field = 'route' if source_type == 'router' else 'text'
            _replace_placeholder(task, source_id, f'{source_id}.{default_field}')
            for field in source.get('output_variables', []) or []:
                if not isinstance(field, dict):
                    continue
                old_name = str(field.get('name') or '')
                if old_name and old_name != source_id:
                    canonical = _canonical_output_name(
                        old_name, source.get('output_variables', []), source_type,
                    )
                    _replace_placeholder(task, f'{source_id}.{old_name}', f'{source_id}.{canonical}')
                    if canonical != old_name:
                        legacy_names.setdefault(old_name, []).append((source_id, canonical))
        for old_name, matches in legacy_names.items():
            if len(matches) == 1:
                source_id, canonical = matches[0]
                _replace_placeholder(task, old_name, f'{source_id}.{canonical}')
        nested = [item for item in task.get('crew_tasks', []) or [] if isinstance(item, dict)]
        nested_sources = {str(item.get('id') or ''): item for item in nested}
        for item in nested:
            _migrate_node_references(item, nested_sources)
            for source_id in upstream_node_ids(str(item.get('id') or ''), nested):
                _replace_placeholder(item, source_id, f'{source_id}.text')

    for task in tasks:
        if not isinstance(task, dict):
            continue
        node_type = str(task.get('node_type') or 'task')
        task['output_variables'] = fixed_output_contract(node_type)
        if node_type == 'crew':
            for nested in task.get('crew_tasks', []) or []:
                if isinstance(nested, dict):
                    nested['output_variables'] = fixed_output_contract('task')
    return definition


def upstream_node_ids(task_id: str, tasks: list[dict[str, Any]]) -> set[str]:
    """Return all transitive predecessors of a node in its dependency graph."""
    by_id = {str(item.get('id') or ''): item for item in tasks if isinstance(item, dict)}
    found: set[str] = set()
    pending = list(by_id.get(str(task_id), {}).get('depends_on') or [])
    while pending:
        current = str(pending.pop())
        if current in found or current not in by_id:
            continue
        found.add(current)
        pending.extend(by_id[current].get('depends_on') or [])
    return found


def _text_output_field() -> dict[str, str]:
    return {
        'name': 'text',
        'value_type': 'string',
        'description': '面向用户的正文结果，可被下游节点引用',
    }


def ensure_text_output_contract(definition: dict[str, Any]) -> dict[str, Any]:
    """Give every language-producing node an explicit user-facing text output.

    A file/object field describes an artifact, but it is not a conversational
    result.  Older generated Flow documents often declared only ``document``
    or ``final_file``; that made downstream nodes and the final response guess
    where the prose lived.  Keep those fields and add one canonical ``text``
    field.  Deterministic code/tool nodes intentionally keep their object
    envelope as their contract and are handled separately.
    """
    for task in definition.get('tasks', []) or []:
        if not isinstance(task, dict):
            continue
        nodes = [task]
        if str(task.get('node_type') or '') == 'crew':
            nodes.extend(item for item in task.get('crew_tasks', []) or []
                         if isinstance(item, dict))
        for node in nodes:
            node_type = str(node.get('node_type') or task.get('node_type') or 'task')
            if node_type not in TEXT_OUTPUT_NODE_TYPES:
                continue
            values = node.get('output_variables')
            if not isinstance(values, list):
                values = []
            normalized = [dict(value) for value in values if isinstance(value, dict)]
            text_field = next(
                (value for value in normalized
                 if str(value.get('name') or '').strip() == 'text'),
                None,
            )
            if text_field is None:
                normalized.append(_text_output_field())
            else:
                text_field['value_type'] = 'string'
                text_field.setdefault('description', _text_output_field()['description'])
            node['output_variables'] = normalized
    return definition


def code_signature(source: str) -> tuple[set[str], set[str], bool]:
    """Return (declared, required, accepts_kwargs) for a code node main()."""
    try:
        tree = ast.parse(str(source or ''))
    except SyntaxError:
        return set(), set(), False
    function = next(
        (item for item in tree.body
         if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
         and item.name == 'main'),
        None,
    )
    if function is None:
        return set(), set(), False
    positional = [*function.args.posonlyargs, *function.args.args]
    keyword_only = list(function.args.kwonlyargs)
    declared = {item.arg for item in [*positional, *keyword_only]}
    defaults = [None] * (len(positional) - len(function.args.defaults)) + list(function.args.defaults)
    required = {item.arg for item, default in zip(positional, defaults) if default is None}
    required.update(item.arg for item, default in zip(keyword_only, function.args.kw_defaults)
                    if default is None)
    return declared, required, function.args.kwarg is not None


def ensure_deterministic_output_contract(definition: dict[str, Any]) -> dict[str, Any]:
    """Persist fixed output fields for deterministic Flow nodes.

    Pydantic validation also enforces this invariant, but validation returns a
    new model and does not mutate the raw workflow document written to the
    database. Normalize the document before both validation and persistence so
    the canvas, saved JSON, and runtime all see the same contract.
    """
    for task in definition.get('tasks', []) or []:
        if isinstance(task, dict) and str(task.get('node_type') or '') in {'code', 'tool'}:
            task['output_variables'] = fixed_output_contract(str(task.get('node_type')))
    return definition


def ensure_file_output_contract(definition: dict[str, Any]) -> dict[str, Any]:
    """Compatibility shim: files are already part of every fixed contract."""
    return definition


def execution_order(definition: dict[str, Any]) -> list[str]:
    """Return the stable topological order shared by canvas, code and runtime."""
    tasks = list(definition.get('tasks', []) or [])
    task_ids = [str(item.get('id') or '') for item in tasks]
    pending = {
        task_id: {str(value) for value in task.get('depends_on', [])}
        for task_id, task in zip(task_ids, tasks)
    }
    ordered: list[str] = []
    while pending:
        ready = [task_id for task_id in task_ids if task_id in pending and pending[task_id] <= set(ordered)]
        if not ready:
            raise ValueError('Flow 存在循环依赖')
        ordered.extend(ready)
        for task_id in ready:
            pending.pop(task_id)
    return ordered


def execution_graph(definition: dict[str, Any]) -> dict[str, Any]:
    order = execution_order(definition)
    nodes = [
        {
            'id': str(item.get('id') or ''),
            'type': str(item.get('node_type') or 'task'),
            'agent_id': item.get('agent_id'),
        }
        for item in definition.get('tasks', []) or []
    ]
    edges = [
        {'source': str(source), 'target': str(item.get('id') or ''), 'type': 'dependency'}
        for item in definition.get('tasks', []) or []
        for source in item.get('depends_on', []) or []
    ]
    canonical = {'nodes': nodes, 'edges': edges, 'order': order}
    canonical['digest'] = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return canonical


def _input_names(definition: dict[str, Any]) -> list[str]:
    # Composer-stage inputs use ``variable`` while persisted workflow inputs
    # store that same machine name in ``name``.
    return [
        str(item.get('variable') or item.get('name') or '').strip()
        for item in definition.get('inputs', [])
    ]


def _mapping_items(raw_mappings: Any) -> list[Any]:
    return raw_mappings if isinstance(raw_mappings, list) else [raw_mappings]


def normalize_deterministic_dependencies(definition: dict[str, Any]) -> dict[str, Any]:
    """Make deterministic input bindings part of the executable graph.

    Code and direct tool nodes use ``input_bindings`` as their parameter
    contract. A node binding therefore also means that the source node must
    execute first, even when an API client omitted the redundant ``depends_on``
    entry. Agent/Crew prompt mappings intentionally keep their explicit edge
    requirement and are not changed here.
    """
    tasks = definition.get('tasks', []) or []
    valid_ids = {
        str(item.get('id') or '') for item in tasks if isinstance(item, dict)
    }
    for task in tasks:
        if not isinstance(task, dict):
            continue
        raw_mappings = task.get('dependency_variables')
        if isinstance(raw_mappings, dict):
            # A weak model often emits a placeholder mapping such as
            # ``{"source_variable": "object", "target_variable": ""}``.
            # It conveys no executable binding and must not make an otherwise
            # valid Crew graph fail the whole generation stage.
            cleaned = {}
            for dependency_id, raw_items in raw_mappings.items():
                items = [
                    item for item in _mapping_items(raw_items)
                    if isinstance(item, dict) and str(item.get('target_variable') or '').strip()
                ]
                if items:
                    cleaned[str(dependency_id)] = items
            task['dependency_variables'] = cleaned
        dependencies = list(task.get('depends_on') or [])
        if str(task.get('node_type') or '') == 'router':
            def visit(expression: Any) -> None:
                if not isinstance(expression, dict):
                    return
                if expression.get('type') == 'group':
                    for child in expression.get('conditions') or []:
                        visit(child)
                elif expression.get('source') == 'node':
                    dependency_id = expression.get('node_id')
                    if dependency_id and dependency_id not in dependencies:
                        dependencies.append(dependency_id)
            for rule in task.get('router_rules') or []:
                visit(rule.get('expression') if isinstance(rule, dict) else None)
            task['depends_on'] = dependencies
            continue
        if str(task.get('node_type') or '') not in {'code', 'tool'}:
            # Flow language nodes refer to reachable outputs directly in
            # their prompt (for example {t_intake.object}); make that edge
            # explicit so visibility validation and execution agree. The
            # old dependency_variables mapping is a Crew-only compatibility
            # format and must not create empty target errors for Flow nodes.
            if str(definition.get('kind') or '') == 'flow':
                dependencies = list(task.get('depends_on') or [])
                task_id = str(task.get('id') or '')
                for field in ('description', 'objective', 'expected_output'):
                    for placeholder in PLACEHOLDER.findall(str(task.get(field) or '')):
                        source_id = placeholder.split('.', 1)[0]
                        if '.' in placeholder and source_id in valid_ids and source_id != task_id and source_id not in dependencies:
                            dependencies.append(source_id)
                task['depends_on'] = dependencies
                if str(task.get('node_type') or '') != 'crew':
                    task['dependency_variables'] = {}
            continue
        for dependency_id in (task.get('dependency_variables') or {}):
            if dependency_id not in dependencies:
                dependencies.append(dependency_id)
        for binding in (task.get('input_bindings') or {}).values():
            if isinstance(binding, dict) and binding.get('source') == 'node':
                dependency_id = str(binding.get('node_id') or '')
                ancestors = upstream_node_ids(str(task.get('id') or ''), tasks)
                if (dependency_id and dependency_id in valid_ids
                        and dependency_id not in dependencies
                        and dependency_id not in ancestors):
                    dependencies.append(dependency_id)
        task['depends_on'] = dependencies
    return definition


def _mapping_targets(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    return {
        str(mapping.get('target_variable') or '').strip()
        for raw_mappings in value.values()
        for mapping in _mapping_items(raw_mappings)
        if isinstance(mapping, dict) and mapping.get('target_variable')
    }


def _prompt_fields(definition: dict[str, Any]):
    """Yield every executable prompt together with variables visible at that point."""
    configured = set(_input_names(definition))
    for task in definition.get('tasks', []):
        predecessors = upstream_node_ids(str(task.get('id') or ''), definition.get('tasks', []))
        visible = configured | BUILTIN_VARIABLES
        for node_id in predecessors:
            source = next((candidate for candidate in definition.get('tasks', [])
                           if str(candidate.get('id') or '') == node_id), None)
            if source:
                fields = source.get('output_variables', []) or []
                visible.update(f'{node_id}.{field.get("name")}' for field in fields if field.get('name'))
        visible |= _mapping_targets(task.get('dependency_variables'))
        label = str(task.get('name') or task.get('id') or '未命名任务')
        yield label, 'description', str(task.get('description') or task.get('objective') or ''), visible
        yield label, 'expected_output', str(task.get('expected_output') or ''), visible
        if task.get('node_type') == 'code' and task.get('execution_contract') != 'object':
            yield label, 'code_snippet', str(task.get('code_snippet') or ''), visible
        for nested in task.get('crew_tasks', []):
            nested_label = f"{label} / {nested.get('name') or nested.get('id') or '内部任务'}"
            # CrewAI provides upstream internal Task output through ``context``;
            # an explicit placeholder is valid only when a dependency mapping
            # gives that value a concrete downstream name.
            nested_visible = set(visible) | _mapping_targets(nested.get('dependency_variables'))
            public_parent = str(task.get('id') or '').replace('_', '')
            nested_predecessors = upstream_node_ids(str(nested.get('id') or ''), task.get('crew_tasks', []))
            for node_id in nested_predecessors:
                source = next((candidate for candidate in task.get('crew_tasks', [])
                               if str(candidate.get('id') or '') == node_id), None)
                if source:
                    fields = source.get('output_variables', []) or []
                    nested_visible.update(f'{node_id}.{field.get("name")}' for field in fields if field.get('name'))
                    nested_visible.update(
                        f'{public_parent}.{str(node_id).replace("_", "")}.{field.get("name")}'
                        for field in fields if field.get('name')
                    )
            # A Crew task's own published path is useful when another nested
            # task or the Flow editor references it explicitly.
            nested_fields = nested.get('output_variables', []) or []
            nested_visible.update(
                f'{public_parent}.{str(nested.get("id") or "").replace("_", "")}.{field.get("name")}'
                for field in nested_fields if isinstance(field, dict) and field.get('name')
            )
            yield nested_label, 'description', str(nested.get('description') or nested.get('objective') or ''), nested_visible
            yield nested_label, 'expected_output', str(nested.get('expected_output') or ''), nested_visible


def _validate_nested_task_contracts(parent: dict[str, Any], errors: list[str]) -> None:
    """Validate variable transfer inside an embedded Crew node."""
    tasks = [item for item in parent.get('crew_tasks', []) or [] if isinstance(item, dict)]
    if not tasks:
        return
    parent_label = str(parent.get('name') or parent.get('id') or '未命名 Crew')
    task_ids = [str(item.get('id') or '').strip() for item in tasks]
    duplicate_tasks = sorted({task_id for task_id in task_ids if task_id and task_ids.count(task_id) > 1})
    if duplicate_tasks:
        errors.append(f"Crew 节点“{parent_label}”内部任务 ID 重复：{', '.join(duplicate_tasks)}")
    task_by_id = {task_id: item for task_id, item in zip(task_ids, tasks) if task_id}
    output_names_by_task: dict[str, set[str]] = {}
    for task_id, task in task_by_id.items():
        label = f"{parent_label} / {task.get('name') or task_id}"
        fields = [item for item in task.get('output_variables', []) or [] if isinstance(item, dict)]
        names_for_task = [str(item.get('name') or '').strip() for item in fields]
        output_names_by_task[task_id] = {name for name in names_for_task if name}
        duplicate_outputs = sorted({name for name in names_for_task if name and names_for_task.count(name) > 1})
        if duplicate_outputs:
            errors.append(f"任务“{label}”输出变量重复：{', '.join(duplicate_outputs)}")
        invalid_outputs = sorted({name for name in names_for_task if name and not VARIABLE_NAME.fullmatch(name)})
        if invalid_outputs:
            errors.append(f"任务“{label}”输出变量必须使用英文 snake_case：{', '.join(invalid_outputs)}")

    for task_id, task in task_by_id.items():
        label = f"{parent_label} / {task.get('name') or task_id}"
        dependencies = {str(value).strip() for value in task.get('depends_on', []) or []}
        for dependency_id in dependencies:
            if dependency_id not in task_by_id:
                errors.append(f"任务“{label}”依赖了不存在的内部任务：{dependency_id}")
        mappings = task.get('dependency_variables') or {}
        if not isinstance(mappings, dict):
            errors.append(f"任务“{label}”的 dependency_variables 必须是对象")
            continue
        for dependency, raw_mappings in mappings.items():
            dependency_id = str(dependency).strip()
            if dependency_id not in task_by_id:
                errors.append(f"任务“{label}”映射了不存在的内部上游任务：{dependency_id}")
                continue
            if dependency_id not in dependencies and dependency_id not in upstream_node_ids(task_id, tasks):
                errors.append(f"任务“{label}”映射的内部上游“{dependency_id}”不在上游路径中")
            upstream_outputs = output_names_by_task.get(dependency_id, set())
            upstream_types = {
                str(item.get('name') or '').strip(): str(item.get('value_type') or 'string')
                for item in (task_by_id.get(dependency_id, {}).get('output_variables') or [])
                if isinstance(item, dict) and item.get('name')
            }
            for mapping in _mapping_items(raw_mappings):
                if not isinstance(mapping, dict):
                    errors.append(f"任务“{label}”存在无效的内部变量映射")
                    continue
                source = str(mapping.get('source_variable') or 'result').strip()
                target = str(mapping.get('target_variable') or '').strip()
                if source not in {'result', '$raw'} and source not in upstream_outputs:
                    errors.append(
                        f"任务“{label}”引用内部上游“{dependency_id}”不存在的输出变量：{source}"
                    )
                if not target or not VARIABLE_NAME.fullmatch(target):
                    errors.append(
                        f"任务“{label}”的内部变量映射目标必须使用英文 snake_case：{target or '空'}"
                    )
                elif (
                    str(parent.get('node_type') or 'task') in TEXT_OUTPUT_NODE_TYPES
                    and upstream_types.get(source) != 'file'
                    and not any(
                        f'{{{target}}}' in str(task.get(field) or '')
                        for field in ('description', 'objective', 'expected_output')
                    )
                ):
                    errors.append(
                        f"任务“{label}”的内部映射目标 {{{target}}} 未被 description 或 expected_output 使用"
                    )


def _visible_object_path(name: str, visible: set[str]) -> bool:
    """``{node.object.a.b}`` is valid wherever ``{node.object}`` is visible."""
    head, sep, path = name.partition('.object.')
    return bool(sep and path and f'{head}.object' in visible
                and all(VARIABLE_NAME.fullmatch(part) for part in path.split('.')))


RESERVED_BINDING_INPUTS = {'message', 'files', 'conversation_history'}


def _tool_input_schemas(resources: dict | None) -> dict[str, dict]:
    """Tool ``input_schema`` by tool ID, from every place a caller may provide it."""
    resources = resources or {}
    schemas: dict[str, dict] = {}
    for source in (resources.get('tools'), (resources.get('selected_resource_details') or {}).get('tools')):
        items = source.values() if isinstance(source, dict) else source or []
        for item in items:
            if isinstance(item, dict) and item.get('id') is not None and isinstance(item.get('input_schema'), dict):
                schemas[str(item['id'])] = item['input_schema']
    return schemas


def code_unbound_names(source: str) -> set[str]:
    """Names ``main()`` reads that are neither parameters, locals, module names nor builtins.

    A code node receives upstream data only as ``main`` arguments.  Using a
    name such as ``content`` without declaring it raises NameError at run time.
    """
    import builtins
    import symtable
    try:
        top = symtable.symtable(str(source or ''), '<code_node>', 'exec')
    except SyntaxError:
        return set()
    main = next((child for child in top.get_children()
                 if child.get_name() == 'main' and child.get_type() == 'function'), None)
    if main is None:
        return set()
    module_names = {item.get_name() for item in top.get_symbols()
                    if item.is_assigned() or item.is_imported()}
    names: set[str] = set()

    def walk(table) -> None:
        for item in table.get_symbols():
            if item.is_referenced() and item.is_global():
                names.add(item.get_name())
        for child in table.get_children():
            walk(child)

    walk(main)
    return {name for name in names if name not in module_names and not hasattr(builtins, name)}


def code_defines_main(source: str) -> bool:
    try:
        tree = ast.parse(str(source or ''))
    except SyntaxError:
        return False
    return any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == 'main'
               for item in tree.body)


def force_object_code_contract(definition: dict[str, Any]) -> dict[str, Any]:
    """Generated code nodes always run as ``main(**input_bindings)``.

    The model often omits ``execution_contract``; the schema default
    ``legacy`` then renders the source as a template and never calls main,
    and the binding checks (which apply to ``object``) are skipped.
    """
    for task in definition.get('tasks', []) or []:
        if isinstance(task, dict) and str(task.get('node_type') or '') == 'code':
            task['execution_contract'] = 'object'
    return definition


def generation_code_node_errors(definition: dict[str, Any]) -> list[str]:
    """Generation-stage completeness of code nodes (not applied to old saved apps)."""
    errors: list[str] = []
    for task in definition.get('tasks', []) or []:
        if not isinstance(task, dict) or str(task.get('node_type') or '') != 'code':
            continue
        label = str(task.get('name') or task.get('id') or '未命名')
        code = str(task.get('code_snippet') or '')
        if not code.strip():
            errors.append(f"代码节点“{label}”必须写出完整 code_snippet，定义 main(显式参数) 并返回对象")
        elif not code_defines_main(code):
            errors.append(f"代码节点“{label}”必须定义顶层函数 main(显式参数)，平台按 input_bindings 调用它")
    return errors


def deterministic_binding_errors(definition: dict[str, Any], resources: dict | None = None) -> list[str]:
    """Check that code/tool/router nodes receive every input they need.

    These nodes do not read prompt placeholders: a code node gets exactly its
    ``input_bindings`` as ``main()`` arguments, a tool node gets them as tool
    arguments, and a router reads the node/input named in each condition.  The
    same rules are enforced when the draft is saved; running them here lets the
    Composer repair a missing binding instead of failing at save time.
    """
    tasks = [item for item in definition.get('tasks', []) or [] if isinstance(item, dict)]
    task_ids = {str(item.get('id')) for item in tasks if item.get('id')}
    task_by_id = {str(item.get('id')): item for item in tasks if item.get('id')}
    input_names = {name for name in _input_names(definition) if name} | RESERVED_BINDING_INPUTS
    schemas = _tool_input_schemas(resources)
    errors: list[str] = []
    fix = '请绑定 source=input（运行输入）或 source=node（上游节点的 text/object/output 字段）'

    def check_binding(label: str, name: str, binding: Any) -> None:
        if not isinstance(binding, dict):
            errors.append(f"节点“{label}”的输入 {name} 格式无效，{fix}")
            return
        source = binding.get('source')
        if source == 'node':
            node_id = str(binding.get('node_id') or '')
            if node_id not in task_ids:
                errors.append(f"节点“{label}”的输入 {name} 引用了不存在的上游节点：{node_id or '空'}")
                return
            upstream = task_by_id[node_id]
            upstream_type = str(upstream.get('node_type') or 'task')
            allowed = [field['name'] for field in fixed_output_contract(upstream_type)]
            root = str(binding.get('variable') or '').split('.', 1)[0]
            if allowed and root not in allowed:
                errors.append(
                    f"节点“{label}”的输入 {name} 引用了上游“{upstream.get('name') or node_id}”不存在的输出"
                    f" {root or '空'}；该节点只能输出 {', '.join(allowed)}")
        elif source == 'input':
            variable = str(binding.get('variable') or '')
            if variable not in input_names:
                errors.append(f"节点“{label}”的输入 {name} 引用了未声明的运行输入：{variable or '空'}")
        elif source != 'literal':
            errors.append(f"节点“{label}”的输入 {name} 缺少有效来源，{fix}")

    for task in tasks:
        node_type = str(task.get('node_type') or 'task')
        label = str(task.get('name') or task.get('id') or '未命名')
        bindings = task.get('input_bindings') or {}
        if node_type in {'code', 'tool'}:
            if not isinstance(bindings, dict):
                errors.append(f"节点“{label}”的 input_bindings 必须是对象")
                continue
            for name, binding in bindings.items():
                check_binding(label, str(name), binding)
        if node_type == 'code' and str(task.get('execution_contract') or 'legacy') == 'object':
            declared, required, accepts_kwargs = code_signature(str(task.get('code_snippet') or ''))
            unbound = sorted(code_unbound_names(str(task.get('code_snippet') or '')))
            if unbound:
                errors.append(
                    f"代码节点“{label}”的 main 使用了未定义的变量 {', '.join(unbound)}：必须把它们声明为 main 参数，"
                    f"并在 input_bindings 中逐个绑定来源（上游数据用 source=node、node_id=直接上游节点 ID、"
                    f"variable=text/object/output 字段）"
                )
            if declared and not accepts_kwargs:
                missing = sorted(required - set(bindings))
                if missing:
                    errors.append(f"代码节点“{label}”的 main 参数 {', '.join(missing)} 没有在 input_bindings 中绑定来源；{fix}")
                unknown = sorted(set(bindings) - declared)
                if unknown:
                    errors.append(f"代码节点“{label}”的 input_bindings 包含 main 签名中不存在的参数：{', '.join(unknown)}")
        if node_type == 'tool':
            schema = schemas.get(str(task.get('tool_id') or ''))
            if not task.get('tool_id'):
                errors.append(f"工具节点“{label}”必须选择工具")
            elif schema:
                properties = schema.get('properties') if isinstance(schema.get('properties'), dict) else {}
                missing = sorted(set(schema.get('required') or []) - set(bindings))
                if missing:
                    errors.append(f"工具节点“{label}”的工具必填参数 {', '.join(missing)} 没有在 input_bindings 中绑定来源；{fix}")
                unknown = sorted(set(bindings) - set(properties)) if properties else []
                if unknown:
                    errors.append(f"工具节点“{label}”的 input_bindings 包含工具未声明的参数：{', '.join(unknown)}")
        if node_type == 'router':
            def visit(expression: Any) -> None:
                if not isinstance(expression, dict):
                    return
                if expression.get('type') == 'group':
                    for child in expression.get('conditions') or []:
                        visit(child)
                    return
                if not str(expression.get('variable') or '').strip():
                    errors.append(f"路由节点“{label}”的条件必须选择变量")
                    return
                check_binding(label, '条件', expression)
            for rule in task.get('router_rules') or []:
                visit(rule.get('expression') if isinstance(rule, dict) else None)
    return list(dict.fromkeys(errors))


def variable_contract_errors(definition: dict[str, Any]) -> list[str]:
    """Validate that runtime data enters executable prompts only through {variables}."""
    # Backup original depends_on before normalization adds placeholder-derived edges
    tasks = definition.get('tasks', []) or []
    original_depends = {
        str(task.get('id') or ''): list(task.get('depends_on', []) or [])
        for task in tasks if isinstance(task, dict)
    }
    normalize_deterministic_dependencies(definition)
    # Restore original edges so visibility check sees only explicit dependencies
    for task in tasks:
        if isinstance(task, dict):
            task_id = str(task.get('id') or '')
            if task_id in original_depends:
                task['depends_on'] = original_depends[task_id]
    names = _input_names(definition)
    errors: list[str] = []
    if any(not name for name in names):
        errors.append('运行输入变量名不能为空')
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append(f"运行输入变量名重复：{', '.join(duplicates)}")
    invalid = sorted({name for name in names if name and not VARIABLE_NAME.fullmatch(name)})
    if invalid:
        errors.append(f"运行输入变量必须使用英文 snake_case：{', '.join(invalid)}")

    tasks = [item for item in definition.get('tasks', []) or [] if isinstance(item, dict)]
    task_ids = [str(item.get('id') or '').strip() for item in tasks]
    duplicate_tasks = sorted({task_id for task_id in task_ids if task_id and task_ids.count(task_id) > 1})
    if duplicate_tasks:
        errors.append(f"任务 ID 重复：{', '.join(duplicate_tasks)}")
    task_by_id = {task_id: item for task_id, item in zip(task_ids, tasks) if task_id}
    output_names_by_task: dict[str, set[str]] = {}
    for task_id, task in task_by_id.items():
        fields = [item for item in task.get('output_variables', []) or [] if isinstance(item, dict)]
        names_for_task = [str(item.get('name') or '').strip() for item in fields]
        output_names_by_task[task_id] = {name for name in names_for_task if name}
        duplicate_outputs = sorted({name for name in names_for_task if name and names_for_task.count(name) > 1})
        if duplicate_outputs:
            errors.append(f"任务“{task.get('name') or task_id}”输出变量重复：{', '.join(duplicate_outputs)}")
        invalid_outputs = sorted({name for name in names_for_task if name and not VARIABLE_NAME.fullmatch(name)})
        if invalid_outputs:
            errors.append(f"任务“{task.get('name') or task_id}”输出变量必须使用英文 snake_case：{', '.join(invalid_outputs)}")

    for task_id, task in task_by_id.items():
        for dependency in task.get('depends_on', []) or []:
            dependency_id = str(dependency).strip()
            if dependency_id not in task_by_id:
                errors.append(f"任务“{task.get('name') or task_id}”依赖了不存在的任务：{dependency_id}")
        mappings = task.get('dependency_variables') or {}
        if not isinstance(mappings, dict):
            errors.append(f"任务“{task.get('name') or task_id}”的 dependency_variables 必须是对象")
            _validate_nested_task_contracts(task, errors)
            continue
        dependencies = {str(value).strip() for value in task.get('depends_on', []) or []}
        for dependency, raw_mappings in mappings.items():
            dependency_id = str(dependency).strip()
            if dependency_id not in task_by_id:
                errors.append(f"任务“{task.get('name') or task_id}”映射了不存在的上游任务：{dependency_id}")
                continue
            if dependency_id not in dependencies and dependency_id not in upstream_node_ids(task_id, tasks):
                errors.append(f"任务“{task.get('name') or task_id}”映射的上游“{dependency_id}”不在上游路径中")
            mapping_items = _mapping_items(raw_mappings)
            upstream_outputs = output_names_by_task.get(dependency_id, set())
            upstream_types = {
                str(item.get('name') or '').strip(): str(item.get('value_type') or 'string')
                for item in (task_by_id.get(dependency_id, {}).get('output_variables') or [])
                if isinstance(item, dict) and item.get('name')
            }
            for mapping in mapping_items:
                if not isinstance(mapping, dict):
                    errors.append(f"任务“{task.get('name') or task_id}”存在无效的变量映射")
                    continue
                source = str(mapping.get('source_variable') or 'result').strip()
                target = str(mapping.get('target_variable') or '').strip()
                if source not in {'result', '$raw'} and source not in upstream_outputs:
                    errors.append(
                        f"任务“{task.get('name') or task_id}”引用上游“{dependency_id}”不存在的输出变量：{source}"
                    )
                if not target or not VARIABLE_NAME.fullmatch(target):
                    errors.append(
                        f"任务“{task.get('name') or task_id}”的变量映射目标必须使用英文 snake_case：{target or '空'}"
                    )
                elif (
                    str(task.get('node_type') or 'task') in TEXT_OUTPUT_NODE_TYPES
                    and upstream_types.get(source) != 'file'
                    and not any(
                        f'{{{target}}}' in str(task.get(field) or '')
                        for field in ('description', 'objective', 'expected_output')
                    )
                ):
                    errors.append(
                        f"任务“{task.get('name') or task_id}”的映射目标 {{{target}}} 未被 description 或 expected_output 使用"
                    )

        _validate_nested_task_contracts(task, errors)

    referenced: set[str] = set()
    configured = {name for name in names if name}
    own_ids = {
        str(task.get('name') or task.get('id') or '未命名任务'): str(task.get('id') or '')
        for task in tasks
    }
    for task_label, field, text, visible in _prompt_fields(definition):
        placeholders = set(PLACEHOLDER.findall(text))
        referenced.update(placeholders & configured)
        own_id = own_ids.get(task_label, '')
        self_refs = sorted(name for name in placeholders if own_id and name.split('.', 1)[0] == own_id)
        if self_refs:
            # A node cannot read the output it is about to produce.  Name the
            # mistake explicitly so the repair Agent (and the user) knows to
            # use a run input or an upstream node instead.
            errors.append(
                f"任务“{task_label}”的 {field} 引用了自身的输出："
                + ', '.join(f'{{{name}}}' for name in self_refs)
                + '；节点只能引用运行输入或上游节点的输出，请改为对应的运行输入变量或上游节点字段'
            )
            placeholders -= set(self_refs)
        unknown = sorted(
            name for name in placeholders
            if name not in visible and not _visible_object_path(name, visible)
        )
        if unknown:
            errors.append(
                f"任务“{task_label}”的 {field} 引用了当前节点不可用的变量："
                + ', '.join(f'{{{name}}}' for name in unknown)
            )
        # A legacy code node is rendered before execution, so placeholders in
        # ``code_snippet`` still need the same visibility check.  Its ordinary
        # Python identifiers are implementation details, though: a signature
        # such as ``def main(message, enabled)`` must not be mistaken for a
        # prompt that forgot to write ``{message}`` and ``{enabled}``.
        if field == 'code_snippet':
            continue
        if field == 'expected_output':
            from .capability_policy import expects_object
            if expects_object(text):
                # In a JSON-object contract a bare name is a field name
                # (e.g. doc_type), not a forgotten placeholder.  Requiring
                # {doc_type} here would contradict the rule that JSON field
                # names are fixed names, never placeholders.
                continue
        without_placeholders = PLACEHOLDER.sub('', text)
        for name in configured:
            bare = re.compile(rf'(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])')
            if bare.search(without_placeholders):
                errors.append(f"任务“{task_label}”直接写了变量 {name}，必须改为 {{{name}}}")

    # Deterministic Flow nodes consume runtime inputs through their explicit
    # binding contract instead of an LLM prompt placeholder. Count those
    # bindings as real consumers so a code/tool-only Flow can be validated
    # without inventing a description or expected_output field.
    for task in tasks:
        if str(task.get('node_type') or '') not in {'code', 'tool'}:
            continue
        for binding in (task.get('input_bindings') or {}).values():
            if not isinstance(binding, dict) or binding.get('source') != 'input':
                continue
            variable = str(binding.get('variable') or '').strip()
            if variable in configured:
                referenced.add(variable)
    # Legacy drafts may still name the single file input reference_file or
    # contract_files. They are migrated to the reserved files input at save
    # time, so count that reserved input as consumed during the transition.
    if 'files' in configured and any(
        str(item.get('input_type') or '') in {'file', 'image'}
        and str(item.get('name') or '') != 'files'
        for item in definition.get('inputs', []) or []
    ):
        referenced.add('files')
    for task in tasks:
        if str(task.get('node_type') or '') != 'router':
            continue
        def collect_inputs(expression: Any) -> None:
            if not isinstance(expression, dict):
                return
            if expression.get('type') == 'group':
                for child in expression.get('conditions') or []:
                    collect_inputs(child)
            elif expression.get('source') == 'input':
                variable = str(expression.get('variable') or '').strip()
                if variable in configured:
                    referenced.add(variable)
        for rule in task.get('router_rules') or []:
            collect_inputs(rule.get('expression') if isinstance(rule, dict) else None)
    unused = sorted(configured - referenced)
    if unused:
        errors.append('运行输入未被任何可执行节点引用：' + ', '.join(f'{{{name}}}' for name in unused))
    try:
        execution_order(definition)
    except ValueError:
        errors.append('节点之间存在循环依赖；Flow 按拓扑顺序执行一次，不支持回边循环，'
                      '需要“修改后重做”时请使用人工审批节点的修改反馈')
    for task in tasks:
        condition = str(task.get('condition') or '').strip()
        if task.get('node_type') == 'router' and condition.startswith('regex:'):
            problem = unsafe_route_regex(condition[len('regex:'):])
            if problem:
                errors.append(f"路由节点“{task.get('name') or task.get('id')}”：{problem}")
    return list(dict.fromkeys(errors))


def ensure_variable_contract(definition: dict[str, Any]) -> None:
    errors = variable_contract_errors(definition)
    if errors:
        raise ValueError('；'.join(errors))


_CHANGE_NOTE = re.compile(
    r'^\s*(仅|本次|本轮|此次|已将|已按|已修|修正|修复|将\S{0,12}改为|改为|调整了)'
    r'|不改动|不改变其[它他]|其[它他]设计不变|保留[^。]{0,30}不改'
    r'|\{[A-Za-z_][\w.]*\}')
_PLACEHOLDER_FIELD = re.compile(r'\{([A-Za-z_]\w*)\}\s*[（(：:]')
_PLACEHOLDER_FIELD_LIST = re.compile(
    r'(字段(?:包括|包含|为|固定为|有)?\s*[:：]?\s*)([^。；;\n]+)'
)
_LIST_FIELD_NAME = re.compile(r'\{([A-Za-z_]\w*)\}(?=\s*(?:[、，,]|$))')


def fix_json_field_placeholders(text: Any) -> str:
    """Turn ``{doc_type}（文种）`` into ``doc_type（文种）`` in a JSON-object contract.

    Models keep writing a JSON field that mirrors a run input as a
    placeholder.  At run time it would be replaced by the user's value and the
    field name would change, so the name must be literal.  The rewrite is
    mechanical and safe, so it is done here instead of by a repair round.
    """
    from .capability_policy import expects_object
    value = str(text or '')
    if not expects_object(value):
        return value
    value = _PLACEHOLDER_FIELD.sub(lambda m: m.group(0)[1:len(m.group(1)) + 1] + m.group(0)[len(m.group(1)) + 2:], value)
    return _PLACEHOLDER_FIELD_LIST.sub(
        lambda m: m[1] + _LIST_FIELD_NAME.sub(lambda field: field[1], m[2]), value,
    )


def summary_looks_like_change_note(text: Any) -> bool:
    """An app summary written as "what this fix changed" instead of an intro."""
    value = str(text or '').strip()
    return bool(value) and bool(_CHANGE_NOTE.search(value))


def design_quality_errors(definition: dict[str, Any]) -> list[str]:
    """Hard checks the review Agent kept missing; fed into every repair round."""
    from .capability_policy import expects_object
    errors: list[str] = []
    if summary_looks_like_change_note(definition.get('summary')):
        errors.append('应用简介 summary 写成了本轮修改说明；summary 必须描述应用目标、交付物和主要处理方式，'
                      '修改说明只写在 reply 中')
    tasks = [item for item in definition.get('tasks', []) or [] if isinstance(item, dict)]

    def check_fields(label: str, item: dict) -> None:
        expected = str(item.get('expected_output') or '')
        names = sorted(set(_PLACEHOLDER_FIELD.findall(expected)))
        if names and expects_object(expected):
            errors.append(f"节点“{label}”的输出要求把占位符 {', '.join('{'+n+'}' for n in names)} 当成 JSON 字段名；"
                          f"字段名必须是固定英文名（如 {names[0]}），占位符只能出现在取值说明里")

    for task in tasks:
        label = str(task.get('name') or task.get('id') or '未命名')
        check_fields(label, task)
        nested = [item for item in task.get('crew_tasks', []) or [] if isinstance(item, dict)]
        for item in nested:
            check_fields(f"{label} / {item.get('name') or item.get('id')}", item)
        if str(task.get('node_type') or '') != 'crew' or not task.get('depends_on') or not nested:
            continue
        upstream = set(upstream_node_ids(str(task.get('id') or ''), tasks)) | {
            str(item) for item in task.get('depends_on') or []}
        text = ' '.join(str(item.get(key) or '') for item in nested for key in ('description', 'expected_output'))
        if not any(re.search(r'\{' + re.escape(node_id) + r'\.', text) for node_id in upstream if node_id):
            errors.append(
                f"Crew 节点“{label}”依赖上游 {', '.join(sorted(upstream))}，但内部任务没有引用任何上游输出，"
                "内部 Agent 拿不到上游数据；请在第一个内部任务 description 中写入可达引用（如 "
                f"{{{sorted(upstream)[0]}.object}} 或 {{{sorted(upstream)[0]}.text}}），修正不可达变量时必须替换而不是删除")
    return list(dict.fromkeys(errors))


def architecture_contract_errors(definition: dict[str, Any]) -> list[str]:
    """Variable contract for the architecture card.

    Code/tool nodes have no code or input_bindings yet: generation writes
    them.  A run input consumed only by such a node therefore looks unused
    here; that check is left to the generation-stage gate.
    """
    errors = variable_contract_errors(definition)
    deferred = any(
        isinstance(task, dict) and str(task.get('node_type') or '') in {'code', 'tool'}
        for task in definition.get('tasks', []) or []
    )
    if deferred:
        errors = [item for item in errors if not item.startswith('运行输入未被任何可执行节点引用')]
    return errors


def executable_contract_errors(definition: dict[str, Any], resources: dict | None = None) -> list[str]:
    """Variable contract plus code/tool/router bindings.

    Draft saving uses only ``variable_contract_errors`` so a draft with a
    broken binding can still be saved and repaired through the chat.  Publish
    and run use this stricter gate: a node that cannot receive its inputs must
    never execute.
    """
    return list(dict.fromkeys([
        *variable_contract_errors(definition),
        *deterministic_binding_errors(definition, resources),
    ]))


def ensure_executable_contract(definition: dict[str, Any], resources: dict | None = None) -> None:
    errors = executable_contract_errors(definition, resources)
    if errors:
        raise ValueError('；'.join(errors))
