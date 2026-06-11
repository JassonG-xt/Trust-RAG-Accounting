"""Run the faithfulness suite against the REAL workflow and print the
baseline numbers. This is the un-self-corrected baseline that Phase 3
(self-correction loop) must improve upon.

The script is self-sufficient: it auto-ingests ``sample_docs/`` into the
local document/chunk stores if they are missing (reusing the eval
runner's bootstrap), then drives the real ``workflow.run_query``.

Usage:
    python scripts/run_faithfulness_baseline.py
"""

import json

from backend.app.evals.faithfulness_runner import run_faithfulness_suite
from backend.app.evals.runner import (
    _DEFAULT_CHUNKS_OUT,
    _DEFAULT_DOCUMENTS_OUT,
    _ensure_corpus,
)


def main() -> None:
    # Bootstrap the corpus the same way the eval runner does, so a fresh
    # clone produces real retrieval rather than empty-evidence everywhere.
    _ensure_corpus(
        source=__import__("pathlib").Path("sample_docs"),
        documents_out=_DEFAULT_DOCUMENTS_OUT,
        chunks_out=_DEFAULT_CHUNKS_OUT,
        quiet=False,
    )

    # query_fn=None => uses the real workflow.run_query.
    summary = run_faithfulness_suite()
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
