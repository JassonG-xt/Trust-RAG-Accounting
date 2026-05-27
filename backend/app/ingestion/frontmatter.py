"""YAML front-matter parser for Markdown documents.

Spec:

```
---
key: value
key2: value2
---

# Body

Body text continues here.
```

The parser is intentionally strict — missing or malformed front matter
raises a typed exception so the ingestion CLI can report it clearly.
"""

from __future__ import annotations

from datetime import date, datetime

import yaml


class FrontMatterError(ValueError):
    """Base error for any front-matter parsing problem."""


class MissingFrontMatterError(FrontMatterError):
    """Raised when a Markdown file has no YAML front matter at all."""


_DELIMITER = "---"


def parse_frontmatter_markdown(raw_text: str) -> tuple[dict, str]:
    """Split ``raw_text`` into a (metadata, body) tuple.

    * Front matter must begin with a literal ``---`` line at position 0.
    * Front matter must close with another ``---`` line.
    * Body is everything after the closing delimiter, stripped of
      leading/trailing whitespace.
    * Empty / ``null`` YAML values become ``None`` (not the literal
      string ``""``).
    """

    if raw_text is None:
        raise FrontMatterError("input is None")

    text = raw_text.lstrip("﻿")  # strip BOM if present
    # Allow leading blank lines but the file must START with --- once
    # whitespace is consumed.
    stripped = text.lstrip()
    if not stripped.startswith(_DELIMITER):
        raise MissingFrontMatterError(
            "document has no YAML front matter (missing leading '---' delimiter)"
        )

    # Re-find delimiters on the original text so we can compute the body slice.
    lines = text.splitlines()

    # Skip any leading blank lines.
    start_idx = 0
    while start_idx < len(lines) and not lines[start_idx].strip():
        start_idx += 1

    if start_idx >= len(lines) or lines[start_idx].strip() != _DELIMITER:
        raise MissingFrontMatterError(
            "document has no YAML front matter (missing leading '---' delimiter)"
        )

    # Find closing delimiter.
    end_idx = None
    for i in range(start_idx + 1, len(lines)):
        if lines[i].strip() == _DELIMITER:
            end_idx = i
            break

    if end_idx is None:
        raise FrontMatterError("front matter is not closed with '---'")

    yaml_block = "\n".join(lines[start_idx + 1 : end_idx])
    body = "\n".join(lines[end_idx + 1 :]).strip()

    try:
        metadata = yaml.safe_load(yaml_block) if yaml_block.strip() else {}
    except yaml.YAMLError as exc:
        raise FrontMatterError(f"failed to parse YAML front matter: {exc}") from exc

    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise FrontMatterError(
            "front matter must be a YAML mapping, got "
            f"{type(metadata).__name__}"
        )

    # Normalize: empty strings → None; date/datetime → ISO strings so
    # the rest of the pipeline can treat them as plain text.
    def _normalize(value):
        if value == "":
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value

    metadata = {k: _normalize(v) for k, v in metadata.items()}

    return metadata, body
