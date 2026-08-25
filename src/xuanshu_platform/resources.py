"""Workspace resource serialization with encrypted tool credentials."""

import json
from typing import Any

from .crypto import decrypt_secret, encrypt_secret


SECRET_FIELDS = {'auth_token', 'headers'}


def secure_plugin_configuration(configuration: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Encrypt credentials before a plugin configuration reaches PostgreSQL."""
    secured = {key: value for key, value in (current or {}).items() if key not in SECRET_FIELDS}
    secured.update({key: value for key, value in configuration.items() if key not in SECRET_FIELDS})
    if 'auth_token' in configuration:
        token = str(configuration.get('auth_token') or '')
        if token or not (current or {}).get('auth_token_encrypted'):
            secured['auth_token_encrypted'] = encrypt_secret(token)
        else:
            secured['auth_token_encrypted'] = (current or {})['auth_token_encrypted']
    if 'headers' in configuration:
        headers = configuration.get('headers') or {}
        if not isinstance(headers, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
            raise ValueError('Additional headers 必须是字符串键值对象')
        if headers or not (current or {}).get('headers_encrypted'):
            secured['headers_encrypted'] = encrypt_secret(json.dumps(headers, ensure_ascii=False))
        else:
            secured['headers_encrypted'] = (current or {})['headers_encrypted']
    return secured


def public_plugin_configuration(configuration: dict[str, Any] | None) -> dict[str, Any]:
    """Return editable metadata without exposing plaintext or ciphertext secrets."""
    configuration = configuration or {}
    public = {
        key: value for key, value in configuration.items()
        if key not in {'auth_token', 'headers', 'auth_token_encrypted', 'headers_encrypted'}
    }
    public['auth_token'] = ''
    public['headers'] = {}
    public['has_auth_token'] = bool(configuration.get('auth_token_encrypted') or configuration.get('auth_token'))
    public['has_headers'] = bool(configuration.get('headers_encrypted') or configuration.get('headers'))
    return public


def runtime_plugin_configuration(configuration: dict[str, Any] | None) -> dict[str, Any]:
    """Decrypt a plugin only inside the backend/worker runtime boundary."""
    configuration = configuration or {}
    runtime = {
        key: value for key, value in configuration.items()
        if key not in {'auth_token_encrypted', 'headers_encrypted'}
    }
    if configuration.get('auth_token_encrypted'):
        runtime['auth_token'] = decrypt_secret(configuration['auth_token_encrypted'])
    if configuration.get('headers_encrypted'):
        runtime['headers'] = json.loads(decrypt_secret(configuration['headers_encrypted']))
    return runtime
