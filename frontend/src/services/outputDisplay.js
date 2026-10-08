// Project the internal output contract to user-visible text. Business objects
// stay available for bindings but never become a JSON wrapper in the chat.
const MARKER = '<XUANSHU_COLLECTION_COMPLETE>'

function unwrap(value) {
  let text = String(value ?? '').trim()
  if (text.startsWith(MARKER)) text = text.slice(MARKER.length).trimStart()
  const fenced = text.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i)
  if (fenced) text = fenced[1]
  try { return JSON.parse(text) } catch { return null }
}

export function displayOutput(value) {
  const parsed = typeof value === 'object' && value !== null ? value : unwrap(value)
  if (parsed && typeof parsed.text === 'string') {
    const nested = unwrap(parsed.text)
    // Legacy runs accidentally stored a second envelope in text.
    if (nested && ('object' in nested || 'file' in nested) && typeof nested.text === 'string')
      return nested.text
    return parsed.text.replace(/^<XUANSHU_COLLECTION_COMPLETE>\s*/, '')
  }
  return String(value ?? '').replace(/^<XUANSHU_COLLECTION_COMPLETE>\s*/, '')
}

export function displayNodeOutput(value, nodeType = '', outputMode = '') {
  if (outputMode === 'json') {
    const envelope = typeof value === 'object' && value !== null ? value : unwrap(value)
    return envelope && typeof envelope.text === 'string' ? envelope.text : String(value ?? '')
  }
  if (!['code', 'tool'].includes(nodeType)) return displayOutput(value)
  const parsed = typeof value === 'object' && value !== null ? value : unwrap(value)
  if (!parsed || typeof parsed !== 'object') return String(value ?? '')
  const result = nodeType === 'code' ? parsed.output : parsed.object
  if (result && typeof result === 'object' && (nodeType === 'code' || Object.keys(result).length))
    return JSON.stringify(result, null, 2)
  return typeof parsed.text === 'string' ? parsed.text : String(value ?? '')
}

// Read a possibly unfinished JSON string without exposing escapes or half a
// surrogate pair. `end` is only set when its closing quote has arrived.
function readString(source, start) {
  let value = ''
  let i = start + 1
  while (i < source.length) {
    const char = source[i++]
    if (char === '"') return { value, end: i }
    if (char !== '\\') { value += char; continue }
    if (i === source.length) break
    const escape = source[i++]
    if (escape === 'u') {
      if (i + 4 > source.length) break
      const hex = source.slice(i, i + 4)
      if (!/^[\da-f]{4}$/i.test(hex)) break
      value += String.fromCharCode(parseInt(hex, 16)); i += 4
    } else {
      const escapes = { '"': '"', '\\': '\\', '/': '/', n: '\n', r: '\r', t: '\t', b: '\b', f: '\f' }
      if (!(escape in escapes)) break
      value += escapes[escape]
    }
  }
  if (/[\uD800-\uDBFF]$/.test(value)) value = value.slice(0, -1)
  return { value, end: null }
}

function topLevelText(source) {
  let depth = 0
  for (let i = 0; i < source.length;) {
    const c = source[i]
    if (c === '"') {
      const token = readString(source, i)
      if (!token.end) return null
      let next = token.end
      while (/\s/.test(source[next] || '') && next < source.length) next++
      if (depth === 1 && token.value === 'text' && source[next] === ':') {
        next++
        while (/\s/.test(source[next] || '') && next < source.length) next++
        if (source[next] === '"') return readString(source, next).value
      }
      i = token.end
    } else {
      if (c === '{' || c === '[') depth++
      if (c === '}' || c === ']') depth--
      i++
    }
  }
  return null
}

export class OutputTextStream {
  constructor(mode = 'text') { this.mode = mode; this.raw = ''; this.shown = ''; this.plain = false }
  push(chunk) {
    this.raw += chunk
    let candidate = this.raw.trimStart()
    if (MARKER.startsWith(candidate)) return ''
    if (candidate.startsWith(MARKER)) candidate = candidate.slice(MARKER.length).trimStart()
    if (!candidate || '```json'.startsWith(candidate)) return ''
    if (candidate.startsWith('```')) {
      const newline = candidate.indexOf('\n')
      if (newline < 0) return ''
      if (/^```(?:json)?\s*\n/i.test(candidate)) candidate = candidate.slice(newline + 1).trimStart()
      else this.plain = true
    }
    let projected
    if (this.mode === 'json') {
      projected = candidate
    } else if (!this.plain && candidate.startsWith('{')) {
      projected = topLevelText(candidate)
      if (projected === null) {
        if (this.mode === 'json') return ''
        const parsed = unwrap(candidate)
        if (!parsed || 'object' in parsed || 'text' in parsed) return ''
        this.plain = true
      }
    } else this.plain = true
    if (this.plain) projected = this.raw.replace(/^\s*<XUANSHU_COLLECTION_COMPLETE>\s*/, '')
    if (projected == null || MARKER.startsWith(projected)) return ''
    if (projected.startsWith(MARKER)) projected = projected.slice(MARKER.length).trimStart()
    if (/[\uD800-\uDBFF]$/.test(projected)) projected = projected.slice(0, -1)
    const delta = projected.slice(this.shown.length)
    this.shown = projected
    return delta
  }
}
