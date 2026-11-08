"""Unified-diff handling: which lines a PR changed, so comments land on lines GitHub can attach them to."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    path: str
    patch: str
    added: dict[int, str] = field(default_factory=dict)       # new line number -> text
    context: dict[int, str] = field(default_factory=dict)     # unchanged lines shown in hunks
    removed: list[str] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False

    def commentable(self, line: int) -> bool:
        """GitHub review comments can only attach to lines inside the diff hunks (added or context)."""
        return line in self.added or line in self.context


def parse_patch(path: str, patch: str) -> FileDiff:
    fd = FileDiff(path=path, patch=patch)
    new_line = 0
    for raw in patch.splitlines():
        m = HUNK.match(raw)
        if m:
            new_line = int(m.group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---") or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            fd.added[new_line] = raw[1:]
            new_line += 1
        elif raw.startswith("-"):
            fd.removed.append(raw[1:])
        elif new_line:
            fd.context[new_line] = raw[1:] if raw.startswith(" ") else raw
            new_line += 1
    return fd


def parse_unified(diff_text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    chunks = re.split(r"(?m)^diff --git ", diff_text)
    for chunk in chunks:
        if not chunk.strip():
            continue
        header, _, body = chunk.partition("\n")
        m = re.match(r"a/(\S+) b/(\S+)", header)
        if not m:
            continue
        path = m.group(2)
        start = body.find("@@")
        fd = parse_patch(path, body[start:] if start >= 0 else "")
        fd.is_new = "new file mode" in body[: max(start, 0)]
        fd.is_deleted = "deleted file mode" in body[: max(start, 0)]
        files.append(fd)
    return files


def local_diff(repo: Path, base: str, head: str = "HEAD") -> str:
    """Diff of `head` against the merge base with `base`, like a pull request shows."""
    return subprocess.run(["git", "-C", str(repo), "diff", "--no-color", "-U3", f"{base}...{head}"],
                          capture_output=True, text=True, check=True).stdout


def render_for_prompt(files: list[FileDiff], max_chars: int = 60_000) -> str:
    """Numbered view of changed lines for the model: '+' added, ' ' context, with new-file line numbers."""
    parts = []
    for fd in files:
        if fd.is_deleted:
            parts.append(f"### {fd.path} (deleted)")
            continue
        lines = sorted({**fd.context, **fd.added}.items())
        body = "\n".join(f"{n:>5} {'+' if n in fd.added else ' '} {text}" for n, text in lines)
        if fd.removed:
            body += "\n  removed lines:\n" + "\n".join(f"      - {t}" for t in fd.removed[:40])
        parts.append(f"### {fd.path}{' (new file)' if fd.is_new else ''}\n{body}")
    text = "\n\n".join(parts)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... (diff truncated)"
