"""Provider-aware CrewAI LLM construction and structured-output handling."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from crewai import LLM
from pydantic import BaseModel


# These providers expose an OpenAI-shaped chat endpoint but do not reliably
# implement OpenAI's beta parse/json_schema contract.  They receive the schema
# in the prompt and are validated locally after the single model call.
PROMPT_STRUCTURED_PROVIDERS = {'openai-compatible', 'ollama', 'ollama_chat', 'custom'}

THINKING_BUDGETS = {
    'minimal': 1024,
    'low': 2048,
    'medium': 4096,
    'high': 8192,
    'max': 16384,
}


def is_openai_compatible_profile(profile: dict | None) -> bool:
    profile = profile or {}
    provider = str(profile.get('provider') or '').strip().lower()
    if provider in PROMPT_STRUCTURED_PROVIDERS:
        return True
    if provider != 'openai' or not profile.get('base_url'):
        return False
    hostname = (urlparse(str(profile['base_url'])).hostname or '').lower()
    return hostname not in {'api.openai.com', 'api.openai.azure.com'}


def uses_openai_compatible_transport(profile: dict | None) -> bool:
    """Whether provider-specific fields are sent through an OpenAI-shaped API."""
    profile = profile or {}
    provider = str(profile.get('provider') or '').strip().lower()
    return is_openai_compatible_profile(profile) or provider in {
        'dashscope', 'hosted_vllm', 'ollama', 'ollama_chat', 'openrouter',
    }


def compatible_thinking_params(thinking_mode: str, effort: str) -> tuple[str, dict[str, Any]]:
    """Build request-level switches understood by common compatible servers.

    The fields intentionally overlap. Compatible gateways ignore fields they do
    not implement, while the explicit false values override provider or chat
    template defaults on DeepSeek, Bailian, vLLM, and SGLang.
    """
    enabled = thinking_mode == 'enabled'
    reasoning_effort = (
        'none' if not enabled
        else {'minimal': 'low'}.get(effort, effort or 'medium')
    )
    chat_template_kwargs: dict[str, Any] = {
        'enable_thinking': enabled,
        'thinking': enabled,
        'reasoning_effort': reasoning_effort,
    }
    extra_body: dict[str, Any] = {
        # DeepSeek official Chat Completions format.
        'thinking': {'type': 'enabled' if enabled else 'disabled'},
        # Alibaba Bailian OpenAI-compatible format.
        'enable_thinking': enabled,
        # vLLM and SGLang request-level chat-template override.
        'chat_template_kwargs': chat_template_kwargs,
    }
    if enabled:
        extra_body['thinking_effort'] = effort or 'medium'
        extra_body['thinking_budget'] = THINKING_BUDGETS.get(effort, THINKING_BUDGETS['medium'])
    return reasoning_effort, extra_body


def profile_llm_kwargs(profile: dict | None, fallback_model: str | None = None) -> dict:
    profile = profile or {}
    model = profile.get('model') or fallback_model
    if not model:
        raise ValueError('未配置模型 ID')
    kwargs = {
        'model': model,
        'api_key': profile.get('api_key') or None,
        'base_url': profile.get('base_url') or None,
        'timeout': profile.get('timeout', 180),
        'max_retries': profile.get('max_retries', 5),
    }
    for key in ('temperature', 'max_tokens'):
        if profile.get(key) is not None:
            kwargs[key] = profile[key]

    thinking_mode = str(profile.get('thinking_mode') or 'auto').strip().lower()
    effort = str(profile.get('thinking_effort') or '').strip().lower()
    if thinking_mode not in {'auto', 'enabled', 'disabled'}:
        thinking_mode = 'auto'
    if effort not in {'minimal', 'low', 'medium', 'high', 'max'}:
        effort = ''

    provider = str(profile.get('provider') or '').strip().lower()
    additional_params: dict[str, Any] = {}
    # Only pass a thinking switch when the user explicitly configured one.
    # Auto intentionally follows the model provider's default.
    if thinking_mode != 'auto':
        if provider in {'anthropic'}:
            budget = {
                'minimal': 1024, 'low': 2048, 'medium': 4096,
                'high': 8192, 'max': 16384,
            }.get(effort, 4096)
            if thinking_mode == 'enabled':
                configured_max = kwargs.get('max_tokens')
                if configured_max is not None and int(configured_max) <= budget:
                    raise ValueError('Anthropic 开启思考时 max_tokens 必须大于思考强度对应的预算')
                if configured_max is None:
                    kwargs['max_tokens'] = budget + 4096
            kwargs['thinking'] = {
                'type': thinking_mode,
                **({'budget_tokens': budget}
                   if thinking_mode == 'enabled' else {}),
            }
        elif provider in {'google', 'gemini'}:
            if 'gemini-3' in str(model).lower():
                kwargs['thinking_config'] = {
                    'thinking_level': (
                        'minimal' if thinking_mode == 'disabled'
                        else effort if effort in {'minimal', 'low', 'medium', 'high'} else 'medium'
                    ),
                }
            else:
                budget = {
                    'minimal': 512, 'low': 1024, 'medium': 4096,
                    'high': 8192, 'max': 16384,
                }.get(effort, -1)
                kwargs['thinking_config'] = {
                    'thinking_budget': 0 if thinking_mode == 'disabled' else budget,
                }
        elif provider in {'openai', 'azure'} and not is_openai_compatible_profile(profile):
            official_effort = {
                'minimal': 'low',
                'max': 'high',
            }.get(effort, effort or 'medium')
            kwargs['reasoning_effort'] = 'none' if thinking_mode == 'disabled' else official_effort

        if uses_openai_compatible_transport(profile):
            compatible_effort, extra_body = compatible_thinking_params(thinking_mode, effort)
            # CrewAI's native compatible adapter only forwards reasoning_effort
            # from additional_params for non-o1 model names. Keep the declared
            # field as well so other adapters receive the same explicit choice.
            kwargs['reasoning_effort'] = compatible_effort
            additional_params['reasoning_effort'] = compatible_effort
            additional_params['extra_body'] = extra_body
    if additional_params:
        kwargs['additional_params'] = additional_params
    if is_openai_compatible_profile(profile):
        if not kwargs['base_url']:
            raise ValueError(f'{provider} 模型连接必须配置 Base URL')
        if not kwargs['api_key']:
            kwargs['api_key'] = 'not-required'
        # CrewAI's native OpenAI adapter supports arbitrary model names when
        # custom_openai is explicit. This avoids requiring LiteLLM just to use
        # a local vLLM/Ollama-compatible endpoint.
        kwargs['custom_openai'] = True
    elif provider in {'ollama', 'ollama_chat', 'hosted_vllm', 'openrouter', 'dashscope', 'cerebras'}:
        prefix = str(model).split('/', 1)[0].lower()
        if prefix != provider:
            kwargs['provider'] = provider
    return kwargs


def profile_llm(profile: dict | None, fallback_model: str | None = None) -> LLM:
    return LLM(**profile_llm_kwargs(profile, fallback_model))


def uses_prompt_structured_output(profile: dict | None) -> bool:
    return is_openai_compatible_profile(profile)


def structured_messages(messages: list[dict], response_model: type[BaseModel]) -> list[dict]:
    schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False, separators=(',', ':'))
    instruction = (
        '当前模型连接不保证支持 OpenAI response_format/json_schema。请只返回一个合法 JSON 对象，'
        '不要使用 Markdown 代码块、解释文字或前后缀。返回对象必须符合以下 JSON Schema：\n' + schema
    )
    result = [dict(item) for item in messages]
    for item in result:
        if item.get('role') == 'system':
            item['content'] = f"{item.get('content', '')}\n\n{instruction}"
            break
    else:
        result.insert(0, {'role': 'system', 'content': instruction})
    return result


def kickoff_structured(agent, messages: list[dict], response_model: type[BaseModel],
                       profile: dict | None, *, label: str = 'agent'):
    started = time.perf_counter()
    if uses_prompt_structured_output(profile):
        prepared = structured_messages(messages, response_model)
        output = agent.kickoff(messages=prepared)
    else:
        prepared = messages
        output = agent.kickoff(messages=messages, response_format=response_model)
    _log_structured_call(label, prepared, response_model, started, getattr(output, 'usage_metrics', None))
    return output


def _usage_dict(usage: Any) -> dict:
    if hasattr(usage, 'model_dump'):
        usage = usage.model_dump()
    return usage if isinstance(usage, dict) else {}


def _log_structured_call(label: str, messages: list[dict], response_model: type[BaseModel],
                         started: float, usage: Any) -> None:
    prompt_chars = sum(len(str(item.get('content', ''))) for item in messages)
    schema_chars = len(json.dumps(response_model.model_json_schema(), ensure_ascii=False))
    usage = _usage_dict(usage)
    logging.info(
        'composer_llm label=%s duration_ms=%d prompt_chars=%d schema_chars=%d usage=%s',
        label, round((time.perf_counter() - started) * 1000), prompt_chars, schema_chars,
        json.dumps(usage or {}, ensure_ascii=False, default=str),
    )


def parse_structured_output(
    output: Any,
    response_model: type[BaseModel],
    normalize: Callable[[Any], Any] | None = None,
):
    """Validate provider output, optionally normalizing legacy JSON first."""
    def validate(candidate: Any):
        if normalize is not None:
            candidate = normalize(candidate)
        return response_model.model_validate(candidate)

    structured = getattr(output, 'pydantic', None)
    if isinstance(structured, response_model):
        return structured
    if structured is not None:
        return validate(structured)
    if isinstance(output, response_model):
        return output
    raw = getattr(output, 'raw', output)
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith('```'):
            lines = text.splitlines()
            text = '\n'.join(lines[1:-1]).strip() if len(lines) >= 3 else text
        try:
            if normalize is None:
                return response_model.model_validate_json(text)
            return validate(json.loads(text))
        except (ValueError, json.JSONDecodeError):
            decoder = json.JSONDecoder()
            for index, char in enumerate(text):
                if char != '{':
                    continue
                try:
                    candidate, _ = decoder.raw_decode(text[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    return validate(candidate)
            raise
    return validate(raw)
