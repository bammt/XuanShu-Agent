"""Deterministic HTTP request binding, independent of Agent execution."""
import re
from collections.abc import Mapping
from typing import Any

VARIABLE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')

def _multipart_file(value: Any):
    """Convert a bound attachment object into the httpx file tuple format."""
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, Mapping) or 'data' not in value:
        return None
    filename = str(value.get('name') or value.get('filename') or 'upload.bin')
    content = value.get('data')
    if isinstance(content, str):
        content = content.encode()
    if not isinstance(content, (bytes, bytearray)):
        return None
    content_type = str(value.get('content_type') or 'application/octet-stream')
    return (filename, bytes(content), content_type)


def _attachment_bytes(value: Any) -> bytes | None:
    """Extract bytes from a bound upload object for a binary HTTP body."""
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, Mapping) and 'data' in value:
        content = value.get('data')
        if isinstance(content, str):
            return content.encode()
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
    return None


def bind(value: Any, arguments: dict) -> Any:
    if isinstance(value, dict):
        return {key: bind(item, arguments) for key, item in value.items()}
    if isinstance(value, list):
        return [bind(item, arguments) for item in value]
    if not isinstance(value, str):
        return value
    def resolve(name):
        if name not in arguments:
            raise ValueError(f'缺少工具输入变量：{name}')
        return arguments[name]
    match = VARIABLE.fullmatch(value)
    if match:
        return resolve(match.group(1))
    return VARIABLE.sub(lambda match: str(resolve(match.group(1))), value)


def build_http_request(endpoint: str, method: str, headers: dict, template: dict, arguments: dict) -> tuple[str, dict]:
    url = str(bind(endpoint, arguments))
    kwargs = {'headers': bind(headers, arguments), 'timeout': 30.0}
    if template.get('version') != 2:
        payload = {**template, **arguments}
        kwargs['params' if method.upper() == 'GET' else 'json'] = payload
        return url, kwargs
    kwargs['timeout'] = max(1.0, min(float(template.get('timeout', 30)), 300.0))
    kwargs['verify'] = bool(template.get('verify_ssl', True))
    kwargs['params'] = bind(template.get('params', {}), arguments)
    body_type = template.get('body_type', 'none')
    body = bind(template.get('body'), arguments)
    if body_type == 'json':
        kwargs['json'] = body
    elif body_type in {'form', 'multipart'}:
        if not isinstance(body, dict): raise ValueError('表单正文必须是键值对象')
        if body_type == 'multipart':
            files = {}
            for key, value in body.items():
                part = _multipart_file(value)
                if part is None:
                    # A ``None`` filename makes httpx emit a normal multipart
                    # field while retaining multipart/form-data even when the
                    # request has no uploaded file.
                    files[key] = (None, str(value) if value is not None else '')
                else:
                    files[key] = part
            kwargs['files'] = files
        else:
            kwargs['data'] = body
    elif body_type == 'raw':
        kwargs['content'] = str(body or '')
    elif body_type == 'binary':
        attachment = _attachment_bytes(body)
        kwargs['content'] = attachment if attachment is not None else (
            body if isinstance(body, (bytes, bytearray)) else str(body or '').encode()
        )
    elif body_type != 'none':
        raise ValueError(f'不支持的 HTTP 正文类型：{body_type}')
    return url, kwargs


def send_http_request(method: str, endpoint: str, kwargs: dict, template: dict):
    """Bounded retries; never retry validation errors or ordinary 4xx responses."""
    import time
    import httpx
    retries = max(0, min(int(template.get('max_retries', 0)), 5)) if template.get('version') == 2 else 0
    delay = max(0, min(float(template.get('retry_interval_ms', 1000)), 10000)) / 1000
    for attempt in range(retries + 1):
        try:
            response = httpx.request(method, endpoint, **kwargs)
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                return response
            if attempt == retries:
                response.raise_for_status()
                return response
        except httpx.TransportError:
            if attempt == retries:
                raise
        time.sleep(delay)
