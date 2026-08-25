const BEIJING_TIME_ZONE = 'Asia/Shanghai'

function asUtcDate(value) {
  if (!value) return null
  const text = String(value)
  // Database timestamps are stored as UTC without an offset.  Treat those
  // values as UTC before converting them, otherwise browsers in China show a
  // misleading server-local time.
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text}Z`
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? null : date
}

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIME_ZONE,
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  hour12: false,
})

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIME_ZONE,
  year: 'numeric', month: '2-digit', day: '2-digit',
})

const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIME_ZONE,
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  hour12: false,
})

export function formatBeijingDateTime(value) {
  const date = asUtcDate(value)
  return date ? dateTimeFormatter.format(date) : '--'
}

export function formatBeijingDate(value) {
  const date = asUtcDate(value)
  return date ? dateFormatter.format(date) : '--'
}

export function formatBeijingTime(value) {
  const date = asUtcDate(value)
  return date ? timeFormatter.format(date) : '--'
}

export function timestampValue(value) {
  const date = asUtcDate(value)
  return date ? date.getTime() : 0
}
