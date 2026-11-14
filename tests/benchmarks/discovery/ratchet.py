"""Compare a benchmark run with the committed baseline.

The ratchet fails when, pooled or for any app, silent misses rise or a
precision/recall figure drops by more than ``TOLERANCE``. Improvements are
accepted by re-writing the baseline in the same pull request.
"""

from __future__ import annotations

from .models import BenchReport, Headline

TOLERANCE = 0.02
_RATES = (
    "inventory_precision",
    "inventory_recall",
    "static_precision",
    "static_recall",
    "manifest_precision",
    "manifest_recall",
    "flag_precision",
    "cause_precision",
)


def _compare(scope: str, before: Headline, after: Headline) -> list[str]:
    problems: list[str] = []
    if after.missed_silent > before.missed_silent:
        problems.append(
            f"{scope}: silent misses rose {before.missed_silent} -> {after.missed_silent}"
        )
    for rate in _RATES:
        old, new = getattr(before, rate), getattr(after, rate)
        if new is None and rate.endswith("precision"):
            continue  # no predictions at all: nothing wrong (recall shows any loss)
        if old is not None and (new is None or new < old - TOLERANCE):
            problems.append(f"{scope}: {rate} dropped {old} -> {new}")
    return problems


def regressions(baseline: BenchReport, current: BenchReport) -> list[str]:
    """Human-readable regressions of ``current`` against ``baseline``."""
    problems = _compare("pooled", baseline.pooled, current.pooled)
    for app, before in sorted(baseline.apps.items()):
        after = current.apps.get(app)
        if after is None:
            problems.append(f"{app}: missing from this run")
        else:
            problems.extend(_compare(app, before, after))
    return problems
