"""Deterministic guardrails that don't depend on which model is used.

* Injection scan: code under review is untrusted. Text that tries to instruct the reviewer is flagged
  (and the model is told, in the system prompt, that such text is data, not instructions).
* Grounding: every finding must quote real code at the line it cites. Findings whose quote isn't there
  are dropped, which removes the most common LLM failure in code review: confident comments about code
  that doesn't exist or isn't where the model says it is.
"""
from __future__ import annotations

import re

from reviewpilot.diff import FileDiff
from reviewpilot.models import Finding, Rejected

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"(note|message|instructions?) (to|for) (the )?(ai|llm|assistant|reviewer|model)",
    r"approve (this|the) (pr|pull request|change)",
    r"do not (report|flag|mention|comment)",
    r"you are now",
    r"system prompt",
]
_INJ = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)


def scan_injection(files: list[FileDiff]) -> list[tuple[str, int, str]]:
    hits = []
    for fd in files:
        for line, text in fd.added.items():
            if _INJ.search(text):
                hits.append((fd.path, line, text.strip()))
    return hits


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def ground(findings: list[Finding], files: list[FileDiff], file_text: dict[str, list[str]],
           window: int = 3) -> tuple[list[Finding], list[Rejected]]:
    """Keep findings whose evidence appears in the cited file within ±window lines; snap the line to it."""
    changed = {fd.path for fd in files}
    kept, rejected = [], []
    for f in findings:
        lines = file_text.get(f.path)
        if f.path not in changed:
            rejected.append(Rejected(finding=f, stage="grounding", reason="file is not part of this pull request"))
            continue
        if lines is None:
            rejected.append(Rejected(finding=f, stage="grounding", reason="file could not be read"))
            continue
        quote = _norm(f.evidence.splitlines()[0] if f.evidence.strip() else "")
        if len(quote) < 4:
            rejected.append(Rejected(finding=f, stage="grounding", reason="no code quoted as evidence"))
            continue
        lo, hi = max(1, f.line - window), min(len(lines), f.line + window)
        match = next((n for n in range(lo, hi + 1) if quote in _norm(lines[n - 1])), None)
        if match is None:
            rejected.append(Rejected(finding=f, stage="grounding",
                                     reason=f"quoted code not found near line {f.line}"))
            continue
        kept.append(f.model_copy(update={"line": match}))
    return dedupe(kept), rejected


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen, out = set(), []
    for f in findings:
        key = (f.path, f.line, f.category)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
