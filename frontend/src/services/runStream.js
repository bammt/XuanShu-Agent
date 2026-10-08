import { displayNodeOutput, displayOutput } from './outputDisplay.js'
import { normalizeEscapedControlCharacters, stripLocalArtifactReferences } from './messageFormatting.js'

import { normalizeRunFrame, CLOSED_EXECUTION_STATUSES } from './runProtocol.js'

function clean(value, options) {
  return stripLocalArtifactReferences(normalizeEscapedControlCharacters(value || ''), options)
}

function frameKey(frame) {
  if (frame.display_id) return frame.display_id
  const base = frame.node_id || frame.step_id || frame.task_id || frame.node_name || frame.step_name || frame.task_name || ''
  // Runtime scopes attach the current node to every CrewAI callback. Keep
  // callbacks for that node in one turn; nested Crew tasks get their own
  // node_id and therefore still render as separate agent turns.
  // Tool/LLM callbacks belonging to a normal top-level task must stay in
  // that task's card. Nested Crew agents have their own crew_task node_id and
  // therefore still get independent cards.
  return String(base || frame.agent_id || frame.agent_role || '')
}

function ensureStep(answer, frame) {
  answer.steps ||= []
  const stepId = frame.node_id || frame.step_id || frame.task_id
  let step = answer.steps.find((item) => item.step_id === stepId)
  if (!step) {
    step = {
      step_id: stepId || `step-${answer.steps.length}`,
      step_index: frame.step_index ?? answer.steps.length,
      step_name: frame.step_name || frame.node_name || '执行步骤',
      agent_role: frame.agent_role || '执行智能体',
      node_type: frame.node_type || 'agent',
      status: 'pending',
      preview: '',
      output: '',
      expanded: false,
      tool_name: '',
      activity: '',
    }
    answer.steps.push(step)
    answer.steps.sort((left, right) => left.step_index - right.step_index)
  }
  return step
}

function isSdkId(value) { return /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(value || '') }

function ensureTurn(answer, frame = {}) {
  answer.turns ||= []
  const current = answer.turns.find((item) => item.status === 'running' && item.run_attempt === (frame.run_attempt || 0))
  const key = frameKey(frame) || current?.id || `turn-${answer.turns.length}`
  let turn = answer.turns.find((item) => item.id === key && item.run_attempt === (frame.run_attempt || 0))
  if (!turn) {
    const node = frame.node_id || frame.step_id || frame.task_id || ''
    const peers = answer.turns.filter(t => t.node_id === node && t.run_attempt === (frame.run_attempt || 0))
    // Node lifecycle and its first agent share the placeholder. Additional
    // agents in a shared node remain separate; role bridges SDK/local IDs.
    turn = peers.find(t => frame.agent_id && t.agent_id === frame.agent_id)
      || peers.find(t => frame.agent_role && t.agent_role === frame.agent_role &&
        (isSdkId(t.agent_id) !== isSdkId(frame.agent_id)))
      || peers.find(t => !t.agent_id && !t.agent_roleExplicit)
    if (!turn && !frame.display_id && peers.length === 1) turn = peers[0]
  }
  if (!turn) {
    turn = {
      id: key,
      node_id: frame.node_id || frame.step_id || frame.task_id || '',
      agent_id: frame.agent_id || '',
      run_attempt: frame.run_attempt || 0,
      step_name: frame.step_name || frame.node_name || frame.task_name || '执行步骤',
      agent_role: frame.agent_role || '执行智能体',
      node_type: frame.node_type || 'agent',
      status: 'pending',
      output: '',
      preview: '',
      expanded: true,
      activity: '',
      tools: [],
    }
    answer.turns.push(turn)
  }
  if (frame.step_name || frame.node_name || frame.task_name)
    turn.step_name = frame.step_name || frame.node_name || frame.task_name
  if (frame.agent_role) { turn.agent_role = frame.agent_role; turn.agent_roleExplicit = true }
  if (frame.agent_id) turn.agent_id = frame.agent_id
  if (frame.node_type) turn.node_type = frame.node_type
  if (frame.output_mode) turn.outputMode = frame.output_mode
  if (frame.node_id) turn.node_id = frame.node_id
  return turn
}

function setTurnOutput(turn, value, replace = false) {
  if (value === undefined || value === null) return
  turn.output = clean(replace ? value : `${turn.output || ''}${value}`, { trim: false })
  turn.preview = turn.output.slice(-220)
}

