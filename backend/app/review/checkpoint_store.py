"""Local JSONL review checkpoint store.

Design points:

* **JSONL on disk, not in memory.** Reviewer dashboards (Phase 7) and
  local CLI tools should be able to consume the queue without
  the FastAPI process being up. JSONL is the simplest format that
  supports streaming append + line-by-line replay.
* **Thread-safe via a single :class:`threading.Lock`.** The store is
  shared by all FastAPI worker threads. Holding the lock during
  ``append`` / ``clear`` / ``_read_all`` keeps the file consistent
  without making the request thread cooperate.
* **Tolerant of malformed lines.** A corrupted JSONL line (manual
  edit, partial write from a previous run) doesn't crash the store —
  the line is skipped with a logger warning. The store is a *local
  debugging aid*, not a durable audit log; consistency >
  completeness here.
* **Default path is ``data/review_queue.jsonl``.** That directory is
  gitignored. Tests **must** override the path via tmp_path or
  monkeypatch, otherwise they would pollute the developer's local
  queue.
* **Max-entries enforcement.** When append pushes the file past
  ``max_entries``, the store rewrites the file with the *last*
  ``max_entries`` lines. This is O(n) but n is bounded (default
  1000) and append frequency is naturally low — one review per query
  on the hard-gate paths.

What this store is NOT:

* Not a durable audit log (use Postgres in Phase 5C).
* Not a queue with claim semantics (use a real queue in Phase 6+).
* Not multi-process safe beyond a single Lock — concurrent processes
  appending to the same file may interleave lines.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock
from typing import Any

from .models import ReviewCheckpoint

logger = logging.getLogger(__name__)


class LocalReviewCheckpointStore:
    """Append-only JSONL store of :class:`ReviewCheckpoint` records."""

    def __init__(
        self,
        path: Path | str,
        *,
        include_content: bool = False,
        max_entries: int = 1000,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._path = Path(path)
        self._include_content = bool(include_content)
        self._max_entries = int(max_entries)
        self._lock = Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def include_content(self) -> bool:
        return self._include_content

    @property
    def max_entries(self) -> int:
        return self._max_entries

    # -- writers -------------------------------------------------------------

    def append(self, checkpoint: ReviewCheckpoint) -> ReviewCheckpoint:
        """Append one checkpoint as a JSONL line. Returns the same object.

        Side effect: creates the parent directory if missing. Holds
        ``self._lock`` for the whole operation including the
        max-entries enforcement, so concurrent appends can't observe
        a half-truncated file.
        """

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(checkpoint.model_dump_json())
                f.write("\n")
            self._enforce_max_entries_locked()
        return checkpoint

    def clear(self) -> int:
        """Delete the JSONL file. Returns the number of entries removed."""

        with self._lock:
            entries = self._read_all_locked()
            if self._path.exists():
                self._path.unlink()
            return len(entries)

    # -- readers -------------------------------------------------------------

    def list_entries(self, limit: int | None = None) -> list[ReviewCheckpoint]:
        """Return checkpoints in append order. Latest entries are last."""

        with self._lock:
            entries = self._read_all_locked()
        if limit is None:
            return entries
        return entries[-limit:]

    def get(self, review_queue_id: str) -> ReviewCheckpoint | None:
        for entry in self.list_entries():
            if entry.review_queue_id == review_queue_id:
                return entry
        return None

    def __len__(self) -> int:
        return len(self.list_entries())

    # -- internals (lock-held) ----------------------------------------------

    def _read_all_locked(self) -> list[ReviewCheckpoint]:
        if not self._path.exists():
            return []
        entries: list[ReviewCheckpoint] = []
        for idx, raw_line in enumerate(
            self._path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entries.append(ReviewCheckpoint.model_validate_json(line))
            except Exception:
                logger.warning(
                    "skipping malformed review queue line %d in %s",
                    idx,
                    self._path,
                )
        return entries

    def _enforce_max_entries_locked(self) -> None:
        entries = self._read_all_locked()
        if len(entries) <= self._max_entries:
            return
        kept = entries[-self._max_entries:]
        with self._path.open("w", encoding="utf-8") as f:
            for e in kept:
                f.write(e.model_dump_json())
                f.write("\n")


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors get_repository / get_local_trace_collector).
# ---------------------------------------------------------------------------


_store_singleton: LocalReviewCheckpointStore | None = None
_store_lock = Lock()


def get_review_checkpoint_store() -> LocalReviewCheckpointStore:
    """Return the process-wide store, built from current settings."""

    global _store_singleton
    with _store_lock:
        if _store_singleton is None:
            # Lazy import to avoid review → config → review cycle.
            from ..core.config import get_settings

            settings = get_settings()
            _store_singleton = LocalReviewCheckpointStore(
                path=Path(settings.trustrag_review_store_path),
                include_content=bool(settings.trustrag_review_include_content),
                max_entries=int(settings.trustrag_review_max_entries),
            )
        return _store_singleton


def reset_review_checkpoint_store() -> None:
    """Drop the singleton — used by tests for fresh per-case state."""

    global _store_singleton
    with _store_lock:
        _store_singleton = None
