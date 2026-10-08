"""Explicit input binding and structured output for deterministic Flow nodes."""
import json
from typing import Any

RESULT_MARKER = '__XUANSHU_NODE_RESULT__='


def bind_node_inputs(bindings: dict, inputs: dict, outputs: dict) -> dict:
    result = {}
    for name, binding in bindings.items():
        source = binding.get('source')
        if source == 'literal':
            result[name] = binding.get('value')
        elif source == 'input':
            variable = binding.get('variable')
            if variable not in inputs: raise ValueError(f'节点输入 {name} 引用缺失变量：{variable}')
            result[name] = inputs[variable]
        elif source == 'node':
            node = binding.get('node_id')
            if node not in outputs: raise ValueError(f'节点输入 {name} 引用未执行节点：{node}')
            value = outputs[node]
            path = binding.get('variable', 'output')
            if path == '$raw':
                result[name] = value
                continue
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError as exc:
                    if path in {'text', 'result'}:
                        result[name] = value
                        continue
                    raise ValueError(f'节点 {node} 没有结构化输出') from exc
            for part in path.split('.'):
                if not isinstance(value, dict) or part not in value:
                    raise ValueError(f'节点 {node} 缺少输出字段：{path}')
                value = value[part]
            result[name] = value
        else:
            raise ValueError(f'节点输入 {name} 的来源无效：{source}')
    return result


def evaluate_condition(expression: dict, inputs: dict, outputs: dict) -> bool:
    """Evaluate a nested AND/OR expression using explicitly bound values."""
    if not isinstance(expression, dict):
        raise ValueError('路由条件必须是对象')
    if expression.get('type') == 'group':
        operator = expression.get('operator', 'and')
        children = expression.get('conditions') or []
        if operator not in {'and', 'or'} or not children:
            raise ValueError('条件组必须选择 AND/OR 并至少包含一个条件')
        values = (evaluate_condition(child, inputs, outputs) for child in children)
        return all(values) if operator == 'and' else any(values)
    if expression.get('type') != 'condition':
        raise ValueError('条件类型无效')

    binding = {key: expression.get(key) for key in ('source', 'node_id', 'variable', 'value')}
    actual = bind_node_inputs({'value': binding}, inputs, outputs)['value']
    operator = str(expression.get('operator') or 'equals')
    expected = expression.get('value')
    if operator == 'is_empty':
        return actual is None or actual == '' or actual == [] or actual == {}
    if operator == 'is_not_empty':
        return not (actual is None or actual == '' or actual == [] or actual == {})
    if operator in {'equals', 'not_equals'}:
        left, right = actual, expected
        value_type = str(expression.get('value_type') or '')
        if value_type == 'number':
            try:
                left, right = float(actual), float(expected)
            except (TypeError, ValueError):
                return False if operator == 'equals' else True
        elif value_type == 'boolean':
            left = actual if isinstance(actual, bool) else str(actual).lower() == 'true'
            right = expected if isinstance(expected, bool) else str(expected).lower() == 'true'
        elif value_type in {'object', 'array'} and isinstance(expected, str):
            try:
                right = json.loads(expected)
            except ValueError:
                pass
        result = left == right
        return result if operator == 'equals' else not result
    left = str(actual if actual is not None else '')
    right = str(expected if expected is not None else '')
    if operator == 'contains': return right.casefold() in left.casefold()
    if operator == 'not_contains': return right.casefold() not in left.casefold()
    if operator == 'starts_with': return left.casefold().startswith(right.casefold())
    if operator == 'ends_with': return left.casefold().endswith(right.casefold())
    if operator in {'greater_than', 'less_than'}:
        try:
            left_number, right_number = float(actual), float(expected)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{operator} 只支持数字变量') from exc
        return left_number > right_number if operator == 'greater_than' else left_number < right_number
    raise ValueError(f'不支持的条件运算符：{operator}')


def evaluate_router(rules: list[dict], inputs: dict, outputs: dict) -> str:
    """Return the first matching CASE id, or ``else``."""
    for rule in rules or []:
        if evaluate_condition(rule.get('expression') or {}, inputs, outputs):
            return str(rule['id'])
    return 'else'


def code_program(source: str, arguments: dict) -> str:
    # Inputs are JSON data, never substituted into executable source text.
    encoded = repr(json.dumps(arguments, ensure_ascii=False))
    return f'''import json as _node_json
{source}
_node_result = main(**_node_json.loads({encoded}))
if not isinstance(_node_result, dict):
    raise TypeError("代码节点 main 必须返回对象")
print({RESULT_MARKER!r} + _node_json.dumps({{"output": _node_result}}, ensure_ascii=False, allow_nan=False))
'''


def parse_code_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            result = json.loads(line[len(RESULT_MARKER):])
            if isinstance(result, dict) and isinstance(result.get('output'), dict):
                return result
    raise ValueError('代码节点没有返回有效的 output 对象')
