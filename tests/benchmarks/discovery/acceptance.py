"""Acceptance criteria for discovery, judged on held-out apps (step 1g).

Agreed with the product owner on 2026-11-14. Held-out apps were never used
to tune the scanner and their ground truth was built without seeing its
output, so their numbers are the honest ones (``Split``).

1. No silent miss: a dependency missed while the caller claims complete
   coverage is the one thing an advisor must never do. Each app may list
   ``known_silent`` misses only as documented limitations.
2. Flags must be worth reading: at least ``MIN_CAUSE_PRECISION`` of the
   causes a coordinator is shown (services sharing unread files) point at a
   real missed dependency. Service-level precision is reported too.
3. Numbers are reported per app; a pooled number never hides a bad app.
"""

from __future__ import annotations

from typing import Final

from .models import BenchReport, Headline

MIN_CAUSE_PRECISION: Final[float] = 0.5


def _check(app: str, headline: Headline) -> list[str]:
    failures: list[str] = []
    if headline.missed_silent:
        failures.append(f"{app}: {headline.missed_silent} silent miss(es)")
    if headline.cause_precision is not None and headline.cause_precision < MIN_CAUSE_PRECISION:
        failures.append(
            f"{app}: only {headline.cause_precision:.0%} of flagged causes point at a real miss "
            f"(need {MIN_CAUSE_PRECISION:.0%})"
        )
    return failures


def acceptance_failures(report: BenchReport, held_out: set[str]) -> list[str]:
    """Why the held-out apps fail acceptance; empty when they pass."""
    if not held_out:
        return ["no held-out app: acceptance can't be judged"]
    return [f for app in sorted(held_out) if app in report.apps for f in _check(app, report.apps[app])]
