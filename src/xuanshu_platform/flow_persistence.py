"""CrewAI Flow persistence backed by the production Redis service."""
import json
from typing import Any

from crewai.flow.persistence.base import FlowPersistence
from pydantic import BaseModel, PrivateAttr
from redis import Redis

from .config import settings


class NullFlowPersistence(FlowPersistence):
    """Persistence metadata marker used when a caller does not supply a backend."""
    persistence_type: str = 'NullFlowPersistence'

    def init_db(self) -> None:
        return None

    def save_state(self, flow_uuid: str, method_name: str,
                   state_data: dict[str, Any] | BaseModel) -> None:
        return None

    def load_state(self, flow_uuid: str) -> dict[str, Any] | None:
        return None


class RedisFlowPersistence(FlowPersistence):
    persistence_type: str = 'RedisFlowPersistence'
    redis_url: str = settings.redis_url
    key_prefix: str = 'xuanshu:composer-flow:'
    _client: Redis = PrivateAttr()

    def model_post_init(self, _context: Any) -> None:
        self._client = Redis.from_url(self.redis_url, decode_responses=True)
        self.init_db()

    def init_db(self) -> None:
        # Redis is schema-less; connectivity is checked by the normal service health path.
        return None

    @staticmethod
    def _document(state_data: dict[str, Any] | BaseModel) -> dict[str, Any]:
        document = state_data.model_dump(mode='json') if isinstance(state_data, BaseModel) else dict(state_data)
        # The current encrypted DB profile is injected on every turn. Never persist its plaintext key.
        if isinstance(document.get('model'), dict):
            document['model'] = {key: value for key, value in document['model'].items() if key != 'api_key'}
        return document

    def save_state(self, flow_uuid: str, method_name: str,
                   state_data: dict[str, Any] | BaseModel) -> None:
        payload = self._document(state_data)
        self._client.set(f'{self.key_prefix}{flow_uuid}', json.dumps({
            'method_name': method_name,
            'state': payload,
        }, ensure_ascii=False))

    def load_state(self, flow_uuid: str) -> dict[str, Any] | None:
        raw = self._client.get(f'{self.key_prefix}{flow_uuid}')
        if not raw:
            return None
        document = json.loads(raw)
        state = document.get('state')
        return state if isinstance(state, dict) else None
