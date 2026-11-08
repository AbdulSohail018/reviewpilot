"""Data shapes shared across the agent."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low"]
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class Finding(BaseModel):
    """One review comment. `evidence` must quote the code it is about, so the harness can check it."""

    path: str
    line: int = Field(description="Line number in the new version of the file")
    severity: Severity = "medium"
    category: str = Field(description="Short kebab-case label, e.g. sql-injection, logic, resource-leak")
    title: str
    explanation: str
    evidence: str = Field(description="Exact code copied from the file at `line`")
    suggestion: str = ""
    source: str = Field(default="model", description="Which step produced it: model, tests, guard")


class Rejected(BaseModel):
    finding: Finding
    stage: Literal["grounding", "skeptic"]
    reason: str


class ReviewResult(BaseModel):
    summary: str
    findings: list[Finding]
    rejected: list[Rejected] = []
    plan: list[str] = []
    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""
    injection_warnings: list[str] = []

    def verdict(self) -> str:
        if any(f.severity in ("critical", "high") for f in self.findings):
            return "changes requested"
        return "comments" if self.findings else "no issues found"
