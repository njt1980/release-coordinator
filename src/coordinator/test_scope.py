"""Suggested test scope: the release radius split by distance from the change.

A pull request's risk reaches its callers through the contracts it changes:
a direct caller uses the changed interface, a caller's caller only sees the
change through it, and so on. Services the change calls are affected only if
its use of them changed. So the scope is tiered by hop distance in the
dependency graph:

1. the changed services;
2. their direct callers (they use what changed);
3. callers two hops away;
4. callers further away, and the services the change calls (integration
   checks).

It is a suggestion computed from the graph, never a verdict, and only as
complete as the graph (see the briefing's evidence gaps).
"""

from collections.abc import Collection, Mapping

from src.core.models import TestScopeTier


def _nearest(hops: Mapping[str, int], keep: "Collection[int] | None" = None, at_least: int | None = None) -> list[str]:
    return sorted(
        service
        for service, distance in hops.items()
        if (keep is None or distance in keep) and (at_least is None or distance >= at_least)
    )


def suggested_test_scope(
    targets: Collection[str],
    upstream_hops: Mapping[str, int],
    downstream_hops: Mapping[str, int],
) -> list[TestScopeTier]:
    """The tiers (empty ones left out), most important first; each service appears once."""
    changed = sorted(set(targets))
    taken = set(changed)

    def fresh(services: list[str]) -> list[str]:
        kept = [service for service in services if service not in taken]
        taken.update(kept)
        return kept

    tiers = [
        ("Changed services", "The pull request changes them: test their own behaviour and contracts.", changed),
        ("Direct callers", "They call a changed service, so they use what changed: test those calls.", fresh(_nearest(upstream_hops, {1}))),
        ("Callers two hops away", "They reach the change through a direct caller: test the paths that pass through it.", fresh(_nearest(upstream_hops, {2}))),
        (
            "Further callers and called services",
            "Further callers see the change only indirectly; services the change calls are affected only if "
            "its use of them changed: an integration or smoke check.",
            fresh([*_nearest(upstream_hops, at_least=3), *_nearest(downstream_hops)]),
        ),
    ]
    numbered: list[TestScopeTier] = []
    for title, why, services in tiers:
        if services:
            numbered.append(TestScopeTier(tier=len(numbered) + 1, title=title, why=why, services=services))
    return numbered
