export function groupTraceEvents(events = []) {
  const groups = []
  const byKey = new Map()
  const agentsByNode = new Map()
  for (const event of events) {
    if (!event.node_id || !event.agent_id) continue
    const scope = `${event.run_id || ''}:${event.node_id}`
    if (!agentsByNode.has(scope)) agentsByNode.set(scope, new Set())
    agentsByNode.get(scope).add(String(event.agent_id))
  }
  for (const event of events) {
    const runId = String(event.run_id || '')
    const nodeId = String(event.node_id || '')
    const agentId = String(event.agent_id || '')
    const sharedNode = agentsByNode.get(`${runId}:${nodeId}`)?.size > 1
    const key = nodeId
      ? `${runId}:node:${nodeId}${sharedNode && agentId ? `:agent:${agentId}` : ''}`
      : agentId ? `${runId}:agent:${agentId}` : `${runId}:event:${event.event_index}`
    let group = byKey.get(key)
    if (!group) {
      group = { key, runId, nodeId, agentId, title: sharedNode && agentId ? event.agent_role || agentId : event.node_name || event.agent_role || event.title || nodeId || '运行事件', events: [], status: 'running' }
      byKey.set(key, group)
      groups.push(group)
    }
    group.events.push(event)
    if (event.type === 'node.completed' || event.type === 'run.completed') group.status = 'completed'
    else if (event.type === 'node.failed' || event.type === 'run.failed') group.status = 'failed'
    else if (event.type === 'node.waiting_input' || event.type === 'run.waiting_input') group.status = 'waiting_input'
  }
  return groups
}

export function expandedTraceEvents(group, rawEvents = []) {
  const result = []
  const source = rawEvents.length
    ? rawEvents.filter((event) => {
      const nodeId = String(event.node_id || event.step_id || event.task_id || '')
      const agentId = String(event.agent_id || '')
      if (String(group.nodeId || '') !== nodeId) return false
      return !group.agentId || !agentId || group.agentId === agentId
    })
    : group.events
  for (const summary of source) {
    const raw = summary.event_index !== undefined
      ? (rawEvents[summary.event_index] || summary)
      : summary
    const event = { ...summary, ...raw, at: summary.at || raw.at }
    if (event.type === 'llm.delta' && result.at(-1)?.type === 'llm.delta' &&
        result.at(-1).llm_call_id === event.llm_call_id &&
        result.at(-1).agent_id === event.agent_id) {
      result.at(-1).text += event.text || ''
      continue
    }
    result.push(event)
  }
  return result
}

export function traceEventPayload(event) {
  const payload = {}
  for (const key of ['error', 'question', 'message', 'arguments', 'output', 'text', 'result']) {
    if (event[key] !== undefined && event[key] !== null && event[key] !== '') payload[key] = event[key]
  }
  const keys = Object.keys(payload)
  if (!keys.length) return ''
  if (keys.length === 1 && typeof payload[keys[0]] === 'string') return payload[keys[0]]
  return JSON.stringify(payload, null, 2)
}
