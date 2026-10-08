// Legacy wire names are accepted only here. State reducers use canonical names.
const EVENT_ALIASES = Object.freeze({
  'step.started': 'node.started',
  'step.completed': 'node.completed',
  'step.skipped': 'node.skipped',
  'step.failed': 'node.failed',
  done: 'run.completed',
  error: 'run.failed',
  waiting_for_feedback: 'approval.required',
  waiting_for_input: 'run.waiting_input',
})

export function normalizeRunFrame(raw) {
  if (!raw) return null
  const frame = { ...raw, type: EVENT_ALIASES[raw.type] || raw.type }
  frame.node_id = raw.node_id || raw.step_id || raw.task_id
  frame.node_name = raw.node_name || raw.step_name
  if (frame.type === 'run.failed') frame.error = raw.error || raw.message || raw.detail || '执行失败'
  if (frame.type === 'approval.required') {
    frame.pending_feedback = raw.pending_feedback || {
      step_id: frame.node_id, step_name: frame.node_name,
      message: raw.message, output: raw.output,
      outcomes: raw.outcomes || ['approved', 'revise'], default_outcome: raw.default_outcome,
    }
  }
  if (!frame.display_id && /^(agent|tool|llm)\./.test(frame.type)) {
    const agent = frame.agent_id || frame.agent_role
    if (agent) frame.display_id = frame.agent_execution_id || JSON.stringify([
      frame.run_attempt || 0, String(frame.node_id || ''), agent,
    ])
  }
  if (raw.status === 'waiting_for_feedback') frame.status = 'waiting_approval'
  return frame
}

export const CLOSED_EXECUTION_STATUSES = new Set([
  'completed', 'failed', 'skipped', 'waiting_input', 'waiting_approval',
])
