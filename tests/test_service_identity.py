"""Tests for canonical service IDs and their use during reconciliation."""

import unittest
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

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


    def test_merging_keeps_mock_fixture_provenance_visible(self) -> None:
        static = _edge("a", "b", DiscoveryMethod.STATIC_CODE).model_copy(
            update={"provenance": "azure_devops"}
        )
        mock = DependencyEdge(
            source_service_id="a",
            target_service_id="b",
            discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
            confidence_score=0.0,
            last_observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            provenance="mock_fixture",
            environment="mock",
        )
        state: Any = {
            "discovered_services": [],
            "static_edges": [static],
            "runtime_edges": [mock],
        }

        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)

        (edge,) = graph["edges"]
        self.assertEqual(edge.provenance, "azure_devops,mock_fixture")
        self.assertEqual(edge.environment, "unknown,mock")
        self.assertEqual(edge.confidence_score, 0.7)
        self.assertTrue(edge.is_mock_fixture)


class EdgeProvenanceTests(unittest.TestCase):
    def test_mock_fixture_edges_must_have_zero_confidence(self) -> None:
        with self.assertRaises(ValidationError):
            DependencyEdge(
                source_service_id="a",
                target_service_id="b",
                discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
                confidence_score=1.0,
                last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                provenance="mock_fixture",
            )

    def test_legacy_edges_default_to_unknown_labels(self) -> None:
        edge = DependencyEdge.model_validate(
            {
                "source_service_id": "a",
                "target_service_id": "b",
                "discovery_methods": ["STATIC_CODE"],
                "confidence_score": 0.7,
                "last_observed_at": "2026-01-01T00:00:00Z",
            }
        )
        self.assertEqual((edge.provenance, edge.environment), ("unknown", "unknown"))
        self.assertFalse(edge.is_mock_fixture)


if __name__ == "__main__":
    unittest.main()
