"""Optional local cross-encoder rerankers."""

from __future__ import annotations

import math
from typing import Any

from ..retrieval.models import ScoreBreakdown, ScoredChunk


class ExternalRerankerNotConfiguredError(RuntimeError):
    """Raised when the optional local reranker dependency is unavailable."""


_BGE_HINT = (
    "BGE reranker requires the optional reranker dependencies. Install with:\n"
    "    pip install -e '.[reranker]'\n"
    "Then set RERANKER_PROVIDER=bge and optionally RERANKER_MODEL, "
    "RERANKER_DEVICE, and RERANKER_BATCH_SIZE."
)
_MALICIOUS_RERANKED_CAP = 0.20


def _identity(value: Any) -> Any:
    return value


class BGEReranker:
    """Local BGE cross-encoder adapter using sentence-transformers."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        *,
        device: str | None = None,
        batch_size: int = 8,
        weight: float = 0.15,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive.")
        if weight < 0:
            raise ValueError("weight must be non-negative.")
        self._model_name = model_name
        self._device = device
        self._batch_size = int(batch_size)
        self._weight = float(weight)
        self._model = self._load_model()

    @property
    def name(self) -> str:
        return "bge"

    def rerank(
        self,
        query: str,
        candidates: list[ScoredChunk],
        *,
        top_k: int | None = None,
    ) -> list[ScoredChunk]:
        if not candidates:
            return []

        pairs = [
            [query, self._candidate_text(candidate)]
            for candidate in candidates
        ]
        raw_scores = self._model.predict(
            pairs,
            batch_size=self._batch_size,
            convert_to_numpy=False,
            show_progress_bar=False,
            activation_fct=_identity,
        )
        values = self._coerce_scores(raw_scores)
        if len(values) != len(candidates):
            raise ValueError(
                "BGE reranker returned a different number of scores than candidates."
            )

        reranked = [
            self._apply_score(candidate, self._sigmoid(raw_score))
            for candidate, raw_score in zip(candidates, values, strict=True)
        ]
        reranked.sort(key=lambda candidate: (-candidate.score, candidate.chunk_id))
        if top_k is not None and top_k >= 0:
            return reranked[:top_k]
        return reranked

    def _load_model(self) -> Any:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ExternalRerankerNotConfiguredError(_BGE_HINT) from exc
        return CrossEncoder(self._model_name, device=self._device)

    @staticmethod
    def _candidate_text(candidate: ScoredChunk) -> str:
        return "\n".join(
            part
            for part in (candidate.title, candidate.section_title, candidate.content)
            if part
        )

    @staticmethod
    def _coerce_scores(raw_scores: Any) -> list[float]:
        values = raw_scores.tolist() if hasattr(raw_scores, "tolist") else raw_scores
        return [float(value) for value in values]

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            factor = math.exp(-value)
            return 1.0 / (1.0 + factor)
        factor = math.exp(value)
        return factor / (1.0 + factor)

    def _apply_score(self, candidate: ScoredChunk, relevance: float) -> ScoredChunk:
        breakdown = candidate.score_breakdown.model_copy(
            update={"reranker": round(self._weight * relevance, 4)}
        )
        total = max(0.0, breakdown.total())
        if candidate.is_malicious and total > _MALICIOUS_RERANKED_CAP:
            breakdown = ScoreBreakdown.model_validate(breakdown.model_dump())
            breakdown.malicious_penalty = round(
                breakdown.malicious_penalty - (total - _MALICIOUS_RERANKED_CAP),
                4,
            )
            total = _MALICIOUS_RERANKED_CAP
        return candidate.model_copy(
            update={
                "score": round(total, 4),
                "score_breakdown": breakdown,
            }
        )


__all__ = ["BGEReranker", "ExternalRerankerNotConfiguredError"]
