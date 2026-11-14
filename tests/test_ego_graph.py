"""Tests for the dependency map of a service or a release (T4c)."""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src import api, webhooks
from src.coordinator import nodes as coordinator_nodes
from src.core.models import (
    AssessmentEvidence,
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    ReasonCode,
    ReleaseAssessment,
    ServiceNode,
)
from src.core.service_ids import unresolved_entity
from src.orchestration import ego_graph
from src.orchestration.ego_graph import build_ego_graph
from src.tools.baseline_store import BaselineScope, StoredBaseline

URN = "urn:release-coord:acme:prod:aks-1:shop"
T0 = datetime(2026, 11, 14, tzinfo=timezone.utc)
STRIPE = unresolved_entity("api.stripe.com")
UNSET = unresolved_entity("env:orders/PAYMENT_URL")


def _id(name: str) -> str:
    return f"{URN}:{name}"


def _edge(source: str, target: str, method: DiscoveryMethod = DiscoveryMethod.STATIC_CODE) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source, target_service_id=target, discovery_methods=[method],
        confidence_score=1.0, last_observed_at=T0,
    )


# mobile -> web -> orders -> db ; orders -> stripe ; orders -> $PAYMENT_URL ; db -> orders (cycle)
GRAPH = KnowledgeGraph(
    services=[ServiceNode(service_id=_id(n), name=n) for n in ("mobile", "web", "orders", "db", "far")],
    edges=[
        _edge(_id("mobile"), _id("web")),
        _edge(_id("web"), _id("orders"), DiscoveryMethod.RUNTIME_TELEMETRY),
        _edge(_id("web"), _id("orders")),
        _edge(_id("orders"), _id("db")),
        _edge(_id("orders"), STRIPE.entity_id),
        _edge(_id("orders"), UNSET.entity_id),
        _edge(_id("db"), _id("orders")),
        _edge(_id("far"), _id("mobile")),
    ],
    unresolved_entities=[STRIPE, UNSET],
)


class EgoGraphTests(unittest.TestCase):
    def test_columns_by_hop_and_direction(self) -> None:
        graph = build_ego_graph(GRAPH, [_id("orders")], depth=2)
        columns = {node.label: node.column for node in graph.nodes}
        # db both calls and is called: the nearer side wins, callers on a tie.
        self.assertEqual(columns, {"orders": 0, "web": -1, "db": -1, "mobile": -2, "api.stripe.com": 1, "$payment_url": 1})
        self.assertNotIn("far", columns)  # three hops away
        kinds = {node.label: node.kind for node in graph.nodes}
        self.assertEqual(kinds["api.stripe.com"], "unresolved")
        self.assertTrue(next(n for n in graph.nodes if n.label == "orders").center)

    def test_edges_are_merged_and_marked_runtime(self) -> None:
        graph = build_ego_graph(GRAPH, [_id("orders")], depth=1)
        web = next(e for e in graph.edges if e.source == _id("web"))
        self.assertEqual((web.methods, web.runtime), (["RUNTIME_TELEMETRY", "STATIC_CODE"], True))
        self.assertNotIn(_id("mobile"), {n.id for n in graph.nodes})

    def test_large_neighbourhoods_are_cut_farthest_first(self) -> None:
        with patch.object(ego_graph, "MAX_NODES", 3):
            graph = build_ego_graph(GRAPH, [_id("orders")], depth=2)
        self.assertEqual(len(graph.nodes), 3)
        self.assertEqual(graph.left_out, 3)
        self.assertNotIn(_id("mobile"), {n.id for n in graph.nodes})

    def test_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            build_ego_graph(GRAPH, [_id("nope")])
        with self.assertRaises(ValueError):
            build_ego_graph(GRAPH, [_id("orders")], depth=4)


class EgoGraphApiTests(unittest.TestCase):
    def setUp(self) -> None:
        store = MagicMock()
        baseline = StoredBaseline(scope=BaselineScope(tenant="acme", project_id="shop"), snapshot_id="snap-1", graph=GRAPH)
        store.latest.return_value = baseline
        store.read.return_value = baseline
        assessment = ReleaseAssessment(
            approved=False, actionable=False, reason_codes=[ReasonCode.COMPATIBILITY_UNVERIFIED],
            deployment_sequence=[_id("orders")], services_to_deploy=[_id("orders")], justification="x",
            evidence=AssessmentEvidence(pr_id="PR-1", baseline_snapshot_id="snap-1"),
        )
        jobs = {"job-1": webhooks.PrAssessmentJob(job_id="job-1", pr_id="PR-1", status="completed",
                                                  tenant="acme", project_id="shop", assessment=assessment)}
        for patcher in (
            patch.object(coordinator_nodes, "BASELINE_STORE", store),
            patch.dict(webhooks._assessment_jobs, jobs, clear=True),
            patch.object(webhooks, "ASSESSMENT_ARCHIVE", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = store
        self.client = TestClient(api.app)

    def test_service_map(self) -> None:
        response = self.client.get("/api/v1/projects/acme/shop/inventory/graph", params={"service": _id("web"), "depth": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["centers"], [_id("web")])
        self.assertEqual(self.client.get("/api/v1/projects/acme/shop/inventory/graph", params={"service": "x"}).status_code, 404)
        self.assertEqual(
            self.client.get("/api/v1/projects/acme/shop/inventory/graph", params={"service": _id("web"), "depth": 9}).status_code,
            422,
        )

    def test_assessment_map_uses_its_pinned_baseline(self) -> None:
        response = self.client.get("/api/v1/webhooks/pr-assessment/job-1/graph")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["centers"], [_id("orders")])
        self.store.read.assert_called_once_with(BaselineScope(tenant="acme", project_id="shop"), "snap-1")
        self.assertEqual(self.client.get("/api/v1/webhooks/pr-assessment/nope/graph").status_code, 404)


if __name__ == "__main__":
    unittest.main()
