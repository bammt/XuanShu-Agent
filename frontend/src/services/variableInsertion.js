export function variableQuery(value, cursor) {
  const before = String(value || '').slice(0, cursor)
  const match = before.match(/(?:^|[\s（(：:，,。；;])\/([^\s/{}]*)$/u)
  return match ? { start: cursor - match[1].length - 1, query: match[1] } : null
}

export function insertVariable(value, start, end, name) {
  const token = `{${name}}`
  return { value: value.slice(0, start) + token + value.slice(end), cursor: start + token.length }
}
