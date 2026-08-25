import hashlib
import json
import re
from typing import Any


VARIABLE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
PLACEHOLDER = re.compile(r'(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})')
BUILTIN_VARIABLES = {'message', 'files', 'conversation_history'}


def ensure_file_output_contract(definition: dict[str, Any]) -> dict[str, Any]:
    """Make generated file artifacts explicit and pass them through the graph.

    The executor observes files independently from an Agent's text response.
    Keeping a typed file output on the producing node and a mapping on every
    dependent node lets the next Agent receive the actual relative artifact
    names instead of a markdown link or an opaque MinIO key.
    """
    tasks = [item for item in (definition.get('tasks') or []) if isinstance(item, dict)]
    agents = {
        str(item.get('id')): item
        for item in (definition.get('agents') or [])
        if isinstance(item, dict)
    }

    def output_variables(task: dict) -> list[dict]:
        values = task.get('output_variables')
        if not isinstance(values, list):
            values = []
        normalized = [dict(item) for item in values if isinstance(item, dict)]
        if not normalized:
            normalized = [{'name': 'result', 'description': '任务最终输出', 'value_type': 'string'}]
        for item in normalized:
            item.setdefault('name', 'result')
            item.setdefault('description', '')
            item.setdefault('value_type', 'string')
        task['output_variables'] = normalized
        return normalized

    def producer_can_write_files(task: dict) -> bool:
        if str(task.get('node_type') or '') == 'code':
            return True
        assigned = agents.get(str(task.get('agent_id') or ''))
        if assigned and assigned.get('allow_code_execution'):
            return True
        crew_ids = {str(value) for value in task.get('crew_agent_ids') or []}
        return any(agents.get(agent_id, {}).get('allow_code_execution') for agent_id in crew_ids)

    for task in tasks:
        fields = output_variables(task)
        if producer_can_write_files(task) and not any(field.get('value_type') == 'file' for field in fields):
            fields.append({
                'name': 'generated_files',
                'description': '代码执行或文件工具实际生成并由平台登记的文件',
                'value_type': 'file',
            })

    by_id = {str(task.get('id')): task for task in tasks}
    # Propagate all typed file outputs through every direct dependency. A
    # generated graph is normally topologically ordered, but models and older
    # saved definitions are not required to emit that order. Iterate to a
    # fixed point so a file created by A is still carried through B to C when
    # the task list is [C, B, A].
    changed = True
    while changed:
        changed = False
        for task in tasks:
            mappings = task.setdefault('dependency_variables', {})
            if not isinstance(mappings, dict):
                mappings = {}
                task['dependency_variables'] = mappings
            fields = output_variables(task)
            for dependency_id in task.get('depends_on') or []:
                dependency = by_id.get(str(dependency_id))
                if not dependency:
                    continue
                file_fields = [
                    field for field in output_variables(dependency)
                    if field.get('value_type') == 'file'
                ]
                if not file_fields:
                    continue
                dependency_mappings = mappings.setdefault(str(dependency_id), [])
                if not isinstance(dependency_mappings, list):
                    dependency_mappings = []
                    mappings[str(dependency_id)] = dependency_mappings
                for field in file_fields:
                    name = str(field.get('name') or 'generated_files')
                    mapping = {'source_variable': name, 'target_variable': name}
                    if mapping not in dependency_mappings:
                        dependency_mappings.append(mapping)
                        changed = True
                    if not any(
                        output.get('name') == name and output.get('value_type') == 'file'
                        for output in fields
                    ):
                        fields.append({
                            'name': name,
                            'description': '从上游接收并继续传递的实际文件产物',
                            'value_type': 'file',
                        })
                        changed = True
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
        dependencies = {str(item) for item in task.get('depends_on', [])}
        mapped = _mapping_targets(task.get('dependency_variables'))
        visible = configured | BUILTIN_VARIABLES | dependencies | mapped
        label = str(task.get('name') or task.get('id') or '未命名任务')
        yield label, 'description', str(task.get('description') or task.get('objective') or ''), visible
        yield label, 'expected_output', str(task.get('expected_output') or ''), visible
        if task.get('node_type') == 'code':
            yield label, 'code_snippet', str(task.get('code_snippet') or ''), visible
        for nested in task.get('crew_tasks', []):
            nested_label = f"{label} / {nested.get('name') or nested.get('id') or '内部任务'}"
            # CrewAI provides upstream internal Task output through ``context``;
            # an explicit placeholder is valid only when a dependency mapping
            # gives that value a concrete downstream name.
            nested_visible = visible | _mapping_targets(nested.get('dependency_variables'))
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
            if dependency_id not in dependencies:
                errors.append(f"任务“{label}”映射的内部上游“{dependency_id}”不是直接依赖")
            upstream_outputs = output_names_by_task.get(dependency_id, set())
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


def variable_contract_errors(definition: dict[str, Any]) -> list[str]:
    """Validate that runtime data enters executable prompts only through {variables}."""
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
            if dependency_id not in dependencies:
                errors.append(f"任务“{task.get('name') or task_id}”映射的上游“{dependency_id}”不是直接依赖")
            mapping_items = _mapping_items(raw_mappings)
            upstream_outputs = output_names_by_task.get(dependency_id, set())
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

        _validate_nested_task_contracts(task, errors)

    referenced: set[str] = set()
    configured = {name for name in names if name}
    for task_label, field, text, visible in _prompt_fields(definition):
        placeholders = set(PLACEHOLDER.findall(text))
        referenced.update(placeholders & configured)
        unknown = sorted(placeholders - visible)
        if unknown:
            errors.append(
                f"任务“{task_label}”的 {field} 引用了当前节点不可用的变量："
                + ', '.join(f'{{{name}}}' for name in unknown)
            )
        without_placeholders = PLACEHOLDER.sub('', text)
        for name in configured:
            bare = re.compile(rf'(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])')
            if bare.search(without_placeholders):
                errors.append(f"任务“{task_label}”直接写了变量 {name}，必须改为 {{{name}}}")

    unused = sorted(configured - referenced)
    if unused:
        errors.append('运行输入未被任何可执行节点引用：' + ', '.join(f'{{{name}}}' for name in unused))
    return list(dict.fromkeys(errors))


def ensure_variable_contract(definition: dict[str, Any]) -> None:
    errors = variable_contract_errors(definition)
    if errors:
        raise ValueError('；'.join(errors))
