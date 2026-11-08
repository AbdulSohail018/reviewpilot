"""Seeded-bug benchmark: how accurate are ReviewPilot's comments, and what does each harness component buy?

    python -m evals.run_evals                         # offline mock reviewer (CI)
    python -m evals.run_evals --provider anthropic    # real model (needs ANTHROPIC_API_KEY)

For each case a throwaway git repo is built from evals/sample_project, the PR is applied on a branch, and the
agent reviews `main...pr`. A comment counts as correct if it is within one line of a planted bug AND names
the right kind of problem (keyword families in FAMILIES); flagging planted reviewer-injection text is accepted
but not required. Precision = correct comments / all comments. Recall = planted bugs found / planted bugs.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evals.cases import CASES, Case
from reviewpilot.config import PRESETS, ReviewConfig
from reviewpilot.diff import local_diff, parse_unified
from reviewpilot.graph import review

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "sample_project"
TEST_COMMAND = "python -m pytest -q -p no:cacheprovider"
ORDER = list(PRESETS)

# A comment only counts if it is on the right line AND names the right kind of problem. Models word categories
# differently, so each expected category accepts a family of keywords, matched against category and title.
FAMILIES = {
    "sql-injection": ["sql", "injection"],
    "hardcoded-secret": ["secret", "credential", "api key", "api_key", "hardcoded", "password", "token"],
    "code-injection": ["eval", "code-injection", "code injection", "arbitrary code", "exec"],
    "command-injection": ["shell", "command", "injection", "subprocess"],
    "mutable-default": ["mutable", "default"],
    "swallowed-exception": ["exception", "swallow", "error", "except", "silent"],
    "resource-leak": ["leak", "close", "resource", "handle", "file"],
    "division-by-zero": ["zero", "division", "divide", "empty"],
    "off-by-one": ["off-by-one", "off by one", "last", "drop", "slice", "boundary", "logic", "bug"],
    "logic": ["logic", "bug", "incorrect", "wrong", "off-by-one", "boundary", "comparison", "regression",
              "test", "behavio", "threshold", "sort", "order", "fail", "division", "rounding", "integer", "truncat"],
    "breaking-change": ["break", "rename", "caller", "reference", "import", "still used", "undefined", "api"],
    "prompt-injection": ["injection", "instruction", "prompt", "reviewer"],
}


def matches(f, path: str, line: int, category: str) -> bool:
    text = f"{f.category} {f.title}".lower()
    return f.path == path and abs(f.line - line) <= 1 and any(k in text for k in FAMILIES.get(category, [category]))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@example.com",
                        "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@example.com",
                        "PATH": "/usr/bin:/bin:/usr/local/bin"})


def build_repo(case: Case, dest: Path) -> Path:
    shutil.copytree(SAMPLE, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "base")
    _git(dest, "checkout", "-q", "-b", "pr")
    for e in case.edits:
        path = dest / e.path
        if e.old is None:
            path.write_text(e.new)
            continue
        text = path.read_text()
        if e.old not in text:
            raise ValueError(f"{case.id}: edit target not found in {e.path}: {e.old[:60]!r}")
        path.write_text(text.replace(e.old, e.new, 1))
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", case.title)
    return dest


def expected_lines(case: Case, repo: Path, which: str = "expected") -> list[tuple[str, int, str]]:
    out = []
    for exp in getattr(case, which):
        lines = (repo / exp.path).read_text().splitlines()
        n = next(i for i, line in enumerate(lines, 1) if exp.line_contains in line)
        out.append((exp.path, n, exp.category))
    return out


def run_case(case: Case, preset: str, provider: str, model: str | None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repo(case, Path(tmp) / "repo")
        files = parse_unified(local_diff(repo, "main", "pr"))
        cfg = ReviewConfig(provider=provider, model=model, test_command=TEST_COMMAND, **PRESETS[preset])
        result = review(cfg, repo, files, case.title)
        expected = expected_lines(case, repo)
        acceptable = expected_lines(case, repo, "acceptable")

    def hit(f, e):
        return matches(f, *e)

    tp = sum(any(hit(f, e) for e in expected) for f in result.findings)
    ok_extra = sum(not any(hit(f, e) for e in expected) and any(hit(f, e) for e in acceptable)
                   for f in result.findings)
    found = sum(any(hit(f, e) for f in result.findings) for e in expected)
    return {
        "case": case.id, "title": case.title, "found_by": case.found_by, "preset": preset,
        "findings": len(result.findings) - ok_extra, "true_positives": tp,
        "false_positives": len(result.findings) - tp - ok_extra, "acceptable_extras": ok_extra,
        "bugs": len(expected), "bugs_found": found,
        "dropped_grounding": sum(r.stage == "grounding" for r in result.rejected),
        "dropped_skeptic": sum(r.stage == "skeptic" for r in result.rejected),
        "tool_calls": result.tool_calls, "tokens": result.tokens, "cost_usd": result.cost_usd,
        "comments": [f"{f.path}:{f.line} [{f.category}] {f.title}" for f in result.findings],
    }


def summarise(rows: list[dict]) -> dict:
    out = {}
    for preset in ORDER:
        r = [x for x in rows if x["preset"] == preset]
        if not r:
            continue
        findings = sum(x["findings"] for x in r)
        tp = sum(x["true_positives"] for x in r)
        bugs = sum(x["bugs"] for x in r)
        found = sum(x["bugs_found"] for x in r)
        clean = [x for x in r if x["found_by"] == "clean"]
        p = tp / findings if findings else 0.0
        rc = found / bugs if bugs else 0.0
        by = {}
        for kind in ("diff", "tests", "context", "reasoning"):
            k = [x for x in r if x["found_by"] == kind]
            if k:
                by[kind] = f"{sum(x['bugs_found'] for x in k)}/{sum(x['bugs'] for x in k)}"
        out[preset] = {
            "precision": round(p, 3), "recall": round(rc, 3),
            "f1": round(2 * p * rc / (p + rc), 3) if p + rc else 0.0,
            "comments": findings, "false_positives": findings - tp,
            "clean_prs_with_false_alarms": f"{sum(x['findings'] > 0 for x in clean)}/{len(clean)}",
            "recall_by_kind": by,
            "avg_tool_calls": round(sum(x["tool_calls"] for x in r) / len(r), 1),
            "avg_tokens": round(sum(x["tokens"] for x in r) / len(r)),
            "total_cost_usd": round(sum(x["cost_usd"] for x in r), 3),
        }
    return out


def chart(summary: dict, provider: str, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    presets = [p for p in ORDER if p in summary]
    labels = {"single_pass": "Single pass", "tools": "+ tools", "tools_grounded": "+ grounding", "full": "+ skeptic"}
    fig, ax = plt.subplots(figsize=(9, 4.2))
    width = 0.38
    xs = range(len(presets))
    for offset, metric, colour in ((-width / 2, "precision", "#2563EB"), (width / 2, "recall", "#16A34A")):
        vals = [summary[p][metric] * 100 for p in presets]
        bars = ax.bar([x + offset for x in xs], vals, width, label=metric.capitalize(), color=colour)
        ax.bar_label(bars, labels=[f"{v:.0f}%" for v in vals], padding=3, fontsize=10)
    ax.set_xticks(list(xs), [labels[p] for p in presets])
    ax.set_ylim(0, 112)
    ax.set_ylabel("%")
    ax.set_title(f"ReviewPilot on 24 seeded pull requests ({'offline mock reviewer' if provider == 'mock' else provider}): each step adds to the last")
    ax.legend(frameon=False, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="mock", choices=["mock", "anthropic", "openai"])
    parser.add_argument("--model")
    parser.add_argument("--presets", nargs="+", default=ORDER, choices=ORDER)
    parser.add_argument("--cases", nargs="+", help="case ids to run (default: all)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", default=str(ROOT / "results"))
    args = parser.parse_args()

    cases = [c for c in CASES if not args.cases or c.id in args.cases]
    jobs = [(c, p) for p in args.presets for c in cases]
    print(f"{len(cases)} cases x {len(args.presets)} configs, provider={args.provider}")
    with ThreadPoolExecutor(args.concurrency) as pool:
        rows = list(pool.map(lambda j: run_case(j[0], j[1], args.provider, args.model), jobs))

    summary = summarise(rows)
    out = Path(args.out) / args.provider
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(rows, indent=2))
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    chart(summary, args.provider, out / "chart.png")
    for preset, s in summary.items():
        print(f"{preset:<15} precision {s['precision']:.0%}  recall {s['recall']:.0%}  F1 {s['f1']:.2f}  "
              f"false positives {s['false_positives']:>2}  clean PRs flagged {s['clean_prs_with_false_alarms']}  "
              f"by kind {s['recall_by_kind']}")
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
