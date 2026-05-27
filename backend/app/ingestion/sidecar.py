"""Shared helper for sidecar-metadata loaders (PDF / DOCX).

PDF/DOCX files do not have a reliable in-file metadata channel for
business-domain fields (client, policy_family, replaces, ...), so the
ingestion layer requires a sibling YAML file:

    example.pdf
    example.metadata.yaml

The YAML payload follows the same shape as Markdown front matter.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .frontmatter import FrontMatterError


def sidecar_path(file_path: Path) -> Path:
    """Return the canonical sidecar path next to ``file_path``."""

    return file_path.with_name(file_path.stem + ".metadata.yaml")


def load_sidecar_metadata(file_path: Path) -> dict:
    """Load and normalize sidecar metadata for ``file_path``.

    Raises ``FrontMatterError`` if the sidecar is missing or malformed.
    """

    sidecar = sidecar_path(file_path)
    if not sidecar.exists():
        raise FrontMatterError(
            f"{file_path}: missing sidecar metadata file "
            f"({sidecar.name}). PDF/DOCX documents require an explicit "
            f"metadata.yaml — TrustRAG refuses to guess accounting fields."
        )

    raw = sidecar.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FrontMatterError(f"{sidecar}: failed to parse YAML: {exc}") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise FrontMatterError(
            f"{sidecar}: metadata YAML must be a mapping, got "
            f"{type(loaded).__name__}"
        )

    # Match Markdown's normalization: empty string → None, dates → ISO.
    from datetime import date, datetime

    def _normalize(value):
        if value == "":
            return None
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    return {k: _normalize(v) for k, v in loaded.items()}
