"""Shared user-facing input policy for Composer and Studio."""


def conversational_inputs(inputs: list[dict]) -> list[dict]:
    """Keep business fields; collect typed values as text for Agent conversion."""
    result = []
    message = None
    for original in inputs:
        item = dict(original)
        kind = item.get('type', 'text')
        keep_text_field = True
        if kind in {'number', 'boolean', 'json'}:
            item['type'] = 'long_text' if kind == 'json' else 'text'
            hint = f'用户以自然语言文本提供，由 Agent 理解、校验并转换为 {kind}。'
            description = item.get('description') or ''
            item['description'] = description if hint in description else description + ('；' if description else '') + hint
        elif kind == 'image':
            # ask_user has one attachment interaction: file. Images remain a
            # valid single-run input, but in a conversational flow they use
            # the ordinary file picker so the interaction contract stays
            # limited to text and files.
            item['type'] = 'file'
        if item.get('type') in {'file', 'image'}:
            item['multiple'] = True
        else:
            item['multiple'] = False
        if item.get('variable') == 'message':
            if message is None:
                message = item
        elif keep_text_field:
            result.append(item)
    message = message or {'name': '用户需求', 'variable': 'message', 'description': '用户本轮对智能体的需求描述，通过 {message} 传入执行流程。'}
    message.update(type='long_text', required=True, multiple=False)
    return [message, *result]
