const ignoredFields = new Set(['id', 'updated_at', 'draft_revision', 'draft_sync', 'status', 'published'])

const same = (left, right) => JSON.stringify(left) === JSON.stringify(right)

export function mergeDraft(base, local, remote) {
  const merged = { ...remote }
  const conflicts = []
  for (const field of new Set([...Object.keys(base || {}), ...Object.keys(local || {})])) {
    if (ignoredFields.has(field)) continue
    const localChanged = !same(local?.[field], base?.[field])
    if (!localChanged) continue
    const remoteChanged = !same(remote?.[field], base?.[field])
    if (remoteChanged && !same(local?.[field], remote?.[field])) {
      conflicts.push(field)
      continue
    }
    merged[field] = local[field]
  }
  return { merged, conflicts }
}
