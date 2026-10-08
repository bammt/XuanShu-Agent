<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { basicSetup } from 'codemirror'
import { EditorState, Prec } from '@codemirror/state'
import { EditorView, keymap, placeholder as placeholderExtension } from '@codemirror/view'
import { indentWithTab } from '@codemirror/commands'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { python } from '@codemirror/lang-python'
import { markdown } from '@codemirror/lang-markdown'
import { json } from '@codemirror/lang-json'
import { tags } from '@lezer/highlight'

const props = defineProps({
  modelValue: { type: String, default: '' },
  language: { type: String, default: 'text' },
  readonly: { type: Boolean, default: false },
  placeholder: { type: String, default: '' },
  minHeight: { type: String, default: '220px' },
})
const emit = defineEmits(['update:modelValue', 'cursor'])
const host = ref(null)
let view = null
let writing = false

const languageExtension = () => ({
  python,
  py: python,
  markdown,
  md: markdown,
  json,
}[String(props.language || '').toLowerCase()]?.() || [])

const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.controlKeyword, tags.operatorKeyword], color: '#79a8ff' },
  { tag: [tags.variableName, tags.propertyName, tags.attributeName], color: '#d7e5ff' },
  { tag: [tags.function(tags.variableName), tags.definition(tags.variableName)], color: '#c7a0ff' },
  { tag: [tags.string, tags.special(tags.string)], color: '#a8d59c' },
  { tag: [tags.number, tags.bool, tags.null], color: '#efb782' },
  { tag: [tags.comment, tags.docComment], color: '#839188', fontStyle: 'italic' },
  { tag: [tags.typeName, tags.className], color: '#80c9c3' },
  { tag: [tags.punctuation, tags.bracket], color: '#aeb9b1' },
])

function extensions() {
  return [
    basicSetup,
    keymap.of([indentWithTab]),
    languageExtension(),
    syntaxHighlighting(highlight),
    EditorView.lineWrapping,
    props.placeholder ? placeholderExtension(props.placeholder) : [],
    EditorState.readOnly.of(props.readonly),
    EditorView.editable.of(!props.readonly),
    EditorView.contentAttributes.of({
      'aria-label': '代码编辑器',
      'data-placeholder': props.placeholder,
    }),
    EditorView.updateListener.of((update) => {
      const head = update.state.selection.main.head
      const line = update.state.doc.lineAt(head)
      emit('cursor', { line: line.number, column: head - line.from + 1 })
      if (!update.docChanged || writing) return
      emit('update:modelValue', update.state.doc.toString())
    }),
    EditorView.theme({
      '&': { minHeight: props.minHeight, height: '100%', background: '#1e231f', color: '#dce5de' },
      '.cm-scroller': { overflow: 'auto', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '13px', lineHeight: '1.65' },
      '.cm-content': { minHeight: props.minHeight, padding: '12px 0', caretColor: '#dce5de' },
      '.cm-line': { padding: '0 14px 0 8px' },
      '.cm-gutters': { background: '#1a1f1b', color: '#667269', borderRight: '1px solid #303832' },
      '.cm-activeLine, .cm-activeLineGutter': { background: '#273029' },
      // Keep selected code visibly separate from the dark editor surface and
      // the green syntax colors used by strings and comments.
      '.cm-selectionLayer': { zIndex: '0' },
      '.cm-selectionBackground, .cm-selectionLayer .cm-selectionBackground, &.cm-focused .cm-selectionBackground, &.cm-focused .cm-selectionLayer .cm-selectionBackground': { background: '#2563eb !important', backgroundColor: '#2563eb !important', opacity: '1 !important' },
      '.cm-content::selection, .cm-line::selection': { background: 'transparent !important', color: '#ffffff' },
      '.cm-cursor': { borderLeftColor: '#dce5de' },
      '.cm-placeholder': { color: '#748078' },
      '&.cm-focused': { outline: 'none' },
    }, { dark: true }),
    // CodeMirror's internal selection layer sits above syntax text in some
    // browsers. Hide it and let the native selection paint the characters so
    // selected text remains readable instead of becoming a solid blue block.
    Prec.highest(EditorView.theme({
      '.cm-selectionLayer': { display: 'none !important' },
      '.cm-content::selection, .cm-content ::selection, .cm-line::selection, .cm-line ::selection': {
        backgroundColor: '#2563eb !important',
        color: '#ffffff !important',
      },
    }, { dark: true })),
  ]
}

function createView() {
  if (!host.value) return
  view = new EditorView({
    parent: host.value,
    state: EditorState.create({ doc: props.modelValue || '', extensions: extensions() }),
  })
}

onMounted(createView)
onBeforeUnmount(() => view?.destroy())

watch(() => props.modelValue, (value) => {
  if (!view || view.state.doc.toString() === String(value || '')) return
  writing = true
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: String(value || '') } })
  writing = false
})

watch(() => [props.language, props.readonly], () => {
  const value = view?.state.doc.toString() ?? props.modelValue
  view?.destroy()
  view = null
  if (host.value) {
    writing = true
    createView()
    if (value !== props.modelValue) emit('update:modelValue', value)
    writing = false
  }
})
</script>

<template>
  <div ref="host" class="code-editor" :class="{ readonly }"></div>
</template>
