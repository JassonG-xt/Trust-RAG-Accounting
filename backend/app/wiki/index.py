"""``index.md`` and ``log.md`` maintenance and parsing.

``index.md`` is the agent's navigation catalog: exactly one entry per page,
each a resolvable ``[[page_id]]`` wikilink. ``log.md`` is an append-only op log
whose entries follow the grammar ``## [YYYY-MM-DD] <op> | <title>``. Both are
validated by the tier-1 lint (index consistency + log grammar invariants).

Dates are passed in by the caller (the applier derives them from the
proposal's ``created_at``) so this module stays deterministic and never reads
the wall clock.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import WikiPage

_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
LOG_LINE = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] (\w+) \| (.+)$")

_LOG_HEADER = "# Wiki Op Log"
_INDEX_HEADER = "# Wiki Index"


# ---------------------------------------------------------------------------
# index.md
# ---------------------------------------------------------------------------


def render_index(pages: dict[str, WikiPage]) -> str:
    """Render index.md — one ``[[page_id]]`` line per page, sorted by id."""

    lines = [_INDEX_HEADER, ""]
    for pid in sorted(pages):
        fm = pages[pid].frontmatter
        lines.append(f"- [[{pid}]] — {fm.title} ({fm.page_type}, {fm.status})")
    return "\n".join(lines) + "\n"


def parse_wikilinks(text: str) -> set[str]:
    """Return the set of ``page_id`` targets referenced by ``[[...]]`` links.

    Tolerates Obsidian ``[[id|alias]]`` and ``[[id#heading]]`` forms by
    keeping only the target id.
    """

    return {m.group(1).strip() for m in _WIKILINK.finditer(text)}


# ---------------------------------------------------------------------------
# log.md
# ---------------------------------------------------------------------------


def format_log_entry(date: str, op: str, title: str) -> str:
    return f"## [{date}] {op} | {title}"


def append_log(log_path: Path | str, entries: list[str]) -> Path:
    """Append op-log entries, seeding the header on first write."""

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8").rstrip("\n")
    else:
        existing = _LOG_HEADER
    body = "\n".join([existing, "", *entries]) if entries else existing
    log_path.write_text(body + "\n", encoding="utf-8")
    return log_path


def parse_log_lines(text: str) -> list[str]:
    """Return the ``## [...]`` heading lines in an op log (grammar-checked
    elsewhere by the lint)."""

    return [line for line in text.splitlines() if line.startswith("## [")]
