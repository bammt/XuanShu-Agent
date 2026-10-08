"""Stable public names for workflow nodes and variable references.

Database keys remain ordinary identifiers, while this module owns the names
shown to users and used in placeholders.  The counters are persisted in the
workflow document so deleting a node never causes a later node to reuse its
number.
"""

from __future__ import annotations

import re
from typing import Any


_CANONICAL_ID = re.compile(r'^(?P<prefix>task|agent|agent_def|crew|node)_(?P<number>[1-9][0-9]*)$')


def _next_counter(counters: dict[str, Any], prefix: str, used: set[str], key: str | None = None) -> int:
    key = key or prefix
    current = int(counters.get(key) or 0)
    while f'{prefix}_{current + 1}' in used:
        current += 1
    counters[key] = current + 1
    return current + 1


def _replace_reference(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in sorted(aliases.items(), key=lambda item: -len(item[0])):
            value = value.replace('{' + old + '}', '{' + new + '}')
            value = value.replace('{' + old + '.', '{' + new + '.')
        return value
    if isinstance(value, list):
        return [_replace_reference(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: _replace_reference(item, aliases) for key, item in value.items()}
    return value


def _rename_values(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, str):
        return aliases.get(value, value)
    if isinstance(value, list):
        return [_rename_values(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: _rename_values(item, aliases) for key, item in value.items()}
    return value


def _canonical_ids(items: list[dict], prefix_for, counters: dict[str, Any], key: str | None = None) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    used: set[str] = set()
    # Preserve already-canonical names. This makes a normal save idempotent and
    # lets the persisted counters control names after a deletion.
    for item in items:
        old = str(item.get('id') or '').strip()
        match = _CANONICAL_ID.fullmatch(old)
        if match and match.group('prefix') == prefix_for(item) and old not in used:
            used.add(old)
            counter_key = key or match.group('prefix')
            counters[counter_key] = max(
                int(counters.get(counter_key) or 0), int(match.group('number')),
            )
    for item in items:
        old = str(item.get('id') or '').strip()
        if old in used:
            continue
        prefix = prefix_for(item)
        number = _next_counter(counters, prefix, used, key)
        new = f'{prefix}_{number}'
        used.add(new)
        if old and old != new:
            aliases[old] = new
        item['id'] = new
    return aliases, used


def normalize_definition_names(definition: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize IDs and every reference in one workflow document in place."""
    if not isinstance(definition, dict):
        return definition or {}
    kind = str(definition.get('kind') or 'crew')
    if kind not in {'crew', 'flow'}:
        kind = 'crew'
    manual_mode = str(definition.get('node_name_mode') or '') == 'manual'
    counters = dict(definition.get('node_name_counters') or {}) if manual_mode else {}
    tasks = [item for item in definition.get('tasks', []) or [] if isinstance(item, dict)]
    agents = [item for item in definition.get('agents', []) or [] if isinstance(item, dict)]
    if not tasks and not agents and 'node_name_counters' not in definition:
        return definition

    # Agent definitions have their own namespace. Task/node IDs are the public
    # variable namespace and are handled separately below.
    agent_aliases, _ = _canonical_ids(
        agents, lambda _item: 'agent' if kind == 'crew' else 'agent_def', counters,
    )
    task_aliases, _ = _canonical_ids(
        tasks,
        (lambda _item: 'task') if kind == 'crew' else
        (lambda item: 'agent' if str(item.get('node_type') or 'agent') == 'agent'
         else 'crew' if str(item.get('node_type') or '') == 'crew' else 'node'),
        counters,
    )

    nested_aliases: dict[str, str] = {}
    nested_aliases_by_parent: dict[str, dict[str, str]] = {}
    for parent in tasks:
        if str(parent.get('node_type') or '') != 'crew':
            continue
        nested = [item for item in parent.get('crew_tasks', []) or [] if isinstance(item, dict)]
        nested_map, _ = _canonical_ids(nested, lambda _item: 'task', counters, f"{parent.get('id')}.task")
        nested_aliases.update(nested_map)
        nested_aliases_by_parent[str(parent.get('id') or '')] = nested_map

    # Update ownership and graph references before rewriting prompt strings.
    for task in tasks:
        if task.get('agent_id') in agent_aliases:
            task['agent_id'] = agent_aliases[task['agent_id']]
        if isinstance(task.get('crew_agent_ids'), list):
            task['crew_agent_ids'] = [
                agent_aliases.get(str(value), value) for value in task['crew_agent_ids']
            ]
        task['depends_on'] = [task_aliases.get(str(value), value) for value in task.get('depends_on', []) or []]
        mappings = task.get('dependency_variables')
        if isinstance(mappings, dict):
            task['dependency_variables'] = {
                task_aliases.get(str(key), str(key)): value
                for key, value in mappings.items()
            }
        bindings = task.get('input_bindings') or {}
        if isinstance(bindings, dict):
            for binding in bindings.values():
                if isinstance(binding, dict) and binding.get('node_id') in task_aliases:
                    binding['node_id'] = task_aliases[binding['node_id']]
        for rule in task.get('router_rules', []) or []:
            expression = rule.get('expression') if isinstance(rule, dict) else None
            if isinstance(expression, dict) and expression.get('node_id') in task_aliases:
                expression['node_id'] = task_aliases[expression['node_id']]
        for nested in task.get('crew_tasks', []) or []:
            if not isinstance(nested, dict):
                continue
            if nested.get('agent_id') in agent_aliases:
                nested['agent_id'] = agent_aliases[nested['agent_id']]
            nested['depends_on'] = [nested_aliases.get(str(value), value) for value in nested.get('depends_on', []) or []]
            nested_ids = {str(item.get('id') or '') for item in task.get('crew_tasks', []) if isinstance(item, dict)}
            # A Crew internal task may depend only on another internal task.
            # A Flow node such as agent_10 is an external context source and
            # belongs on the parent Crew edge, never in this local graph.
            nested['depends_on'] = [value for value in nested.get('depends_on', []) if str(value) in nested_ids]
            nested_maps = nested.get('dependency_variables')
            if isinstance(nested_maps, dict):
                nested['dependency_variables'] = {
                    nested_aliases.get(str(key), str(key)): value
                    for key, value in nested_maps.items()
                    if nested_aliases.get(str(key), str(key)) in nested_ids
                }

    for key in ('manager_agent_id', 'manager_model_profile_id'):
        if definition.get(key) in agent_aliases:
            definition[key] = agent_aliases[definition[key]]
    for task in tasks:
        for key in ('crew_manager_agent_id', 'crew_planning_model_profile_id'):
            if task.get(key) in agent_aliases:
                task[key] = agent_aliases[task[key]]
    interaction = definition.get('interaction')
    if isinstance(interaction, dict):
        for key in ('collection_task_id',):
            if interaction.get(key) in task_aliases:
                interaction[key] = task_aliases[interaction[key]]
        if isinstance(interaction.get('interactive_task_ids'), list):
            interaction['interactive_task_ids'] = [
                task_aliases.get(str(value), str(value))
                for value in interaction['interactive_task_ids']
            ]

    aliases = {**task_aliases}
    # Nested references use the public Flow form ``crew1.task1.field``.
    for parent in tasks:
        if str(parent.get('node_type') or '') != 'crew':
            continue
        parent_id = str(parent.get('id') or '')
        public_parent = parent_id.replace('_', '')
        old_parent = next((old for old, new in task_aliases.items() if new == parent_id), parent_id)
        for old, new in nested_aliases_by_parent.get(parent_id, {}).items():
            aliases[old] = f'{public_parent}.{new.replace("_", "")}'
            aliases[f'{old_parent}.{old}'] = f'{public_parent}.{new.replace("_", "")}'
            aliases[f'{old_parent}.{new}'] = f'{public_parent}.{new.replace("_", "")}'
            aliases[f'{public_parent}.{old.replace("_", "")}'] = f'{public_parent}.{new.replace("_", "")}'
    for key in ('description', 'objective', 'expected_output', 'code_snippet'):
        for task in tasks:
            if isinstance(task.get(key), str):
                task[key] = _replace_reference(task[key], aliases)
            for nested in task.get('crew_tasks', []) or []:
                if isinstance(nested, dict) and isinstance(nested.get(key), str):
                    nested[key] = _replace_reference(nested[key], aliases)

    definition['node_name_counters'] = counters
    definition['node_name_mode'] = 'manual' if manual_mode else 'automatic'
    return definition


def canonical_public_reference(node_id: str, field: str) -> str:
    return f'{node_id}.{field}'