function ensureTool(turn, frame) {
  turn.tools ||= []
  const name = frame.tool_name || frame.tool || '工具'
  const explicitId = frame.tool_call_id || frame.call_id
  const id = String(explicitId || `${name}:${turn.tools.length}`)
  let tool = turn.tools.find((item) => explicitId && item.id === String(explicitId))
  // Some MCP adapters emit a generated id on `tool.started` and a different
  // id on `tool.completed`. If there is only one active call with that name,
  // merge the completion into it instead of rendering a second card. Several
  // concurrent calls with the same name remain distinct.
  if (!tool && !explicitId) {
    const active = turn.tools.filter((item) => item.name === name && item.status === 'running')
    if (active.length === 1) tool = active[0]
  }
  if (!tool) {
    tool = { id, name, status: 'running', detail: '', output: '' }
    turn.tools.push(tool)
  }
  tool.name = name
  return tool
}

function finishTurn(turn, frame, status = 'completed') {
  if (frame.output !== undefined)
    setTurnOutput(turn, displayNodeOutput(frame.output, frame.node_type || turn.node_type, frame.output_mode), true)
  turn.status = status
  for (const tool of turn.tools || []) {
    if (tool.status === 'running') {
      tool.status = 'ended'
      tool.detail = '调用已结束（未收到独立完成事件）'
    }
  }
  turn.activity = frame.detail || (status === 'failed' ? '执行失败' : `已完成：${turn.step_name}`)
  // Completed turns are compact summaries. The active turn stays open until
  // the run's terminal event decides which one is the final answer.
  if (status !== 'running') turn.expanded = false
}

function settleActiveWork(answer, status = 'completed', exceptNodeId = '') {
  for (const turn of answer.turns || []) {
    if (exceptNodeId && String(turn.node_id || '') === String(exceptNodeId)) continue
    if (turn.status === 'running') turn.status = status
    for (const tool of turn.tools || []) {
      if (tool.status === 'running') {
        tool.status = 'ended'
        tool.detail = '调用已结束（未收到独立完成事件）'
      }
    }
  }
  for (const step of answer.steps || []) {
    if (exceptNodeId && String(step.step_id || '') === String(exceptNodeId)) continue
    if (step.status === 'running') step.status = status
  }
}

function finishRun(answer, output, files) {
  settleActiveWork(answer, 'completed')
  const completed = (answer.turns || []).filter((turn) => turn.status !== 'failed')
  const finalTurn = completed[completed.length - 1]
  if (finalTurn) {
    answer.finalTurnId = finalTurn.id
    answer.turns.forEach((turn) => {
      // The final answer is shown in the transcript below the node list.
      // Keep its node card available for inspection, but collapse it to avoid
      // rendering the same long answer twice by default.
      turn.expanded = false
      turn.isFinal = turn.id === finalTurn.id
    })
  }
  const fallback = finalTurn?.output || answer.text || '执行已完成。'
  answer.text = clean(output || fallback) || '执行已完成。'
  answer.files = files || answer.files || []
  answer.runtimeNotice = ''
  answer.activity = ''
  answer.role = 'assistant'
  answer.error = ''
  answer.status = 'completed'
  answer.streaming = false
}

export { createRunFrameBatcher } from './runFrameBuffer.js'

function resetAttempt(answer, attempt) {
  answer.runAttempt = attempt
  answer.turns = []
  answer.steps = (answer.steps || []).map(step => ({ ...step, status: 'pending', output: '', preview: '', activity: '' }))
  answer.files = []
  answer.text = ''
  answer.error = ''
  answer.waitingInput = null
  answer.pendingFeedback = null
  answer.finalTurnId = null
  answer.status = 'running'
  answer.streaming = true
  answer._toolEventKeys = new Set()
}

export function applyRunFrame(answer, raw) {
  const frame = normalizeRunFrame(raw)
  if (!frame) return ''
  // CrewAI adapters can replay the same lifecycle callback while a stream is
  // being resumed.  The SDK event id identifies one call; suppress only the
  // same event/type pair so two real calls to the same MCP tool still render.
  if (frame.type === 'tool.started' || frame.type === 'tool.completed' || frame.type === 'tool.failed') {
    const callId = frame.tool_call_id || frame.call_id
    const eventId = frame.sdk_event_id || frame.event_id
    const identity = callId || eventId
    if (identity) {
      answer._toolEventKeys ||= new Set()
      const key = `${frame.type}:${frame.node_id || ''}:${identity}`
      if (answer._toolEventKeys.has(key)) return ''
      answer._toolEventKeys.add(key)
    }
  }
  const attempt = frame.run_attempt ?? answer.runAttempt ?? 0
  if (attempt < (answer.runAttempt || 0)) return ''
  if (attempt > (answer.runAttempt || 0)) resetAttempt(answer, attempt)
  frame.run_attempt = attempt
  if (frame.display_only) {
    const turn = ensureTurn(answer, frame)
    setTurnOutput(turn, frame.text)
    return ''
  }
  if (Number.isFinite(frame.event_cursor))
    answer.eventCursor = Math.max(answer.eventCursor || 0, frame.event_cursor)
  if (frame.run_id) answer.runId = frame.run_id

  return RUN_FRAME_HANDLERS[frame.type]?.(answer, frame) || ''
}

