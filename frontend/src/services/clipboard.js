function copyWithSelection(text, doc) {
  const previousFocus = doc.activeElement
  const selection = doc.getSelection?.()
  const ranges = []
  if (selection) {
    for (let index = 0; index < selection.rangeCount; index += 1)
      ranges.push(selection.getRangeAt(index).cloneRange())
  }
  const input = doc.createElement('textarea')
  input.value = text
  input.readOnly = true
  input.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;font-size:16px;'
  doc.body.appendChild(input)
  try {
    input.focus({ preventScroll: true })
    input.select()
    input.setSelectionRange(0, text.length)
    if (!doc.execCommand?.('copy')) throw new Error('无法复制，请选中内容手动复制')
  } finally {
    input.remove()
    previousFocus?.focus?.({ preventScroll: true })
    if (selection) {
      selection.removeAllRanges()
      ranges.forEach(range => selection.addRange(range))
    }
  }
}

export async function copyText(text, nav = globalThis.navigator, doc = globalThis.document) {
  const value = String(text ?? '')
  if (nav?.clipboard?.writeText) {
    try {
      await nav.clipboard.writeText(value)
      return
    } catch (_) {
      // HTTP hosts and denied Clipboard API permissions need the selection path.
    }
  }
  copyWithSelection(value, doc)
}
