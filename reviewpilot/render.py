"""Turn a ReviewResult into a GitHub review body, inline comments, or terminal output."""
from __future__ import annotations

from reviewpilot.diff import FileDiff
from reviewpilot.models import Finding, ReviewResult

ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
MARKER = "<!-- reviewpilot -->"


def comment_body(f: Finding) -> str:
    text = f"{ICON[f.severity]} **{f.title}** · `{f.category}` · {f.severity}\n\n{f.explanation}"
    if f.suggestion:
        text += f"\n\n**Suggestion:** {f.suggestion}"
    return text


def split_inline(result: ReviewResult, files: list[FileDiff]) -> tuple[list[dict], list[Finding]]:
    """Inline comments for lines GitHub can attach to; the rest go in the review body."""
    by_path = {fd.path: fd for fd in files}
    inline, body_only = [], []
    for f in result.findings:
        fd = by_path.get(f.path)
        if fd and fd.commentable(f.line):
            inline.append({"path": f.path, "line": f.line, "side": "RIGHT", "body": comment_body(f)})
        else:
            body_only.append(f)
    return inline, body_only


def review_body(result: ReviewResult, body_only: list[Finding], model_label: str) -> str:
    counts = {s: sum(f.severity == s for f in result.findings) for s in ICON}
    tally = ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "none"
    lines = [MARKER, f"### ReviewPilot: {result.verdict()}", "",
             result.summary or "", "", f"**Findings:** {tally}"]
    if result.injection_warnings:
        lines += ["", "> ⚠️ This pull request contains text addressed to AI reviewers. It was treated as data, "
                      "not instructions:"]
        lines += [f"> `{w}`" for w in result.injection_warnings[:5]]
    if body_only:
        lines += ["", "**Findings outside the changed lines:**"]
        lines += [f"- `{f.path}:{f.line}` {comment_body(f)}" for f in body_only]
    if result.rejected:
        lines += ["", f"<details><summary>{len(result.rejected)} candidate comment(s) dropped by verification</summary>",
                  ""]
        lines += [f"- `{r.finding.path}:{r.finding.line}` {r.finding.title}: {r.stage}, {r.reason}"
                  for r in result.rejected[:20]]
        lines += ["", "</details>"]
    lines += ["", f"<sub>{model_label} · {result.tool_calls} tool calls · {result.tokens:,} tokens · "
                  f"${result.cost_usd:.3f}{' · stopped: ' + result.stop_reason if result.stop_reason else ''}</sub>"]
    return "\n".join(lines)


def terminal(result: ReviewResult) -> str:
    out = [f"ReviewPilot: {result.verdict()}", result.summary, ""]
    if result.plan:
        out += ["Plan:"] + [f"  {i}. {s}" for i, s in enumerate(result.plan, 1)] + [""]
    for f in result.findings:
        out.append(f"{ICON[f.severity]} {f.path}:{f.line}  [{f.category}] {f.title}")
        out.append(f"    {f.explanation}")
        out.append(f"    > {f.evidence}")
    if result.rejected:
        out += ["", f"Dropped by verification ({len(result.rejected)}):"]
        out += [f"  - {r.finding.path}:{r.finding.line} {r.finding.title} ({r.stage}: {r.reason})" for r in result.rejected]
    if result.injection_warnings:
        out += ["", "Prompt-injection text detected:"] + [f"  {w}" for w in result.injection_warnings]
    out += ["", f"{result.tool_calls} tool calls · {result.tokens:,} tokens · ${result.cost_usd:.3f}"]
    return "\n".join(out)
