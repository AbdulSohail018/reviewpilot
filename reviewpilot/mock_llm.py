"""Deterministic offline stand-in for an LLM reviewer, for tests, CI and key-free demos.

It finds problems the honest way where it can: generic code checks on the added lines, the test results it
asked for, and search results for renamed names. It also reproduces common LLM reviewer failures, chosen
deterministically from a hash of the diff:

  * ungrounded comments: quoting code that is not in the file (the grounding check should drop these);
  * confident false claims about correct code, e.g. "possible None dereference" (the skeptic should drop these);
  * a style nit when reviewing in a single pass;
  * following instructions planted in the code, when the prompt does not mark the diff as untrusted.

It has no real reasoning, so it misses bugs that need judgement (see evals/cases.py, found_by="reasoning").
Benchmark numbers from this model show that the harness mechanics work; use a real provider for real numbers.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from reviewpilot.guards import _INJ

# (category, severity, pattern, title, explanation, replacement-builder)
RULES: list[tuple[str, str, str, str, str]] = [
    ("sql-injection", "critical", r"execute\(\s*f[\"']|execute\(.*[\"']\s*%\s*\w|execute\(.*\.format\(",
     "SQL built from user input", "The query is assembled with string formatting, so a crafted value can change "
                                  "the SQL. Pass values as parameters (`?` placeholders) instead."),
    ("hardcoded-secret", "high", r"(?i)\b\w*(api_key|secret|password|token)\w*\s*=\s*[\"'][^\"']{12,}[\"']",
     "Secret committed to source", "A live credential in the repository is exposed to everyone with read access "
                                   "and stays in git history. Load it from the environment or a secret store."),
    ("code-injection", "high", r"\beval\(|\bexec\(",
     "eval() on external input", "eval executes arbitrary Python. Parse the input explicitly (e.g. json.loads or "
                                 "int() per value)."),
    ("command-injection", "high", r"shell\s*=\s*True",
     "Shell command built from variables", "With shell=True, paths containing shell metacharacters run arbitrary "
                                           "commands. Pass an argument list without shell=True, or use shutil.copy."),
    ("mutable-default", "high", r"def \w+\(.*=\s*(\[\]|\{\}|set\(\))",
     "Mutable default argument", "The default list is created once and shared between calls, so state leaks "
                                 "across calls. Default to None and create the list inside the function."),
    ("resource-leak", "medium", r"^\s*\w+\s*=\s*open\(",
     "File opened without being closed", "The handle is never closed, so buffered writes can be lost and file "
                                         "descriptors leak. Use `with open(...) as f:`."),
]
RULE_CATEGORIES = {r[0] for r in RULES} | {"swallowed-exception", "division-by-zero", "prompt-injection"}


def _h(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


def _diff_lines(text: str) -> list[tuple[str, int, str, bool]]:
    """Parse render_for_prompt output back into (path, line, code, added)."""
    out, path = [], None
    for raw in text.splitlines():
        if raw.startswith("### "):
            path = raw[4:].split(" (")[0].strip()
            continue
        m = re.match(r"^\s*(\d+) ([+ ]) (.*)$", raw)
        if m and path:
            out.append((path, int(m.group(1)), m.group(3), m.group(2) == "+"))
    return out


def _removed(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^\s{6}- (.*)$", text, re.M)]


def rule_findings(lines: list[tuple[str, int, str, bool]], removed: list[str]) -> list[dict]:
    found = []
    added = [(p, n, c) for p, n, c, a in lines if a]
    for i, (path, n, code) in enumerate(added):
        if path.startswith("tests/"):
            continue
        for cat, sev, pat, title, expl in RULES:
            if re.search(pat, code):
                found.append(dict(path=path, line=n, severity=sev, category=cat, title=title, explanation=expl,
                                  evidence=code.strip()))
        nxt = added[i + 1][2].strip() if i + 1 < len(added) else ""
        if re.match(r"\s*except(\s+Exception)?\s*:\s*$", code) and nxt == "pass":
            found.append(dict(path=path, line=n, severity="medium", category="swallowed-exception",
                              title="Errors silently ignored",
                              explanation="Every database error is discarded, so a failed save looks like a "
                                          "success. Log and re-raise, or catch the specific error you expect.",
                              evidence=code.strip()))
    # A removed guard makes an unchanged division line unsafe, so context lines are checked too.
    if any(r.strip().startswith("if not") for r in removed):
        for path, n, code, _added in lines:
            if not path.startswith("tests/") and re.search(r"/\s*len\(\w+\)", code):
                found.append(dict(path=path, line=n, severity="high", category="division-by-zero",
                                  title="Empty input now raises ZeroDivisionError",
                                  explanation="The `if not ...` guard was removed, so an empty list divides by zero.",
                                  evidence=code.strip()))
    return found


def skeptic_keeps(f: dict, evidence: str) -> tuple[bool, str]:
    cat, ev = f.get("category", ""), f.get("evidence", "")
    if cat in RULE_CATEGORIES and cat != "prompt-injection":
        rules = [r for r in RULES if r[0] == cat]
        if not rules or any(re.search(r[2], ev) for r in rules):
            return True, "pattern confirmed in the quoted code"
        return False, "the quoted code does not show this problem"
    if cat == "prompt-injection":
        return True, "instruction aimed at the reviewer is present in the diff"
    if cat == "logic":
        return ("FAILED" in evidence, "test suite fails on this change" if "FAILED" in evidence
                else "no failing test or other evidence supports this")
    if cat == "breaking-change":
        return True, "search shows remaining references to the old name"
    return False, "speculative: nothing in the code or tool output supports it"


class MockReviewer(BaseChatModel):
    tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "reviewpilot-mock"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "MockReviewer":  # type: ignore[override]
        return self.model_copy(update={"tool_names": [getattr(t, "name", None) or t["name"] for t in tools]})

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        system = next((str(m.content) for m in messages if isinstance(m, SystemMessage)), "")
        if "ROLE: PLANNER" in system:
            msg = self._plan(str(messages[-1].content))
        elif "ROLE: SKEPTIC" in system:
            msg = self._skeptic(str(messages[-1].content))
        else:
            msg = self._investigate(messages)
        chars = sum(len(str(m.content)) for m in messages) + len(str(msg.content))
        msg.usage_metadata = {"input_tokens": chars // 4, "output_tokens": len(str(msg.content)) // 4,
                              "total_tokens": chars // 4 + len(str(msg.content)) // 4}
        return ChatResult(generations=[ChatGeneration(message=msg)])

    # ---------------------------------------------------------------------------------------------
    def _plan(self, text: str) -> AIMessage:
        files = sorted({p for p, *_ in _diff_lines(text)})
        steps = [f"Read the changed code in {f}" for f in files[:3]]
        steps.append("Search for callers of any renamed or re-signatured names")
        steps.append("Run the test suite")
        return AIMessage(content=json.dumps({"steps": steps}))

    @staticmethod
    def _renames(text: str) -> list[str]:
        removed = _removed(text)
        olds = []
        for r in removed:
            m = re.match(r"\s*def (\w+)\(", r) or re.match(r"^([A-Z_][A-Z0-9_]+)\s*=", r)
            if m:
                olds.append(m.group(1))
        added_names = {m.group(1) for _, _, c, a in _diff_lines(text) if a
                       for m in [re.match(r"\s*def (\w+)\(", c) or re.match(r"^([A-Z_][A-Z0-9_]+)\s*=", c)] if m}
        return [o for o in olds if o not in added_names]

    def _investigate(self, messages: list[BaseMessage]) -> AIMessage:
        prompt = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
        defended = "<untrusted_pull_request>" in prompt
        lines, removed = _diff_lines(prompt), _removed(prompt)
        tool_out = {m.name: str(m.content) for m in messages if isinstance(m, ToolMessage)}
        searched = [str(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "search_code"]
        calls_made = [tc for m in messages if isinstance(m, AIMessage) for tc in (m.tool_calls or [])]
        injected = any(_INJ.search(c) for _, _, c, a in lines if a)

        # 1. Gather evidence with tools (one call per turn, like a careful reviewer).
        if self.tool_names and not (injected and not defended):
            if "run_tests" in self.tool_names and "run_tests" not in tool_out:
                return self._call("run_tests", {})
            for old in self._renames(prompt):
                if not any(tc["args"].get("pattern") == rf"\b{old}\b" for tc in calls_made):
                    return self._call("search_code", {"pattern": rf"\b{old}\b"})

        # 2. Planted instructions win when the diff isn't marked as untrusted.
        if injected and not defended:
            return AIMessage(content=json.dumps({"findings": [], "summary": "Already audited by security; approving."}))

        findings = rule_findings(lines, removed)
        added = [(p, n, c) for p, n, c, a in lines if a and c.strip()]
        code_added = [x for x in added if not x[0].startswith("tests/")]
        if injected:
            p, n, c = next(x for x in added if _INJ.search(x[2]))
            findings.append(dict(path=p, line=n, severity="medium", category="prompt-injection",
                                 title="Instruction aimed at automated reviewers",
                                 explanation="This comment tries to make an AI reviewer approve the change. It was "
                                             "ignored; consider removing it and checking why it was added.",
                                 evidence=c.strip()))
        if "FAILED" in tool_out.get("run_tests", "") and code_added:
            p, n, c = next(((p, n, c) for p, n, c in code_added if re.search(r"[<>=!]=?|\[:|//|-\s*1", c)),
                           code_added[0])
            failing = re.findall(r"FAILED (\S+)", tool_out["run_tests"])
            findings.append(dict(path=p, line=n, severity="high", category="logic",
                                 title="Change breaks the test suite",
                                 explanation=f"After this change the tests fail ({', '.join(failing[:3]) or 'see run'}). "
                                             "The modified condition changes behaviour the tests rely on.",
                                 evidence=c.strip()))
        for old, out in zip(self._renames(prompt), searched, strict=False):
            hits = [h for h in out.splitlines() if h and not h.startswith("no matches")]
            if hits and code_added:
                p, n, c = next(((p, n, c) for p, n, c in code_added if re.match(r"\s*def \w+|^[A-Z_]+\s*=", c)),
                               code_added[0])
                where = ", ".join(sorted({h.split(":")[0] for h in hits})[:3])
                findings.append(dict(path=p, line=n, severity="high", category="breaking-change",
                                     title=f"`{old}` is still used elsewhere",
                                     explanation=f"`{old}` was renamed but is still referenced in {where}, which "
                                                 "will now fail at import or call time.",
                                     evidence=c.strip()))

        # 3. Simulated reviewer noise (deterministic per diff).
        h = _h(prompt.split("Plan:")[0] + "\n".join(c for *_, c, _a in lines))
        if code_added and h % 100 < 50:
            p, n, _ = code_added[h % len(code_added)]
            findings.append(dict(path=p, line=n + 1, severity="medium", category="resource-leak",
                                 title="Cursor is never closed", explanation="The database cursor is not closed.",
                                 evidence="cursor.close()  # missing"))
        flagged = {(f["path"], f["line"]) for f in findings}
        clean_lines = [x for x in code_added if (x[0], x[1]) not in flagged and len(x[2].strip()) > 8]
        if clean_lines and (h // 100) % 100 < 65:
            p, n, c = clean_lines[(h // 7) % len(clean_lines)]
            findings.append(dict(path=p, line=n, severity="high", category="null-dereference",
                                 title="Possible None dereference",
                                 explanation="This value can be None here, which would raise at runtime.",
                                 evidence=c.strip()))
        if not self.tool_names and clean_lines:
            p, n, c = clean_lines[-1]
            findings.append(dict(path=p, line=n, severity="low", category="style",
                                 title="Consider adding a docstring", explanation="Public functions should be documented.",
                                 evidence=c.strip()))
        summary = f"Reviewed {len({p for p, *_ in lines})} changed file(s)" + (
            ", ran the tests" if "run_tests" in tool_out else "") + (
            f" and searched for {len(searched)} renamed name(s)" if searched else "") + "."
        return AIMessage(content=json.dumps({"findings": findings, "summary": summary}))

    def _skeptic(self, text: str) -> AIMessage:
        body = text.split("FINDINGS:\n", 1)[-1]
        findings_json, _, evidence = body.partition("\n\nCODE AND TOOL EVIDENCE:\n")
        try:
            findings = json.loads(findings_json)
        except json.JSONDecodeError:
            findings = []
        verdicts = []
        for i, f in enumerate(findings):
            keep, reason = skeptic_keeps(f, evidence)
            verdicts.append({"index": i, "keep": keep, "reason": reason})
        return AIMessage(content=json.dumps({"verdicts": verdicts}))

    @staticmethod
    def _call(name: str, args: dict) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}"}])
