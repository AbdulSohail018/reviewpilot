"""ReviewPilot command line.

    python -m reviewpilot local --repo . --base main          # review your branch against main
    python -m reviewpilot github --pr 12                       # review a PR and post comments (in Actions)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from reviewpilot.config import PRESETS, ReviewConfig, load_repo_config
from reviewpilot.diff import local_diff, parse_unified
from reviewpilot.graph import review
from reviewpilot.render import review_body, split_inline, terminal


def _config(args) -> ReviewConfig:
    overrides = dict(PRESETS[args.preset])
    if args.provider:
        overrides["provider"] = args.provider
    if args.model:
        overrides["model"] = args.model
    return load_repo_config(Path(args.repo), ReviewConfig(**overrides))


def cmd_local(args) -> int:
    repo = Path(args.repo).resolve()
    cfg = _config(args)
    files = parse_unified(local_diff(repo, args.base, args.head))
    if not files:
        print("No changes against", args.base)
        return 0
    result = review(cfg, repo, files, title=args.title or f"Changes against {args.base}")
    print(result.model_dump_json(indent=2) if args.json else terminal(result))
    return 1 if args.fail_on_findings and result.verdict() == "changes requested" else 0


def cmd_github(args) -> int:
    from reviewpilot.github import GitHub

    slug = args.slug or os.environ["GITHUB_REPOSITORY"]
    number = args.pr or int(json.load(open(os.environ["GITHUB_EVENT_PATH"]))["pull_request"]["number"])
    cfg = _config(args)
    if cfg.provider != "mock" and not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")):
        print("::warning::No model API key set; falling back to the offline mock reviewer.")
        cfg = cfg.model_copy(update={"provider": "mock", "model": None})
    gh = GitHub(slug)
    pr = gh.pull(number)
    files = gh.files(number)
    result = review(cfg, Path(args.repo).resolve(), files, pr["title"], pr.get("body") or "")
    inline, body_only = split_inline(result, files)
    label = f"{cfg.provider}:{cfg.resolved_model()}" + (" (offline mock reviewer)" if cfg.provider == "mock" else "")
    posted = gh.post_review(number, pr["head"]["sha"], review_body(result, body_only, label), inline)
    print(f"Posted review {posted.get('id')} with {len(inline)} inline comment(s); verdict: {result.verdict()}")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(terminal(result).replace("\n", "  \n") + "\n")
    return 1 if args.fail_on_findings and result.verdict() == "changes requested" else 0


def main() -> None:
    p = argparse.ArgumentParser(prog="reviewpilot", description="AI code review with verified findings.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("local", "github"):
        s = sub.add_parser(name)
        s.add_argument("--repo", default=".")
        s.add_argument("--provider", choices=["mock", "anthropic", "openai"])
        s.add_argument("--model")
        s.add_argument("--preset", choices=list(PRESETS), default="full")
        s.add_argument("--fail-on-findings", action="store_true", help="exit 1 when high or critical issues are found")
    sub.choices["local"].add_argument("--base", default="main")
    sub.choices["local"].add_argument("--head", default="HEAD")
    sub.choices["local"].add_argument("--title")
    sub.choices["local"].add_argument("--json", action="store_true")
    sub.choices["github"].add_argument("--pr", type=int)
    sub.choices["github"].add_argument("--slug", help="owner/repo (defaults to GITHUB_REPOSITORY)")
    args = p.parse_args()
    sys.exit(cmd_local(args) if args.cmd == "local" else cmd_github(args))


if __name__ == "__main__":
    main()
