"""Translate retrieval-layer ``MetadataFilter`` to the vector store's
payload-filter DSL.

The retrieval layer's :class:`backend.app.retrieval.models.MetadataFilter`
expresses *what the workflow wants* (client, document_types,
policy_families, include_malicious). The vector store's payload
filter is a flat dict the in-memory store consumes literally and the
Qdrant adapter translates further.

Conversion rules:

* ``client`` → ``{"client_any_of": [client, None]}``. Firm-wide
  chunks (``client=None``) must remain reachable when the question
  names a specific client — that's the same semantic the keyword
  + BM25 retrievers enforce.
* ``document_types`` → ``{"document_type_any_of": [...]}``. Empty
  list disables the constraint.
* ``policy_families`` → ``{"policy_family_any_of": [...]}``.
* ``include_malicious=False`` → ``{"is_malicious": False}``.
  When ``True``, no constraint on ``is_malicious`` is emitted (the
  workflow's safety path runs).

The function is pure — no IO, no state.
"""

from __future__ import annotations

from typing import Any

from ..retrieval.models import MetadataFilter


def metadata_filter_to_payload_filter(
    metadata_filter: MetadataFilter,
) -> dict[str, Any]:
    """Build the internal payload-filter dict from a MetadataFilter."""

    pf: dict[str, Any] = {}

    if metadata_filter.client is not None:
        # Firm-wide (client=None) chunks remain visible — they answer
        # questions about any client.
        pf["client_any_of"] = [metadata_filter.client, None]

    if metadata_filter.document_types:
        pf["document_type_any_of"] = list(metadata_filter.document_types)

    if metadata_filter.policy_families:
        pf["policy_family_any_of"] = list(metadata_filter.policy_families)

    if not metadata_filter.include_malicious:
        pf["is_malicious"] = False

    return pf
