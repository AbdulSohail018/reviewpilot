"""The review agent as a LangGraph state machine.

    START -> scan -> planner? -> investigator <-> tools -> grounding? -> skeptic? -> finalize -> END

Guardrails: a tool-call limit and a token budget. When either is hit, the investigator is asked for its
final answer immediately instead of the run being cut off with nothing to show.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from reviewpilot import prompts
from reviewpilot.config import ReviewConfig
from reviewpilot.diff import FileDiff, render_for_prompt
from reviewpilot.guards import dedupe, ground, scan_injection
from reviewpilot.llm import get_model
from reviewpilot.models import SEVERITY_ORDER, Finding, Rejected, ReviewResult
from reviewpilot.tools import make_tools


class State(TypedDict, total=False):
    title: str
    body: str
    plan: list[str]
    messages: Annotated[list[AnyMessage], add_messages]
    raw_findings: list[dict]
    findings: list[Finding]
    rejected: list[Rejected]
    summary: str
    tool_calls: int
    tokens: int
    stop_reason: str
    injection_warnings: list[str]


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _json(content: Any) -> dict:
    text = _text(content)
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else (re.search(r"\{.*\}", text, re.S) or [None])[0]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _tokens(msg: AIMessage) -> int:
    usage = getattr(msg, "usage_metadata", None) or {}
    return int(usage.get("total_tokens", 0)) or len(_text(msg.content)) // 4


def build_review_graph(cfg: ReviewConfig, repo: Path, files: list[FileDiff]):
    files = [f for f in files if not any(fnmatch.fnmatch(f.path, pat) for pat in cfg.ignore_paths)]
    llm = get_model(cfg)
    tools = make_tools(repo, cfg.test_command, cfg.lint_command) if cfg.use_tools else []
    tools_by_name = {t.name: t for t in tools}
    investigator_llm = llm.bind_tools(tools) if tools else llm
    diff_view = render_for_prompt(files)

    def file_lines(path: str) -> list[str] | None:
        p = (repo / path)
        try:
            return p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else None
        except OSError:
            return None

    # ---------------------------------------------------------------------------------------- nodes
    def scan(state: State) -> dict:
        hits = scan_injection(files)
        return {"injection_warnings": [f"{p}:{n}: {t[:120]}" for p, n, t in hits], "tool_calls": 0, "tokens": 0}

    def planner(state: State) -> dict:
        resp = llm.invoke([SystemMessage(prompts.PLANNER),
                           HumanMessage(f"Title: {state['title']}\n\n{diff_view}")])
        steps = [str(s) for s in _json(resp.content).get("steps", [])][:5]
        return {"plan": steps, "tokens": state.get("tokens", 0) + _tokens(resp)}

    def investigator(state: State) -> dict:
        new: list[AnyMessage] = []
        if not state.get("messages"):
            system = prompts.INVESTIGATOR + ("\n\n" + prompts.INJECTION_RULE if cfg.injection_defense else "")
            new = [SystemMessage(system), HumanMessage(prompts.investigator_input(
                state["title"], state.get("body", ""), diff_view, state.get("plan", []), cfg.injection_defense))]
        history = list(state.get("messages", [])) + new
        over_budget = state.get("tool_calls", 0) >= cfg.max_tool_calls or state.get("tokens", 0) > cfg.token_budget
        if over_budget and tools:
            history.append(HumanMessage("Tool budget reached. Reply now with your final JSON."))
            new.append(history[-1])
            resp = llm.invoke(history)
        else:
            resp = investigator_llm.invoke(history)
        update: dict = {"messages": new + [resp], "tokens": state.get("tokens", 0) + _tokens(resp)}
        if over_budget:
            update["stop_reason"] = "tool_budget" if state.get("tool_calls", 0) >= cfg.max_tool_calls else "token_budget"
        if not getattr(resp, "tool_calls", None):
            data = _json(resp.content)
            update["raw_findings"] = data.get("findings", []) if isinstance(data.get("findings"), list) else []
            update["summary"] = str(data.get("summary", ""))
        return update

    def run_tools(state: State) -> dict:
        last = state["messages"][-1]
        out = []
        for call in last.tool_calls:
            t = tools_by_name.get(call["name"])
            try:
                result = t.invoke(call["args"]) if t else f"ERROR: unknown tool {call['name']}"
            except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
                result = f"ERROR: {exc}"
            out.append(ToolMessage(content=str(result), tool_call_id=call["id"], name=call["name"]))
        return {"messages": out, "tool_calls": state.get("tool_calls", 0) + len(out)}

    def parse(state: State) -> dict:
        findings = []
        for raw in state.get("raw_findings", []):
            try:
                f = Finding(**{**raw, "line": int(raw.get("line", 0) or 0)})
            except (ValidationError, TypeError, ValueError):
                continue
            findings.append(f)
        return {"findings": dedupe(findings), "rejected": []}

    def grounding(state: State) -> dict:
        texts = {f.path: file_lines(f.path) for f in state["findings"]}
        kept, rejected = ground(state["findings"], files, {k: v for k, v in texts.items() if v is not None})
        return {"findings": kept, "rejected": state.get("rejected", []) + rejected}

    def skeptic(state: State) -> dict:
        findings = state["findings"]
        if not findings:
            return {}
        evidence_parts = []
        for f in findings:
            lines = file_lines(f.path) or []
            lo, hi = max(1, f.line - 6), min(len(lines), f.line + 6)
            snippet = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(lo, hi + 1))
            evidence_parts.append(f"--- {f.path} lines {lo}-{hi}\n{snippet}")
        for m in state.get("messages", []):
            if isinstance(m, ToolMessage):
                evidence_parts.append(f"--- tool {m.name}\n{_text(m.content)[:3000]}")
        payload = json.dumps([f.model_dump(exclude={"source"}) for f in findings], indent=1)
        resp = llm.invoke([SystemMessage(prompts.SKEPTIC),
                           HumanMessage(prompts.skeptic_input(payload, "\n".join(evidence_parts)))])
        verdicts = {int(v.get("index", -1)): v for v in _json(resp.content).get("verdicts", []) if isinstance(v, dict)}
        kept, rejected = [], list(state.get("rejected", []))
        for i, f in enumerate(findings):
            v = verdicts.get(i, {"keep": True})
            if v.get("keep", True):
                kept.append(f)
            else:
                rejected.append(Rejected(finding=f, stage="skeptic", reason=str(v.get("reason", ""))))
        return {"findings": kept, "rejected": rejected, "tokens": state.get("tokens", 0) + _tokens(resp)}

    def finalize(state: State) -> dict:
        floor = SEVERITY_ORDER.get(cfg.min_severity, 3)
        findings = [f for f in state.get("findings", []) if SEVERITY_ORDER[f.severity] <= floor]
        findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.path, f.line))
        findings = findings[: cfg.max_findings]
        dropped = len(state.get("rejected", []))
        tail = (f" {len(findings)} issue(s) reported" + (f"; {dropped} unverified comment(s) dropped." if dropped else "."))
        return {"findings": findings, "summary": (state.get("summary", "") + tail).strip()}

    # ---------------------------------------------------------------------------------------- wiring
    def after_investigator(state: State) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else "parse"

    g = StateGraph(State)
    g.add_node("scan", scan)
    g.add_node("planner", planner)
    g.add_node("investigator", investigator)
    g.add_node("tools", run_tools)
    g.add_node("parse", parse)
    g.add_node("grounding", grounding)
    g.add_node("skeptic", skeptic)
    g.add_node("finalize", finalize)

    g.add_edge(START, "scan")
    g.add_edge("scan", "planner" if cfg.use_planner else "investigator")
    g.add_edge("planner", "investigator")
    g.add_conditional_edges("investigator", after_investigator, ["tools", "parse"])
    g.add_edge("tools", "investigator")
    after_parse = "grounding" if cfg.use_grounding else ("skeptic" if cfg.use_skeptic else "finalize")
    g.add_edge("parse", after_parse)
    g.add_edge("grounding", "skeptic" if cfg.use_skeptic else "finalize")
    g.add_edge("skeptic", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def review(cfg: ReviewConfig, repo: Path, files: list[FileDiff], title: str, body: str = "") -> ReviewResult:
    graph = build_review_graph(cfg, repo, files)
    out = graph.invoke({"title": title, "body": body or ""}, {"recursion_limit": 4 * cfg.max_tool_calls + 30})
    return ReviewResult(
        summary=out.get("summary", ""), findings=out.get("findings", []), rejected=out.get("rejected", []),
        plan=out.get("plan", []), tool_calls=out.get("tool_calls", 0), tokens=out.get("tokens", 0),
        cost_usd=cfg.cost(out.get("tokens", 0)), stop_reason=out.get("stop_reason", ""),
        injection_warnings=out.get("injection_warnings", []),
    )
