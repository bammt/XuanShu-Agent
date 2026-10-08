import { OutputTextStream } from './outputDisplay.js'
import { normalizeRunFrame } from './runProtocol.js'

export function createRunFrameBatcher(onFrame, delay = 12) {
  const buffers = new Map()
  const projections = new Map()
  const modes = new Map()
  let timer = null
  let resolvers = []
  const complete = () => {
    if (buffers.size) return
    const done = resolvers; resolvers = []; done.forEach(resolve => resolve())
  }
  const deliver = (frame) => onFrame(frame)
  const pump = () => {
    timer = null
    // Each execution gets one character per tick, irrespective of how much
    // another agent has buffered. Lifecycle events use the immediate path.
    for (const [key, queue] of buffers) {
      const frame = queue[0]
      const char = String.fromCodePoint(frame.text.codePointAt(0))
      deliver({ ...frame, text: char, display_only: true })
      frame.text = frame.text.slice(char.length)
      if (!frame.text) queue.shift()
      if (!queue.length) buffers.delete(key)
    }
    if (buffers.size) timer = setTimeout(pump, delay)
    else complete()
  }
  const nodeKey = frame => String(frame.node_id || frame.step_id || frame.task_id || '')
  const discard = predicate => {
    for (const [key, queue] of buffers) if (predicate(queue[0])) buffers.delete(key)
    complete()
  }
  return {
    push(raw) {
      let frame = normalizeRunFrame(raw)
      if (!frame) return
      if (frame.type === 'run.manual_retry') { discard(() => true); projections.clear(); modes.clear() }
      if (frame.output_mode) modes.set(nodeKey(frame), frame.output_mode)
      // An authoritative replacement supersedes unplayed draft characters.
      if ((frame.type === 'delta' && frame.replace) ||
          (frame.type === 'node.completed' && frame.output !== undefined)) {
        discard(item => nodeKey(item) === nodeKey(frame) &&
          (item.run_attempt || 0) === (frame.run_attempt || 0))
      }
      if (frame.type !== 'llm.delta' || !frame.text) {
        deliver(frame)
        return
      }
      // Record reception/state immediately; animation may arrive after a
      // completion and must never mutate execution state or stream cursors.
      deliver({ ...frame, type: 'llm.received', text: undefined })
      const key = frame.display_id || JSON.stringify([frame.run_attempt || 0, nodeKey(frame), frame.agent_id || ''])
      const streamKey = JSON.stringify([key, frame.llm_call_id || ''])
      if (!projections.has(streamKey)) projections.set(streamKey, new OutputTextStream(frame.output_mode || modes.get(nodeKey(frame)) || 'text'))
      const projected = projections.get(streamKey).push(frame.text)
      if (!projected) return
      frame = { ...frame, text: projected }
      // Background tabs throttle timers aggressively. Apply received text
      // immediately there so the stream keeps advancing while the page is
      // minimized; the character animation is only a foreground affordance.
      if (typeof document !== 'undefined' && document.hidden) {
        deliver({ ...frame, display_only: true })
        return
      }
      if (!buffers.has(key)) buffers.set(key, [])
      buffers.get(key).push(frame)
      if (timer === null) timer = setTimeout(pump, delay)
    },
    flush() {
      if (timer !== null) clearTimeout(timer)
      timer = null
      for (const queue of buffers.values()) for (const frame of queue)
        deliver({ ...frame, display_only: true })
      buffers.clear(); complete()
    },
    finish() {
      if (!buffers.size) return Promise.resolve()
      return new Promise(resolve => resolvers.push(resolve))
    },
  }
}
