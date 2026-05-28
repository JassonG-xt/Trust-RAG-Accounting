"""Create or update the TrustRAG accounting eval PR comment.

Environment variables:

* GITHUB_REPOSITORY
* GITHUB_TOKEN
* PR_NUMBER
* COMMENT_FILE
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


MARKER = "<!-- trustrag-accounting-eval-comment -->"
BOT_LOGIN = "github-actions[bot]"
API_ROOT = "https://api.github.com"


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    pr_number = os.environ.get("PR_NUMBER")
    comment_file = os.environ.get("COMMENT_FILE")
    missing = [
        name
        for name, value in {
            "GITHUB_REPOSITORY": repository,
            "GITHUB_TOKEN": token,
            "PR_NUMBER": pr_number,
            "COMMENT_FILE": comment_file,
        }.items()
        if not value
    ]
    if missing:
        print(f"[eval-comment] skipping: missing env vars: {', '.join(missing)}")
        return 0

    try:
        with open(comment_file, encoding="utf-8") as handle:
            body = handle.read()
    except OSError as exc:
        print(f"[eval-comment] failed to read COMMENT_FILE: {exc}", file=sys.stderr)
        return 1

    if MARKER not in body:
        print("[eval-comment] failed: comment body is missing stable marker", file=sys.stderr)
        return 1

    try:
        existing = _find_existing_comment(repository, token, pr_number)
        if existing is None:
            created = _create_comment(repository, token, pr_number, body)
            print(f"[eval-comment] created: {created.get('html_url', 'url unavailable')}")
        else:
            updated = _update_comment(repository, token, existing["id"], body)
            print(f"[eval-comment] updated: {updated.get('html_url', 'url unavailable')}")
    except Exception as exc:
        print(f"[eval-comment] GitHub API failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _find_existing_comment(repository: str, token: str, pr_number: str) -> dict | None:
    page = 1
    while True:
        comments = _api_request(
            "GET",
            f"{API_ROOT}/repos/{repository}/issues/{pr_number}/comments?per_page=100&page={page}",
            token,
        )
        if not isinstance(comments, list):
            raise RuntimeError("comments response was not a JSON array")
        for comment in comments:
            user = comment.get("user") or {}
            if user.get("login") == BOT_LOGIN and MARKER in (comment.get("body") or ""):
                return comment
        if len(comments) < 100:
            return None
        page += 1


def _create_comment(repository: str, token: str, pr_number: str, body: str) -> dict:
    return _api_request(
        "POST",
        f"{API_ROOT}/repos/{repository}/issues/{pr_number}/comments",
        token,
        {"body": body},
    )


def _update_comment(repository: str, token: str, comment_id: int, body: str) -> dict:
    return _api_request(
        "PATCH",
        f"{API_ROOT}/repos/{repository}/issues/comments/{comment_id}",
        token,
        {"body": body},
    )


def _api_request(method: str, url: str, token: str, payload: dict | None = None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "trustrag-accounting-eval-comment")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")

    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read().decode("utf-8")
    except Exception as exc:
        body = ""
        read = getattr(exc, "read", None)
        if callable(read):
            try:
                body = read().decode("utf-8")
            except Exception:
                body = ""
        raise RuntimeError(f"{method} {url} failed: {exc} {body}") from exc

    if not raw:
        return {}
    return json.loads(raw)


if __name__ == "__main__":
    sys.exit(main())
