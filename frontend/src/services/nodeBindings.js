export function isFileVariable(item) {
  return ['file', 'image'].includes(String(item?.value_type || item?.input_type || ''))
}

export function selectableNodeVariables(items, nodeType) {
  return items
}

export function addCodeDependencyBinding(bindings, sourceId, outputFields) {
  const sourceField = (outputFields || []).find((field) => !isFileVariable(field)) || (outputFields || [])[0]
  if (!sourceField) return { ...(bindings || {}) }
  const next = { ...(bindings || {}) }
  const emptyName = Object.keys(next).find((name) => {
    const binding = next[name]
    return !binding?.variable && binding?.source !== 'literal'
  })
  let parameter = emptyName || String(sourceField.name || 'value')
    .replace(/[^A-Za-z0-9_]/g, '_')
    .replace(/^[^A-Za-z_]+/, '') || 'value'
  if (!emptyName) {
    const base = parameter
    let suffix = 2
    while (parameter in next) parameter = `${base}_${suffix++}`
  }
  next[parameter] = {
    source: 'node', node_id: sourceId,
    variable: sourceField.name,
    value_type: sourceField.value_type || 'object', value: '',
  }
  return next
}

export function removeDependencyBindings(bindings, sourceId) {
  return Object.fromEntries(
    Object.entries(bindings || {}).filter(
      ([, binding]) => !(binding?.source === 'node' && binding.node_id === sourceId),
    ),
  )
}

export function migrateDeterministicBindings(task, tasks) {
  const bindings = { ...(task.input_bindings || {}) }
  for (const [dependencyId, mappings] of Object.entries(task.dependency_variables || {})) {
    const upstream = (tasks || []).find((candidate) => candidate.id === dependencyId)
    for (const mapping of mappings || []) {
      const field = (upstream?.output_variables || []).find(
        (candidate) => candidate.name === mapping.source_variable,
      )
      const oldName = mapping.source_variable || '$raw'
      const outputName = oldName === '$raw' ? oldName
        : ['object', 'file', 'text', 'route'].includes(oldName) ? oldName
        : oldName === 'generated_files' || field?.value_type === 'file' ? 'file'
        : upstream?.node_type === 'router' ? 'route'
        : upstream?.node_type === 'code' ? (field?.value_type === 'string' ? 'text' : 'output')
        : field?.value_type === 'string' ? 'text' : 'object'
      if (mapping.target_variable && !bindings[mapping.target_variable]) {
        bindings[mapping.target_variable] = {
          source: 'node', node_id: dependencyId,
          variable: outputName,
          value_type: field?.value_type || 'object', value: '',
        }
      }
    }
  }
  return bindings
}
