"""CrewAI Memory helpers that do not require a separate embedding service."""
import hashlib
import math
from pathlib import Path

from crewai import Memory
from crewai.memory.storage.lancedb_storage import LanceDBStorage


class LocalEmbeddingFunction:
    """Stable character n-gram embeddings for private, local memory retrieval."""

    def __init__(self, dimensions: int = 384):
        self.dimensions = dimensions

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(str(text)) for text in texts]

    def _embed(self, text: str) -> list[float]:
        normalized = ' '.join(text.casefold().split())
        vector = [0.0] * self.dimensions
        tokens = list(normalized)
        tokens.extend(normalized[index:index + size]
                      for size in (2, 3) for index in range(max(0, len(normalized) - size + 1)))
        for token in tokens:
            digest = hashlib.blake2b(token.encode('utf-8'), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], 'big') % self.dimensions
            vector[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def persistent_memory(path: Path, llm, root_scope: str) -> Memory:
    path.mkdir(parents=True, exist_ok=True)
    return Memory(
        llm=llm,
        storage=LanceDBStorage(path=path, vector_dim=384),
        embedder=LocalEmbeddingFunction(),
        root_scope=root_scope,
    )
