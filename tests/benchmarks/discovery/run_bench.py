"""Discovery-accuracy benchmark runner.

    python -m tests.benchmarks.discovery.run_bench [APP ...] [--offline]
        [--cache DIR] [--out DIR] [--update-baseline]

Fetches each app's pinned repositories (once, into the cache), runs the real
static scanner over them offline, scores the result against the app's
ground truth, and compares the pooled headline with ``baseline.json``.
Exit code 1 on a ratchet regression (see ``ratchet.py``). Not part of the
default ``unittest`` run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .checkouts import DEFAULT_CACHE, CheckoutError, ensure_checkout
from .env_sources import load_envs
from .models import AppResult, AppTruth, BenchReport, EdgeMode
from .offline_scan import scan_checkout
from .ratchet import regressions
from .scoring import headline, score_app

HERE = Path(__file__).resolve().parent
APPS_DIR = HERE / "apps"
BASELINE = HERE / "baseline.json"
PROJECT_ROOT = HERE.parents[2]
PROJECT_PREFIX = "@project/"


def load_truth(app: str) -> AppTruth:
    return AppTruth.model_validate_json((APPS_DIR / f"{app}.json").read_text(encoding="utf-8"))


def all_apps() -> list[str]:
    return sorted(path.stem for path in APPS_DIR.glob("*.json"))


def run_app(truth: AppTruth, cache: Path, offline: bool) -> AppResult:
    checkouts = {pin.name: ensure_checkout(pin, cache, offline) for pin in truth.repos}

    def resolve(path: str) -> Path:
        if path.startswith(PROJECT_PREFIX):
            return PROJECT_ROOT / path[len(PROJECT_PREFIX):]
        repo, _, rest = path.partition("/")
        if repo not in checkouts:
            raise ValueError(f"{truth.app}: env source {path!r} names no pinned repository")
        return checkouts[repo] / rest

    scans = [
        (pin.name, scan_checkout(pin.name, checkouts[pin.name], pin.sha)) for pin in truth.repos
    ]
    return score_app(truth, scans, load_envs(truth.env_sources, resolve))


def summary_line(result: AppResult) -> str:
    inv, misses = result.inventory, result.misses
    static, manifest = result.edges[EdgeMode.STATIC].all, result.edges[EdgeMode.MANIFEST].all
    return (
        f"{result.app:32} inventory P={inv.precision} R={inv.recall} | "
        f"static P={static.precision} R={static.recall} | "
        f"manifest P={manifest.precision} R={manifest.recall} | "
        f"missed flagged={misses.missed_flagged} silent={misses.missed_silent} "
        f"no-boundary={misses.missed_caller_has_no_boundary}"
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("apps", nargs="*", help="apps to run (default: all)")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--offline", action="store_true", help="never fetch; use the cache only")
    parser.add_argument("--out", type=Path, help="write per-app result JSON here")
    parser.add_argument("--update-baseline", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    logging.disable(logging.CRITICAL)  # the scanner logs every unread file
    apps = args.apps or all_apps()
    results: list[AppResult] = []
    for app in apps:
        try:
            result = run_app(load_truth(app), args.cache, args.offline)
        except CheckoutError as error:
            print(f"{app}: {error}", file=sys.stderr)
            return 2
        results.append(result)
        print(summary_line(result))
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / f"{app}.json").write_text(result.model_dump_json(indent=1), encoding="utf-8")
    report = BenchReport(apps={r.app: headline([r]) for r in results}, pooled=headline(results))
    print("pooled:", report.pooled.model_dump_json())
    if args.update_baseline:
        if args.apps:
            print("--update-baseline needs a full run (no app filter)", file=sys.stderr)
            return 2
        BASELINE.write_text(report.model_dump_json(indent=1) + "\n", encoding="utf-8")
        print(f"baseline written: {BASELINE}")
        return 0
    baseline = BenchReport.model_validate_json(BASELINE.read_text(encoding="utf-8"))
    if args.apps:
        baseline = BenchReport(
            apps={a: h for a, h in baseline.apps.items() if a in apps}, pooled=report.pooled
        )
    problems = regressions(baseline, report)
    for problem in problems:
        print("REGRESSION:", problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
