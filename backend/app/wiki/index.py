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

_WIKILINK = re.compile(r"\[\[([^\]|#\n]+?)(?:[#|][^\]\n]*)?\]\]")
LOG_LINE = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] ([\w-]+) \| (.+)$")

_LOG_HEADER = "# Wiki Op Log"
_INDEX_HEADER = "# Wiki Index"


def _strip_code_fences(text: str) -> str:
    """Drop fenced code blocks so ``[[...]]``-looking text inside a code fence
    is not mistaken for a wikilink."""

    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


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
    """Return the set of ``page_id`` targets referenced by closed ``[[...]]``
    links, ignoring anything inside code fences.

    Tolerates Obsidian ``[[id|alias]]`` and ``[[id#heading]]`` forms by
    keeping only the target id; an unclosed ``[[`` is not a link.
    """

    scan = _strip_code_fences(text)
    return {m.group(1).strip() for m in _WIKILINK.finditer(scan)}


# ---------------------------------------------------------------------------
# log.md
# ---------------------------------------------------------------------------


def format_log_entry(date: str, op: str, title: str) -> str:
    """Render and validate one op-log line against :data:`LOG_LINE`.

    Rejects newlines in the title (which would split the entry and let the tail
    escape the grammar check) and any op/date that breaks the grammar.
    """

    if "\n" in title or "\r" in title:
        raise ValueError("log entry title must not contain newlines")
    line = f"## [{date}] {op} | {title}"
    if not LOG_LINE.match(line):
        raise ValueError(f"log entry does not match grammar {LOG_LINE.pattern!r}: {line!r}")
    return line


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
