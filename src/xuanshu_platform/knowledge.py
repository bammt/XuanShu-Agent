"""Workspace knowledge ingestion and CrewAI-compatible Qdrant storage."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from crewai.knowledge.knowledge import Knowledge
from crewai.knowledge.storage.base_knowledge_storage import BaseKnowledgeStorage
from crewai.rag.types import SearchResult
from openai import OpenAI
from pydantic import ConfigDict, PrivateAttr
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams

from .config import settings
from .crypto import decrypt_secret


def collection_name(workspace_id: int, knowledge_base_id: int) -> str:
    return f"ws_{workspace_id}_knowledge_{knowledge_base_id}"


def embedding_config(profile) -> dict[str, str]:
    model = profile.model.split('/', 1)[1] if profile.model.startswith('openai/') else profile.model
    return {
        'model': model,
        'api_key': decrypt_secret(profile.api_key_encrypted),
        'base_url': profile.base_url or '',
    }


class QdrantKnowledgeStorage(BaseKnowledgeStorage):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    collection: str
    embedding: dict[str, str]
    _qdrant: QdrantClient = PrivateAttr()
    _openai: OpenAI = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        self._qdrant = QdrantClient(url=settings.qdrant_url)
        self._openai = OpenAI(
            api_key=self.embedding.get('api_key') or 'not-required',
            base_url=self.embedding.get('base_url') or None,
            timeout=120,
            max_retries=4,
        )

    def _embed(self, values: list[str]) -> list[list[float]]:
        if not values:
            return []
        response = self._openai.embeddings.create(model=self.embedding['model'], input=values)
        return [item.embedding for item in response.data]

    def search(self, query: list[str], limit: int = 5, metadata_filter: dict[str, Any] | None = None,
               score_threshold: float = 0.35) -> list[SearchResult]:
        if not query or not self._qdrant.collection_exists(self.collection):
            return []
        vector = self._embed([' '.join(query)])[0]
        points = self._qdrant.query_points(
            collection_name=self.collection,
            query=vector,
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
        ).points
        return [SearchResult(id=str(item.id), content=str((item.payload or {}).get('content', '')),
                             metadata=dict((item.payload or {}).get('metadata') or {}), score=float(item.score))
                for item in points]

    async def asearch(self, query: list[str], limit: int = 5, metadata_filter: dict[str, Any] | None = None,
                      score_threshold: float = 0.35) -> list[SearchResult]:
        return await asyncio.to_thread(self.search, query, limit, metadata_filter, score_threshold)

    def save(self, documents: list[str]) -> None:
        self.save_documents(documents)

    def save_documents(self, documents: list[str], metadata: dict[str, Any] | None = None) -> None:
        if not documents:
            return
        vectors = self._embed(documents)
        if not self._qdrant.collection_exists(self.collection):
            self._qdrant.create_collection(self.collection, vectors_config=VectorParams(size=len(vectors[0]), distance=Distance.COSINE))
        metadata = metadata or {}
        points = [PointStruct(
            id=int(hashlib.sha256(f'{metadata.get("file_id", "")}:\n{content}'.encode()).hexdigest()[:15], 16),
            vector=vector,
            payload={'content': content, 'metadata': metadata},
        ) for index, (content, vector) in enumerate(zip(documents, vectors, strict=True))]
        self._qdrant.upsert(self.collection, points=points, wait=True)

    async def asave(self, documents: list[str]) -> None:
        await asyncio.to_thread(self.save, documents)

    def reset(self) -> None:
        if self._qdrant.collection_exists(self.collection):
            self._qdrant.delete_collection(self.collection)

    async def areset(self) -> None:
        await asyncio.to_thread(self.reset)


class MultiQdrantKnowledgeStorage(BaseKnowledgeStorage):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    stores: list[QdrantKnowledgeStorage]

    def search(self, query: list[str], limit: int = 5, metadata_filter: dict[str, Any] | None = None,
               score_threshold: float = 0.35) -> list[SearchResult]:
        merged = [result for store in self.stores for result in store.search(query, limit, metadata_filter, score_threshold)]
        return sorted(merged, key=lambda item: float(item.get('score', 0)), reverse=True)[:limit]

    async def asearch(self, query: list[str], limit: int = 5, metadata_filter: dict[str, Any] | None = None,
                      score_threshold: float = 0.35) -> list[SearchResult]:
        batches = await asyncio.gather(*(store.asearch(query, limit, metadata_filter, score_threshold) for store in self.stores))
        return sorted([item for batch in batches for item in batch],
                      key=lambda item: float(item.get('score', 0)), reverse=True)[:limit]

    def save(self, documents: list[str]) -> None:
        raise RuntimeError('运行时知识库为只读资源')

    async def asave(self, documents: list[str]) -> None:
        raise RuntimeError('运行时知识库为只读资源')

    def reset(self) -> None:
        raise RuntimeError('运行时知识库不能由 Agent 删除')

    async def areset(self) -> None:
        raise RuntimeError('运行时知识库不能由 Agent 删除')


def extract_text(name: str, data: bytes, content_type: str = '') -> str:
    suffix = Path(name).suffix.lower()
    if suffix == '.pdf':
        from pypdf import PdfReader
        return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(data)))
    if suffix == '.docx':
        from docx import Document
        return '\n'.join(paragraph.text for paragraph in Document(io.BytesIO(data)).paragraphs)
    if suffix in {'.json'}:
        parsed = json.loads(data.decode('utf-8'))
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    if suffix in {'.txt', '.md', '.csv', '.xml', '.yaml', '.yml'} or content_type.startswith('text/'):
        return data.decode('utf-8', errors='replace')
    raise ValueError('当前支持 PDF、DOCX、TXT、Markdown、CSV、JSON、XML 和 YAML 文件')


def chunks_for(text: str, chunk_size: int, overlap: int) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    size = max(200, min(chunk_size, 8000)); overlap = max(0, min(overlap, size // 2))
    return [normalized[index:index + size] for index in range(0, len(normalized), size - overlap)]


def ingest(workspace_id: int, knowledge_base_id: int, profile, name: str, data: bytes,
           content_type: str, chunk_size: int, chunk_overlap: int, file_id: int,
           parsing_strategy: str = 'auto') -> int:
    text = (data.decode('utf-8', errors='replace') if parsing_strategy == 'plain'
            else extract_text(name, data, content_type))
    chunks = chunks_for(text, chunk_size, chunk_overlap)
    if not chunks:
        raise ValueError('文件中没有可解析的文本内容')
    storage = QdrantKnowledgeStorage(collection=collection_name(workspace_id, knowledge_base_id),
                                      embedding=embedding_config(profile))
    storage.save_documents(chunks, {'file_id': file_id, 'file_name': name})
    return len(chunks)


def build_knowledge(workspace_id: int, resources: list[dict]) -> Knowledge | None:
    stores = [QdrantKnowledgeStorage(collection=collection_name(workspace_id, int(item['id'])),
                                     embedding=item['embedding']) for item in resources]
    if not stores:
        return None
    storage = MultiQdrantKnowledgeStorage(stores=stores)
    return Knowledge(collection_name=f'workspace_{workspace_id}', sources=[], storage=storage)


def delete_collection(workspace_id: int, knowledge_base_id: int) -> None:
    client = QdrantClient(url=settings.qdrant_url)
    name = collection_name(workspace_id, knowledge_base_id)
    if client.collection_exists(name):
        client.delete_collection(name)


def delete_file_vectors(workspace_id: int, knowledge_base_id: int, file_id: int) -> None:
    client = QdrantClient(url=settings.qdrant_url)
    name = collection_name(workspace_id, knowledge_base_id)
    if client.collection_exists(name):
        client.delete(name, points_selector=Filter(must=[FieldCondition(
            key='metadata.file_id', match=MatchValue(value=file_id),
        )]), wait=True)
