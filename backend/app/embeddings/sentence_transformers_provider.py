"""Local sentence-transformers embedding provider.

This adapter keeps real open-source embeddings optional. The default project
path still uses ``MockEmbeddingProvider`` and does not import or install
``sentence-transformers``. Operators opt in with:

    EMBEDDING_PROVIDER=sentence_transformers
    EMBEDDING_MODEL=BAAI/bge-m3
    EMBEDDING_DIMENSION=1024
"""

from __future__ import annotations

from typing import Any

DEFAULT_SENTENCE_TRANSFORMERS_MODEL = "BAAI/bge-m3"
DEFAULT_SENTENCE_TRANSFORMERS_DIMENSION = 1024
DEFAULT_SENTENCE_TRANSFORMERS_BATCH_SIZE = 16

_INSTALL_HINT = (
    "sentence-transformers is not installed. Install the optional extra:\n"
    "    pip install -e '.[embeddings]'\n"
    "then set EMBEDDING_PROVIDER=sentence_transformers, "
    "EMBEDDING_MODEL=BAAI/bge-m3, and EMBEDDING_DIMENSION=1024."
)


class SentenceTransformersEmbeddingProvider:
    """Embeds text with a local SentenceTransformer model."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        dimension: int = DEFAULT_SENTENCE_TRANSFORMERS_DIMENSION,
        device: str | None = None,
        batch_size: int = DEFAULT_SENTENCE_TRANSFORMERS_BATCH_SIZE,
    ) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension must be positive, got {dimension}.")
        if batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {batch_size}.")

        self._model_name = model_name or DEFAULT_SENTENCE_TRANSFORMERS_MODEL
        self._dimension = int(dimension)
        self._device = device
        self._batch_size = int(batch_size)
        self._model = self._load_model()

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_text(self, text: str) -> list[float]:
        vectors = self.embed_texts([text])
        if not vectors:
            return [0.0] * self._dimension
        return vectors[0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        raw_vectors = self._model.encode(
            [text or "" for text in texts],
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=False,
        )
        return self._coerce_vectors(raw_vectors)

    def _load_model(self) -> Any:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(_INSTALL_HINT) from exc

        return SentenceTransformer(self._model_name, device=self._device)

    def _coerce_vectors(self, raw_vectors: Any) -> list[list[float]]:
        rows = raw_vectors.tolist() if hasattr(raw_vectors, "tolist") else raw_vectors
        vectors: list[list[float]] = []
        for idx, row in enumerate(rows):
            values = row.tolist() if hasattr(row, "tolist") else row
            vector = [float(value) for value in values]
            if len(vector) != self._dimension:
                raise ValueError(
                    f"Embedding model {self._model_name!r} returned vector "
                    f"dimension {len(vector)} for item {idx}, but "
                    f"EMBEDDING_DIMENSION is {self._dimension}. Update "
                    "EMBEDDING_DIMENSION to match the model and rebuild the "
                    "vector index."
                )
            vectors.append(vector)
        return vectors
