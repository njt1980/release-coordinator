"""Tests for the suggested test scope by hop distance (T4b)."""

import unittest
from datetime import datetime, timezone

from src.coordinator.nodes import traverse_blast_radius
from src.coordinator.test_scope import suggested_test_scope
from src.core.models import (
    AssessmentEvidence,
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    ReasonCode,
    ReleaseAssessment,
    ServiceNode,
)
from src.orchestration.briefing import build_briefing, render_markdown

URN = "urn:release-coord:acme:prod:aks-1:shop"
T0 = datetime(2026, 11, 14, tzinfo=timezone.utc)


def _id(name: str) -> str:
    return f"{URN}:{name}"


def _edge(source: str, target: str) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=_id(source), target_service_id=_id(target),
        discovery_methods=[DiscoveryMethod.STATIC_CODE], confidence_score=1.0, last_observed_at=T0,
    )


# web -> gateway -> orders -> db ; admin -> orders ; mobile -> web ; orders -> queue
NAMES = ["web", "gateway", "orders", "db", "admin", "mobile", "queue"]
GRAPH = KnowledgeGraph(
    services=[ServiceNode(service_id=_id(n), name=n) for n in NAMES],
    edges=[
        _edge("web", "gateway"), _edge("gateway", "orders"), _edge("orders", "db"),
        _edge("admin", "orders"), _edge("mobile", "web"), _edge("orders", "queue"),
        _edge("admin", "gateway"),
    ],
)


class TestScopeTests(unittest.TestCase):
    def test_radius_records_shortest_hops(self) -> None:
        radius = traverse_blast_radius(GRAPH, [_id("orders")])
        self.assertEqual(
            radius.upstream_hops,
            {_id("gateway"): 1, _id("admin"): 1, _id("web"): 2, _id("mobile"): 3},
        )
        self.assertEqual(radius.downstream_hops, {_id("db"): 1, _id("queue"): 1})

    def test_tiers_by_distance(self) -> None:
        radius = traverse_blast_radius(GRAPH, [_id("orders")])
        tiers = suggested_test_scope([_id("orders")], radius.upstream_hops, radius.downstream_hops)
        self.assertEqual(
            [(t.tier, t.title, t.services) for t in tiers],
            [
                (1, "Changed services", [_id("orders")]),
                (2, "Direct callers", [_id("admin"), _id("gateway")]),
                (3, "Callers two hops away", [_id("web")]),
                (4, "Further callers and called services", [_id("mobile"), _id("db"), _id("queue")]),
            ],
        )

    def test_empty_tiers_are_left_out_and_numbering_closes_up(self) -> None:
        tiers = suggested_test_scope([_id("db")], {_id("orders"): 1, _id("web"): 3}, {})
        self.assertEqual([(t.tier, t.title) for t in tiers], [
            (1, "Changed services"), (2, "Direct callers"), (3, "Further callers and called services"),
        ])

    def test_a_changed_service_is_never_listed_twice(self) -> None:
        # Two changed services that call each other: neither appears as a caller.
        tiers = suggested_test_scope([_id("a"), _id("b")], {_id("a"): 1, _id("c"): 1}, {_id("b"): 1})
        listed = [service for tier in tiers for service in tier.services]
        self.assertEqual(sorted(listed), [_id("a"), _id("b"), _id("c")])

    def test_briefing_shows_short_names(self) -> None:
        radius = traverse_blast_radius(GRAPH, [_id("orders")])
        assessment = ReleaseAssessment(
            approved=False, actionable=False, reason_codes=[ReasonCode.COMPATIBILITY_UNVERIFIED],
            deployment_sequence=[_id("orders")], justification="x",
            evidence=AssessmentEvidence(
                pr_id="PR-1",
                test_scope=suggested_test_scope([_id("orders")], radius.upstream_hops, radius.downstream_hops),
            ),
        )
        briefing = build_briefing(assessment)
        self.assertEqual(briefing.test_scope[1].services, ["admin", "gateway"])
        self.assertIn("2. **Direct callers**: admin, gateway.", render_markdown(briefing))


if __name__ == "__main__":
    unittest.main()
