"""Tests for the local JSON knowledge graph bridging discovery and coordination."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.coordinator import nodes as coordinator_nodes
from src.core.models import DependencyEdge, DiscoveryMethod, ServiceNode
from src.discovery_engine import nodes as discovery_nodes


def _edge(source: str, target: str) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.STATIC_CODE],
        confidence_score=0.7,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class KnowledgeGraphPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "knowledge_graph.json"
        for module in (discovery_nodes, coordinator_nodes):
            patcher = patch.object(module, "KNOWLEDGE_GRAPH_PATH", self.path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _persist(self, edges: list[DependencyEdge]) -> None:
        state: Any = {
            "reconciled_graph": {
                "services": [ServiceNode(service_id="payment-service", name="Payment")],
                "edges": edges,
            }
        }
        self.assertEqual(discovery_nodes.persist_to_graph_db(state), {})

    def test_persist_writes_serialized_services_and_edges(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])

        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["services"][0]["service_id"], "payment-service")
        self.assertEqual(data["edges"][0]["discovery_methods"], ["STATIC_CODE"])
        self.assertEqual(
            DependencyEdge.model_validate(data["edges"][0]),
            _edge("api-gateway", "payment-service"),
        )

    def test_static_radius_collects_both_directions_without_duplicates(self) -> None:
        self._persist(
            [
                _edge("api-gateway", "payment-service"),
                _edge("payment-service", "ledger-service"),
                _edge("order-service", "payment-service"),
                _edge("api-gateway", "payment-service"),
                _edge("order-service", "inventory-service"),
            ]
        )
        state: Any = {"target_services": ["payment-service"]}

        result = coordinator_nodes.calculate_static_radius_node(state)

        self.assertEqual(
            result,
            {"static_dependents": ["api-gateway", "ledger-service", "order-service"]},
        )

    def test_static_radius_missing_file_warns_and_returns_empty(self) -> None:
        state: Any = {"target_services": ["payment-service"]}

        with self.assertLogs(coordinator_nodes.logger, level="WARNING"):
            result = coordinator_nodes.calculate_static_radius_node(state)

        self.assertEqual(result, {"static_dependents": []})


if __name__ == "__main__":
    unittest.main()
