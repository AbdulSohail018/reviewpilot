"""Run configuration. Every harness component can be switched off, so the benchmark can measure each one."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Provider = Literal["mock", "anthropic", "openai"]

DEFAULT_MODELS = {
    "mock": "mock-reviewer-v1",
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
    "openai": os.getenv("OPENAI_MODEL", "gpt-4.1"),
}
# Blended USD per million tokens, for cost estimates. Override with REVIEWPILOT_COST_PER_MTOK.
COST_PER_MTOK = {"mock": 0.0, "anthropic": 6.0, "openai": 4.0}


class ReviewConfig(BaseModel):
    provider: Provider = Field(default_factory=lambda: os.getenv("REVIEWPILOT_PROVIDER", "mock"))  # type: ignore[arg-type]
    model: str | None = None

    use_planner: bool = True
    use_tools: bool = True
    use_grounding: bool = True
    use_skeptic: bool = True
    injection_defense: bool = True

    max_tool_calls: int = Field(default=12, ge=0, le=60)
    token_budget: int = Field(default=150_000, ge=5_000)
    max_findings: int = 15
    min_severity: str = "low"

    # From the reviewed repo's .reviewpilot.toml, never from the model.
    test_command: str | None = None
    lint_command: str | None = None
    ignore_paths: list[str] = ["*.lock", "*.min.js", "dist/*", "*.ipynb"]

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS[self.provider]

    def cost(self, tokens: int) -> float:
        rate = float(os.getenv("REVIEWPILOT_COST_PER_MTOK", COST_PER_MTOK[self.provider]))
        return round(tokens / 1_000_000 * rate, 4)


PRESETS: dict[str, dict] = {
    # One prompt with the diff, no tools, no checks: what most "AI review" scripts do.
    "single_pass": {"use_planner": False, "use_tools": False, "use_grounding": False, "use_skeptic": False,
                    "injection_defense": False},
    "tools": {"use_planner": True, "use_tools": True, "use_grounding": False, "use_skeptic": False},
    "tools_grounded": {"use_planner": True, "use_tools": True, "use_grounding": True, "use_skeptic": False},
    "full": {},
}


def load_repo_config(repo: Path, base: ReviewConfig | None = None) -> ReviewConfig:
    """Merge settings from <repo>/.reviewpilot.toml, e.g.

        test_command = "pytest -q"
        lint_command = "ruff check ."
        ignore_paths = ["docs/*"]
    """
    cfg = base or ReviewConfig()
    path = repo / ".reviewpilot.toml"
    if path.exists():
        data = tomllib.loads(path.read_text())
        allowed = {"test_command", "lint_command", "ignore_paths", "max_findings", "min_severity", "max_tool_calls"}
        cfg = cfg.model_copy(update={k: v for k, v in data.items() if k in allowed})
    return cfg
