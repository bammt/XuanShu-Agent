"""Deterministic conversation context budgeting and Redis coordination."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException

from .config import settings
from .services import redis


def estimate_tokens(value: Any) -> int:
    """Portable token estimate used before provider-specific tokenization."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    ascii_words = len(re.findall(r"[A-Za-z0-9_]+", text))
    non_ascii = sum(1 for char in text if ord(char) > 127)
    punctuation = len(re.findall(r"[^\w\s]", text, flags=re.UNICODE))
    return max(1, ascii_words + non_ascii + punctuation // 3)


def _compact_text(value: str, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _trim_to_token_budget(value: str, budget: int, *, keep_tail: bool = False) -> str:
    """Trim deterministically until the portable estimate fits the budget."""
    text = str(value or '').strip()
    if budget <= 0:
        return ''
    while text and estimate_tokens(text) > budget:
        estimate = estimate_tokens(text)
        target = max(1, int(len(text) * budget / estimate * .92))
        if target >= len(text):
            target = len(text) - 1
        text = text[-target:] if keep_tail else text[:target]
    return text


def compact_history_summary(previous: str, removed: list[dict]) -> str:
    lines = [_compact_text(previous, 1200)] if previous else []
    for turn in removed:
        user = _compact_text(turn.get("user", ""), 220)
        assistant = _compact_text(turn.get("assistant", ""), 320)
        if user or assistant:
            lines.append(f"用户：{user}\n助手：{assistant}")
    summary = "\n".join(filter(None, lines))
    return summary[-settings.conversation_summary_max_chars:]


def budget_conversation_history(
    history: list[dict],
    previous_summary: str = "",
    *,
    token_budget: int | None = None,
) -> tuple[list[dict], str, int]:
    budget = max(256, int(token_budget or settings.conversation_history_token_budget))
    kept: list[dict] = []
    removed: list[dict] = []
    # Reserve room for a summary whenever older turns exist. Recent verbatim
    # context may consume at most 70% of the total budget.
    recent_budget = max(128, int(budget * .7))
    used = 0
    for turn in reversed(history):
        cost = estimate_tokens(turn)
        if kept and used + cost > recent_budget:
            removed.append(turn)
            continue
        if not kept and cost > recent_budget:
            user_budget = max(32, int(recent_budget * .4))
            compact = {
                "user": _trim_to_token_budget(turn.get("user", ""), user_budget),
                "assistant": _trim_to_token_budget(
                    turn.get("assistant", ""), max(32, recent_budget - user_budget - 8),
                ),
            }
            while estimate_tokens(compact) > recent_budget and compact['assistant']:
                compact['assistant'] = _trim_to_token_budget(
                    compact['assistant'], max(1, estimate_tokens(compact['assistant']) - 8),
                )
            kept.append(compact)
            used += estimate_tokens(compact)
            continue
        kept.append(turn)
        used += cost
    kept.reverse()
    removed.reverse()
    summary = compact_history_summary(previous_summary, removed)
    kept_cost = sum(estimate_tokens(item) for item in kept)
    while len(kept) > 1 and kept_cost > recent_budget:
        removed_turn = kept.pop(0)
        summary = compact_history_summary(summary, [removed_turn])
        kept_cost = sum(estimate_tokens(item) for item in kept)
    summary = _trim_to_token_budget(summary, max(0, budget - kept_cost), keep_tail=True)
    summary_cost = estimate_tokens(summary) if summary else 0
    return kept, summary, summary_cost + kept_cost


def budget_chat_messages(messages: list[dict], *, token_budget: int | None = None) -> tuple[list[dict], str, int]:
    """Budget role/content messages while keeping the stored transcript untouched."""
    turns: list[dict] = []
    current: dict[str, str] = {'user': '', 'assistant': ''}
    for message in messages:
        role = str(message.get('role') or '')
        content = str(message.get('content') or '')
        if role == 'user':
            if current['user'] or current['assistant']:
                turns.append(current)
            current = {'user': content, 'assistant': ''}
        elif role in {'assistant', 'error'}:
            current['assistant'] = '\n'.join(filter(None, [current['assistant'], content]))
    if current['user'] or current['assistant']:
        turns.append(current)
    kept, summary, tokens = budget_conversation_history(turns, token_budget=token_budget)
    result: list[dict] = []
    if summary:
        result.append({'role': 'system', 'content': f'较早编排对话摘要：\n{summary}'})
    for turn in kept:
        if turn.get('user'):
            result.append({'role': 'user', 'content': turn['user']})
        if turn.get('assistant'):
            result.append({'role': 'assistant', 'content': turn['assistant']})
    return result, summary, tokens


def request_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@asynccontextmanager
async def conversation_lock(conversation_id: str, *, ttl: int | None = None):
    """Serialize state mutation for one conversation across API replicas."""
    key = f"xuanshu:conversation-lock:{conversation_id}"
    token = uuid.uuid4().hex
    acquired = await redis.set(
        key, token, nx=True, ex=max(10, int(ttl or settings.conversation_lock_seconds))
    )
    if not acquired:
        raise HTTPException(409, "当前对话正在处理另一条请求，请稍后重试")
    try:
        yield
    finally:
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1, key, token,
        )
