"""index.md / log.md rendering and parsing tests."""

from __future__ import annotations

from backend.app.wiki.index import (
    LOG_LINE,
    append_log,
    format_log_entry,
    parse_wikilinks,
    render_index,
)
from backend.app.wiki.store import load_wiki

from ._meta import FIXTURE_WIKI


def test_render_index_lists_every_page_once_sorted():
    pages = load_wiki(FIXTURE_WIKI)
    text = render_index(pages)
    linked = parse_wikilinks(text)
    assert linked == set(pages)
    # Entries are sorted by page_id.
    body_lines = [ln for ln in text.splitlines() if ln.startswith("- [[")]
    assert body_lines == sorted(body_lines)


def test_parse_wikilinks_handles_alias_and_heading():
    text = "see [[page-a]] and [[page-b|Alias]] and [[page-c#section]]"
    assert parse_wikilinks(text) == {"page-a", "page-b", "page-c"}


def test_log_grammar_matches_formatted_entry():
    entry = format_log_entry("2026-07-21", "ingest", "Alpha Trading Co.")
    assert entry == "## [2026-07-21] ingest | Alpha Trading Co."
    assert LOG_LINE.match(entry)


def test_append_log_is_append_only_and_seeds_header(tmp_path):
    log_path = tmp_path / "log.md"
    append_log(log_path, [format_log_entry("2026-07-21", "ingest", "First")])
    append_log(log_path, [format_log_entry("2026-07-22", "ingest", "Second")])
    text = log_path.read_text()
    assert text.startswith("# Wiki Op Log")
    assert "ingest | First" in text
    assert "ingest | Second" in text
    # First entry precedes the second (append-only ordering).
    assert text.index("First") < text.index("Second")
