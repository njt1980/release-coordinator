"""Tests for canonical service IDs and their use during reconciliation."""

import unittest
from datetime import datetime, timezone
from typing import Any

from src.core.models import DependencyEdge, DiscoveryMethod, ServiceNode
from src.core.service_ids import build_name_index, canonical_service_id
from src.discovery_engine import nodes as discovery_nodes


def _edge(source: str, target: str, method: DiscoveryMethod) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[method],
        confidence_score=0.7,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class CanonicalServiceIdTests(unittest.TestCase):
    def test_resolution_rules(self) -> None:
        index = build_name_index(["default/order-service", "shop/rabbitmq"])
        for raw, default_ns, expected in [
            ("default/order-service", None, "default/order-service"),
            ("order-service", None, "default/order-service"),
            ("order-service:3000", None, "default/order-service"),
            ("rabbitmq", "default", "shop/rabbitmq"),
            ("ai-service", "default", "default/ai-service"),
            ("ai-service", None, "ai-service"),
            ("product-service.prod.svc.cluster.local", None, "prod/product-service"),
            ("api.example.com", "default", "api.example.com"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(
                    canonical_service_id(raw, index, default_ns), expected
                )

    def test_ambiguous_names_are_not_indexed(self) -> None:
        index = build_name_index(["a/web", "b/web", "a/api"])
        self.assertEqual(index, {"api": "a/api"})


class ReconcileCanonicalizationTests(unittest.TestCase):
    def test_static_and_k8s_evidence_share_namespaced_ids(self) -> None:
        state: Any = {
            "target_namespaces": ["default"],
            "discovered_services": [
                ServiceNode(
                    service_id="store-front",
                    name="store-front",
                    metadata={"repository": "repo-a"},
                ),
                ServiceNode(service_id="default/store-front", name="store-front"),
                ServiceNode(service_id="default/order-service", name="order-service"),
            ],
            "static_edges": [
                _edge("store-front", "order-service", DiscoveryMethod.STATIC_CODE),
                _edge("store-front", "store-front", DiscoveryMethod.STATIC_CODE),
            ],
            "runtime_edges": [
                _edge(
                    "default/store-front",
                    "order-service.default.svc.cluster.local",
                    DiscoveryMethod.RUNTIME_TELEMETRY,
                )
            ],
        }

        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)

        self.assertEqual(
            [(s.service_id, s.metadata) for s in graph["services"]],
            [
                ("default/store-front", {"repository": "repo-a"}),
                ("default/order-service", {}),
            ],
        )
        edges = graph["edges"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(
            (edges[0].source_service_id, edges[0].target_service_id),
            ("default/store-front", "default/order-service"),
        )
        self.assertEqual(
            edges[0].discovery_methods,
            [DiscoveryMethod.STATIC_CODE, DiscoveryMethod.RUNTIME_TELEMETRY],
        )


if __name__ == "__main__":
    unittest.main()
