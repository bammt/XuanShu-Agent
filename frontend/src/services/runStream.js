import { stripLocalArtifactReferences } from './messageFormatting'

function ensureStep(answer, frame) {
  answer.steps ||= []
  let step = answer.steps.find(item => item.step_id === frame.step_id)
  if (!step) {
    step = {
      step_id: frame.step_id || `step-${answer.steps.length}`,
      step_index: frame.step_index ?? answer.steps.length,
      step_name: frame.step_name || '执行步骤',
      agent_role: frame.agent_role || 'CrewAI Agent',
      node_type: frame.node_type || 'agent',
      status: 'pending',
      preview: '',
      output: '',
      expanded: false,
      tool_name: '',
    }
    answer.steps.push(step)
    answer.steps.sort((left, right) => left.step_index - right.step_index)
  }
  return step
}

export function applyRunFrame(answer, frame) {
  if (Number.isFinite(frame.event_cursor)) answer.eventCursor = Math.max(answer.eventCursor || 0, frame.event_cursor)
  if (frame.status) {
    answer.status = frame.status
    if (frame.status === 'failed') answer.streaming = false
  }
  if (frame.run_id) answer.runId = frame.run_id
  if (frame.type === 'run.retrying') {
    answer.runtimeNotice = frame.detail || `网络波动，正在进行第 ${frame.attempt || 1} 次重试...`
  } else if (frame.type === 'plan') {
    answer.steps = (frame.steps || []).map(step => ({
      ...step, status: 'pending', preview: '', output: '', expanded: false, tool_name: '',
    }))
  } else if (frame.type === 'step.started') {
    const step = ensureStep(answer, frame)
    step.status = 'running'
    if (frame.agent_role) step.agent_role = frame.agent_role
  } else if (frame.type === 'agent.started') {
    const step = ensureStep(answer, frame)
    step.status = 'running'
    step.agent_role = frame.agent_role || step.agent_role
  } else if (frame.type === 'tool.started') {
    const step = ensureStep(answer, frame)
    step.status = 'running'; step.tool_name = frame.tool_name || 'Tool'
  } else if (frame.type === 'step.completed') {
    const step = ensureStep(answer, frame)
    step.status = 'completed'; step.tool_name = ''
  } else if (frame.type === 'step.skipped') {
    ensureStep(answer, frame).status = 'skipped'
  } else if (frame.type === 'step.failed') {
    ensureStep(answer, frame).status = 'failed'
  } else if (frame.type === 'delta' && frame.text) {
    const step = ensureStep(answer, frame)
    step.status = 'running'
    if (frame.agent_role) step.agent_role = frame.agent_role
    if (frame.scope === 'answer')
      answer.text = stripLocalArtifactReferences(
        `${answer.text || ''}${frame.text}`,
        { trim: false },
      )
    else {
      step.output = stripLocalArtifactReferences(
        frame.replace ? frame.text : `${step.output || ''}${frame.text}`,
        { trim: false },
      )
      step.preview = step.output.slice(-220)
    }
  } else if (frame.type === 'done') {
    answer.runtimeNotice = ''
    answer.text = stripLocalArtifactReferences(frame.output || answer.text) || '执行已完成。'
    answer.files = frame.files || []
    answer.routerOnly = frame.metrics?.runtime_type === 'conversation_router'
    // A local submit error can be rendered before the terminal event arrives.
    // The server's completed event is authoritative, so do not leave that
    // stale error attached to a successfully completed run.
    answer.role = 'assistant'
    answer.error = ''
    answer.status = 'completed'; answer.streaming = false
    return 'completed'
  } else if (frame.type === 'waiting_for_feedback') {
    answer.text = answer.text || '流程已暂停，正在等待人工审批。'
    answer.status = 'waiting_for_feedback'; answer.streaming = false
    return 'waiting_for_feedback'
  } else if (frame.type === 'run.waiting_input' || frame.type === 'waiting_for_input') {
    answer.text = stripLocalArtifactReferences(frame.question || frame.output || answer.text) || '请补充必要信息。'
    answer.waitingInput = frame.waiting_input || { question: answer.text }
    answer.status = 'waiting_input'; answer.streaming = false
    return 'waiting_input'
  } else if (frame.type === 'error') {
    const partial = stripLocalArtifactReferences(frame.output || answer.text || '')
    answer.role = partial ? 'assistant' : 'error'
    answer.text = partial
    answer.error = frame.message || '执行失败'
    answer.status = 'failed'; answer.streaming = false
    return 'failed'
  }
  return ''
}
