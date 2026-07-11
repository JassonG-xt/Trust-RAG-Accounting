"""Application-owned dependency assembly.

The default assembly preserves the existing local singleton implementations.
Tests and later production adapters can inject implementations explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..review import (
    LocalReviewActionStore,
    LocalReviewCheckpointStore,
    ReviewService,
    get_review_action_store,
    get_review_checkpoint_store,
)
from ..services.document_repository import DocumentRepository, get_repository
from ..tracing import LocalTraceCollector, get_local_trace_collector
from .config import Settings, get_settings


class DocumentCatalog(Protocol):
    """Read seam used by the HTTP document diagnostics."""

    @property
    def source(self) -> str | None: ...

    def describe(self) -> list[dict]: ...

    def chunk_count(self) -> int: ...


@dataclass(frozen=True)
class ApplicationContainer:
    settings: Settings
    document_catalog: DocumentCatalog
    review_service: ReviewService
    trace_collector: LocalTraceCollector
    _settings_provider: Callable[[], Settings] | None = field(default=None, repr=False)
    _document_catalog_provider: Callable[[], DocumentCatalog] | None = field(
        default=None, repr=False
    )
    _review_service_provider: Callable[[], ReviewService] | None = field(
        default=None, repr=False
    )
    _trace_collector_provider: Callable[[], LocalTraceCollector] | None = field(
        default=None, repr=False
    )

    def current_settings(self) -> Settings:
        return self._settings_provider() if self._settings_provider else self.settings

    def current_document_catalog(self) -> DocumentCatalog:
        if self._document_catalog_provider:
            return self._document_catalog_provider()
        return self.document_catalog

    def current_review_service(self) -> ReviewService:
        if self._review_service_provider:
            return self._review_service_provider()
        return self.review_service

    def current_trace_collector(self) -> LocalTraceCollector:
        if self._trace_collector_provider:
            return self._trace_collector_provider()
        return self.trace_collector


def _build_current_review_service() -> ReviewService:
    return ReviewService(get_review_checkpoint_store(), get_review_action_store())


def build_application_container(settings: Settings | None = None) -> ApplicationContainer:
    """Build the local application implementation graph.

    With no explicit settings, reuse the legacy singletons so graph nodes and
    HTTP routes observe the same local stores during the transition. Explicit
    settings create isolated adapters for tests and future factories.
    """

    if settings is None:
        current = get_settings()
        documents: DocumentRepository = get_repository()
        checkpoints = get_review_checkpoint_store()
        actions = get_review_action_store()
        traces = get_local_trace_collector()
        settings_provider: Callable[[], Settings] | None = get_settings
        document_provider: Callable[[], DocumentCatalog] | None = get_repository
        review_provider: Callable[[], ReviewService] | None = _build_current_review_service
        trace_provider: Callable[[], LocalTraceCollector] | None = get_local_trace_collector
    else:
        current = settings
        documents = DocumentRepository()
        checkpoints = LocalReviewCheckpointStore(
            Path(current.trustrag_review_store_path),
            include_content=current.trustrag_review_include_content,
            max_entries=current.trustrag_review_max_entries,
        )
        actions = LocalReviewActionStore(
            Path(current.trustrag_review_actions_path),
            max_entries=current.trustrag_review_actions_max_entries,
        )
        traces = LocalTraceCollector(
            max_events=current.trustrag_trace_max_events,
            include_content=current.trustrag_trace_include_content,
        )
        settings_provider = None
        document_provider = None
        review_provider = None
        trace_provider = None

    return ApplicationContainer(
        settings=current,
        document_catalog=documents,
        review_service=ReviewService(checkpoints, actions),
        trace_collector=traces,
        _settings_provider=settings_provider,
        _document_catalog_provider=document_provider,
        _review_service_provider=review_provider,
        _trace_collector_provider=trace_provider,
    )