const RUN_FRAME_HANDLERS = Object.freeze({
  'run.started': (answer) => { answer.status = 'running'; answer.streaming = true },
  'run.manual_retry': () => {},
  'llm.received': (answer, frame) => {
    ensureTurn(answer, frame)
    // Reception advances the cursor only; lifecycle events own status.
  },
  'run.retrying': (answer, frame) => {
    answer.runtimeNotice = frame.detail || `网络波动，正在进行第 ${frame.attempt || 1} 次重试...`
    answer.retryHistory ||= []
    answer.retryHistory.push({ attempt: frame.attempt || 1, message: answer.runtimeNotice })
    answer.activity = answer.runtimeNotice
    answer.retryAttempt = frame.attempt || answer.retryAttempt || 1
    answer.status = 'queued'
    answer.streaming = true
    answer.error = ''
  },
  'agent.retrying': (answer, frame) => {
    answer.runtimeNotice = frame.detail || '模型调用失败，正在重试当前任务…'
    answer.retryHistory ||= []
    answer.retryHistory.push({
      attempt: frame.attempt || answer.retryHistory.length + 1,
      message: answer.runtimeNotice,
      internal: true,
    })
    answer.activity = answer.runtimeNotice
    answer.status = 'running'
    answer.streaming = true
  },
  'plan': (answer, frame) => {
    answer.steps = (frame.steps || []).map((step) => ({
      ...step,
      status: 'pending',
      preview: '',
      output: '',
      expanded: false,
      tool_name: '',
      activity: '',
    }))
    answer.activity = `已规划 ${answer.steps.length} 个执行步骤`
  },
  'node.started': (answer, frame) => {
    const step = ensureStep(answer, frame)
    const turn = ensureTurn(answer, frame)
    step.status = 'running'
    step.activity = `正在准备：${frame.step_name || frame.node_name || '执行步骤'}`
    turn.status = 'running'
    turn.activity = step.activity
    turn.expanded = true
    answer.activity = step.activity
  },
  'agent.started': (answer, frame) => {
    const step = ensureStep(answer, frame)
    const turn = ensureTurn(answer, frame)
    step.status = 'running'
    step.agent_role = frame.agent_role || step.agent_role
    step.activity = frame.detail || `正在理解：${step.step_name}`
    turn.status = 'running'
    turn.activity = step.activity
    turn.expanded = true
    answer.activity = step.activity
  },
  'agent.completed': (answer, frame) => {
    const step = ensureStep(answer, frame)
    const turn = ensureTurn(answer, frame)
    step.activity = frame.detail || '已完成思考'
    finishTurn(turn, frame, 'completed')
    answer.activity = step.activity
  },
  'agent.failed': (answer, frame) => {
    const step = ensureStep(answer, frame)
    const turn = ensureTurn(answer, frame)
    step.activity = frame.detail || '智能体执行失败'
    finishTurn(turn, frame, 'failed')
    answer.activity = step.activity
  },
  'tool.failed': (answer, frame) => {
    const step = ensureStep(answer, frame)
    const turn = ensureTurn(answer, frame)
    const tool = ensureTool(turn, frame)
    tool.status = 'failed'
    tool.detail = frame.detail || `工具执行失败：${tool.name}`
    step.tool_name = tool.name
    step.activity = tool.detail
    // A tool error can be handled by the Agent; its lifecycle remains active.
    turn.activity = tool.detail
    answer.activity = step.activity
  },
  'tool.started': (answer, frame) => {
    const step = ensureStep(answer, frame)
    const turn = ensureTurn(answer, frame)
    const tool = ensureTool(turn, frame)
    if (tool.status !== 'running') return ''
    if (CLOSED_EXECUTION_STATUSES.has(turn.status)) {
      tool.status = 'ended'; tool.detail = '调用已结束'; return ''
    }
    tool.status = 'running'
    tool.detail = frame.detail || `正在调用 ${tool.name}…`
    step.status = 'running'
    step.tool_name = tool.name
    step.activity = tool.detail
    turn.status = 'running'
    turn.activity = tool.detail
    turn.expanded = true
    answer.activity = step.activity
  },
  'tool.completed': (answer, frame) => {
    const step = ensureStep(answer, frame)
    const turn = ensureTurn(answer, frame)
    const tool = ensureTool(turn, frame)
    tool.status = 'completed'
    tool.detail = frame.detail || `已完成 ${tool.name}`
    tool.output = frame.output || frame.result || ''
    step.tool_name = tool.name
    step.activity = tool.detail
    turn.activity = tool.detail
    answer.activity = step.activity
  },
  'node.completed': (answer, frame) => {
    const step = ensureStep(answer, frame)
    const turn = ensureTurn(answer, frame)
    step.status = 'completed'
    step.tool_name = ''
    step.activity = `已完成：${frame.step_name || frame.node_name || step.step_name}`
    finishTurn(turn, frame, 'completed')
    step.output = turn.output
    step.preview = turn.preview
    for (const child of answer.turns || []) {
      if (child !== turn && child.node_id === turn.node_id && child.run_attempt === turn.run_attempt && child.status === 'running')
        finishTurn(child, {}, 'completed')
    }
    answer.activity = step.activity
  },
  'node.skipped': (answer, frame) => {
    const turn = ensureTurn(answer, frame)
    ensureStep(answer, frame).status = 'skipped'
    finishTurn(turn, frame, 'skipped')
  },
  'node.failed': (answer, frame) => {
    const turn = ensureTurn(answer, frame)
    ensureStep(answer, frame).status = 'failed'
    finishTurn(turn, frame, 'failed')
  },
  'llm.thinking': (answer, frame) => {
    const turn = ensureTurn(answer, frame)
    if (CLOSED_EXECUTION_STATUSES.has(turn.status)) return
    const step = ensureStep(answer, frame)
    turn.activity = step.activity = frame.detail || '正在整理思路…'
    turn.expanded = true
    answer.activity = turn.activity
  },
  'llm.delta': (answer, frame) => {
    if (!frame.text) return
    const turn = ensureTurn(answer, frame)
    setTurnOutput(turn, frame.text)
    if (CLOSED_EXECUTION_STATUSES.has(turn.status)) return
    const step = ensureStep(answer, frame)
    turn.activity = step.activity = frame.detail || '正在生成回复…'
    turn.expanded = true
    answer.activity = turn.activity
  },
  'delta': (answer, frame) => {
    if (!frame.text) return
    if (frame.scope === 'answer') {
      answer.text = clean(`${answer.text || ''}${frame.text}`, { trim: false })
      return
    }
    const turn = ensureTurn(answer, frame)
    const step = ensureStep(answer, frame)
    setTurnOutput(turn, frame.text, Boolean(frame.replace))
    step.output = turn.output
    step.preview = turn.preview
    if (CLOSED_EXECUTION_STATUSES.has(turn.status)) return
    turn.activity = step.activity = '正在整理结果…'
    turn.expanded = true
    answer.activity = turn.activity
  },
  'run.completed': (answer, frame) => {
    answer.routerOnly = frame.metrics?.runtime_type === 'conversation_router'
    answer.files = frame.files || []
    finishRun(answer, displayOutput(frame.output), frame.files)
    return 'completed'
  },
  'run.failed': (answer, frame) => {
    settleActiveWork(answer, 'failed')
    answer.text = clean(displayOutput(frame.output) || answer.text || '')
    answer.role = answer.text ? 'assistant' : 'error'
    answer.error = frame.error || '执行失败'
    answer.activity = ''
    answer.status = 'failed'
    answer.streaming = false
    return 'failed'
  },
  'approval.required': (answer, frame) => {
    answer.pendingFeedback = frame.pending_feedback
    answer.text = answer.text || '流程已暂停，正在等待人工审批。'
    answer.activity = ''
    answer.status = 'waiting_approval'
    answer.streaming = false
    return 'waiting_approval'
  },
  'node.waiting_input': (answer, frame) => {
    const turn = ensureTurn(answer, frame)
    const step = ensureStep(answer, frame)
    for (const peer of answer.turns || []) {
      if (peer.node_id === turn.node_id && peer.run_attempt === turn.run_attempt &&
          ['pending', 'running', 'waiting_input'].includes(peer.status)) finishTurn(peer, {detail:'等待补充信息'}, 'waiting_input')
    }
    turn.status = step.status = 'waiting_input'
    turn.activity = step.activity = '等待补充信息'
  },
  'run.waiting_input': (answer, frame) => {
    answer.text = clean(frame.question || frame.output || answer.text) || '请补充必要信息。'
    answer.activity = ''
    answer.waitingInput = frame.waiting_input || { question: answer.text }
    answer.status = 'waiting_input'
    answer.streaming = false
    return 'waiting_input'
  },
})
