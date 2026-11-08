"""Read-only repository tools for the investigator. Every path is confined to the checked-out repo."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from langchain_core.tools import BaseTool, tool

MAX_OUTPUT = 6_000


def _safe(repo: Path, rel: str) -> Path:
    target = (repo / rel).resolve()
    if target != repo and repo not in target.parents:
        raise ValueError("path is outside the repository")
    return target


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n... ({len(text) - MAX_OUTPUT} more characters)"


def make_tools(repo: Path, test_command: str | None = None, lint_command: str | None = None,
               timeout: int = 180) -> list[BaseTool]:
    repo = repo.resolve()

    @tool
    def read_file(path: str, start: int = 1, end: int = 200) -> str:
        """Read lines start..end (1-based, inclusive) of a file in the repository, with line numbers."""
        try:
            p = _safe(repo, path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not p.is_file():
            return f"ERROR: {path} not found"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = max(start, 1), min(end, len(lines))
        return _clip("\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1)) or "(empty file)")

    @tool
    def search_code(pattern: str, glob: str = "") -> str:
        """Search the repository for a regular expression (ripgrep). Returns file:line:text matches.
        Use it to find callers of a changed function or uses of a renamed name."""
        cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "-e", pattern]
        if glob:
            cmd += ["--glob", glob]
        try:
            out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=30).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            out = subprocess.run(["git", "grep", "-n", "-E", pattern], cwd=repo, capture_output=True, text=True).stdout
        return _clip(out.strip() or "no matches")

    @tool
    def list_files(directory: str = ".") -> str:
        """List tracked files under a directory of the repository."""
        try:
            _safe(repo, directory)
        except ValueError as exc:
            return f"ERROR: {exc}"
        out = subprocess.run(["git", "ls-files", directory], cwd=repo, capture_output=True, text=True).stdout
        return _clip(out.strip() or "no files")

    @tool
    def git_log(path: str = "", count: int = 5) -> str:
        """Recent commit history, optionally for one file."""
        cmd = ["git", "log", f"-{min(count, 20)}", "--format=%h %ad %s", "--date=short"]
        if path:
            cmd += ["--", path]
        return _clip(subprocess.run(cmd, cwd=repo, capture_output=True, text=True).stdout.strip() or "no history")

    def _run(command: str) -> str:
        # Commands come from the repo's .reviewpilot.yml, never from the model, and run without secrets.
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        try:
            proc = subprocess.run(shlex.split(command), cwd=repo, capture_output=True, text=True,
                                  timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return f"TIMEOUT after {timeout}s"
        except FileNotFoundError as exc:
            return f"ERROR: {exc}"
        status = "PASSED" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
        return _clip(f"{status}\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}".strip())

    tools: list[BaseTool] = [read_file, search_code, list_files, git_log]
    if test_command:
        @tool
        def run_tests() -> str:
            """Run the project's configured test suite and return the result summary."""
            return _run(test_command)
        tools.append(run_tests)
    if lint_command:
        @tool
        def run_linter() -> str:
            """Run the project's configured linter."""
            return _run(lint_command)
        tools.append(run_linter)
    return tools
