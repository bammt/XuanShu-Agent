"""Transport projection for persisted runtime events; shared by all SSE routes.

A completion is one event, never a synthetic start/delta/completion sequence.
Identity, attempt and diagnostic fields survive projection unchanged.
"""
import json
from typing import Any

STOPPED_RUN_STATUSES = frozenset({
    'completed', 'failed', 'waiting_input', 'waiting_approval',
    'approved', 'rejected', 'needs_revision',
})


def project_run_event(event: dict[str, Any], cursor: int, *, output: str = '') -> dict[str, Any]:
    frame = {**event, 'event_cursor': cursor}
    event_type = frame.get('type')
    if event_type == 'run.retrying':
        frame.setdefault('detail', frame.get('message', ''))
    elif event_type == 'run.waiting_input':
        frame['question'] = frame.get('question') or output
    elif event_type == 'run.failed':
        frame.setdefault('output', output)
        frame.setdefault('error', frame.get('message') or '执行失败')
    return frame


def encode_run_event(frame: dict[str, Any]) -> str:
    return f"event: {frame['type']}\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n"


async def stream_run_frames(load_run, *, after_event=0, initial_frame=None,
                            completed_files=None, completed_document=None, poll_interval=0.4):
    """One cursor/polling implementation for authenticated, public and API runs."""
    import asyncio

    sent = max(0, after_event)
    if sent == 0 and initial_frame is not None:
        yield {**initial_frame, 'event_cursor': 0}
    while True:
        run = await load_run()
        if run is None:
            yield {'type': 'run.failed', 'error': '运行不存在', 'event_cursor': sent}
            return
        events = run.events or []
        for cursor, event in enumerate(events[sent:], start=sent + 1):
            yield project_run_event(event, cursor, output=run.output)
        sent = len(events)
        if run.status == 'completed' and (completed_files is not None or completed_document is not None):
            result = completed_document(run) if completed_document is not None else {
                'output': run.output, 'files': completed_files(run),
            }
            yield {'type': 'run.completed', 'output': run.output,
                   'files': result.get('files', []), 'result': result, 'event_cursor': sent,
                   'run_attempt': (run.approval_payload or {}).get('run_attempt', 0)}
        if run.status in STOPPED_RUN_STATUSES:
            return
        # None is a transport heartbeat, never a runtime event.
        yield None
        await asyncio.sleep(poll_interval)
