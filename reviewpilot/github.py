"""Minimal GitHub REST client: read a pull request's files and post one review."""
from __future__ import annotations

import os

import httpx

from reviewpilot.diff import FileDiff, parse_patch
from reviewpilot.render import MARKER

API = os.getenv("GITHUB_API_URL", "https://api.github.com")


class GitHub:
    def __init__(self, repo: str, token: str | None = None):
        self.repo = repo
        token = token or os.environ["GITHUB_TOKEN"]
        self.http = httpx.Client(base_url=API, timeout=60, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    def pull(self, number: int) -> dict:
        r = self.http.get(f"/repos/{self.repo}/pulls/{number}")
        r.raise_for_status()
        return r.json()

    def files(self, number: int) -> list[FileDiff]:
        out, page = [], 1
        while True:
            r = self.http.get(f"/repos/{self.repo}/pulls/{number}/files", params={"per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            for f in batch:
                fd = parse_patch(f["filename"], f.get("patch", ""))
                fd.is_new, fd.is_deleted = f["status"] == "added", f["status"] == "removed"
                out.append(fd)
            if len(batch) < 100:
                return out
            page += 1

    def previous_reviews(self, number: int) -> list[dict]:
        r = self.http.get(f"/repos/{self.repo}/pulls/{number}/reviews", params={"per_page": 100})
        r.raise_for_status()
        return [rv for rv in r.json() if MARKER in (rv.get("body") or "")]

    def post_review(self, number: int, commit_id: str, body: str, comments: list[dict]) -> dict:
        payload = {"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments}
        r = self.http.post(f"/repos/{self.repo}/pulls/{number}/reviews", json=payload)
        if r.status_code == 422 and comments:
            # A comment pointed at a line GitHub rejects: fall back to a body-only review rather than failing.
            listed = "\n".join(f"- `{c['path']}:{c['line']}` {c['body']}" for c in comments)
            payload = {"commit_id": commit_id, "event": "COMMENT", "comments": [],
                       "body": body + "\n\n**Inline comments:**\n" + listed}
            r = self.http.post(f"/repos/{self.repo}/pulls/{number}/reviews", json=payload)
        r.raise_for_status()
        return r.json()
