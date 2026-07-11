"""Persistence interfaces shared by local and durable implementations."""

from .protocols import ReviewActionRepository, ReviewCheckpointRepository

__all__ = ["ReviewActionRepository", "ReviewCheckpointRepository"]
