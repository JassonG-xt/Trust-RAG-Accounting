"""Run the faithfulness suite twice — loop OFF (baseline) vs loop ON — and
print the before→after delta. This is the Phase-3 headline result.

Usage:
    python scripts/run_faithfulness_comparison.py
"""

import json
import os
from pathlib import Path

from backend.app.evals.faithfulness_runner import run_faithfulness_suite
from backend.app.evals.runner import (
    _DEFAULT_CHUNKS_OUT,
    _DEFAULT_DOCUMENTS_OUT,
    _ensure_corpus,
)


def _run() -> dict:
    # get_settings() reads env fresh each call, and get_workflow() is cached,
    # so the suite drives run_query -> get_workflow built under the current
    # flag. Clear the workflow cache between runs so the flag change takes.
    from backend.app.graph import workflow

    workflow.get_workflow.cache_clear()
    return run_faithfulness_suite()


def main() -> None:
    _ensure_corpus(
        source=Path("sample_docs"),
        documents_out=_DEFAULT_DOCUMENTS_OUT,
        chunks_out=_DEFAULT_CHUNKS_OUT,
        quiet=False,
    )

    os.environ["TRUST_RAG_ENABLE_GROUNDEDNESS_SELF_CORRECTION"] = "false"
    before = _run()

    os.environ["TRUST_RAG_ENABLE_GROUNDEDNESS_SELF_CORRECTION"] = "true"
    after = _run()

    os.environ.pop("TRUST_RAG_ENABLE_GROUNDEDNESS_SELF_CORRECTION", None)

    print(json.dumps({
        "before": {
            "composite_groundedness": before["composite_groundedness"],
            "abstain": before["behavior_confusion"]["abstain"],
            "escalate": before["behavior_confusion"]["escalate"],
            "by_mode": {m: v["behavior_accuracy"] for m, v in before["by_mode"].items()},
        },
        "after": {
            "composite_groundedness": after["composite_groundedness"],
            "abstain": after["behavior_confusion"]["abstain"],
            "escalate": after["behavior_confusion"]["escalate"],
            "by_mode": {m: v["behavior_accuracy"] for m, v in after["by_mode"].items()},
        },
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
