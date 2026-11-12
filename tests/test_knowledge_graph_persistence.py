"""Tests for the local JSON knowledge graph bridging discovery and coordination."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.coordinator import nodes as coordinator_nodes
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    ServiceNode,
)
from src.discovery_engine import nodes as discovery_nodes


def _edge(source: str, target: str) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.STATIC_CODE],
        confidence_score=0.7,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _reports(
    runtime_status: DiscoveryStatus = DiscoveryStatus.SUCCESS,
) -> list[DiscoveryStageReport]:
    errors = [] if runtime_status in COMPLETE else ["query timed out"]
    return [
        DiscoveryStageReport(
            source=DiscoveryMethod.STATIC_CODE, status=DiscoveryStatus.SUCCESS
        ),
        DiscoveryStageReport(
            source=DiscoveryMethod.K8S_MANIFEST, status=DiscoveryStatus.SKIPPED
        ),
        DiscoveryStageReport(
            source=DiscoveryMethod.RUNTIME_TELEMETRY,
            status=runtime_status,
            errors=errors,
        ),
    ]


COMPLETE = {DiscoveryStatus.SUCCESS, DiscoveryStatus.SKIPPED}


class KnowledgeGraphPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "knowledge_graph.json"
        for module in (discovery_nodes, coordinator_nodes):
            patcher = patch.object(module, "KNOWLEDGE_GRAPH_PATH", self.path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _persist(
        self,
        edges: list[DependencyEdge],
        reports: list[DiscoveryStageReport] | None = None,
    ) -> None:
        state: Any = {
            "discovery_reports": _reports() if reports is None else reports,
            "reconciled_graph": {
                "services": [ServiceNode(service_id="payment-service", name="Payment")],
                "edges": edges,
            },
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
        self.assertEqual(
            [report["status"] for report in data["discovery"]],
            ["SUCCESS", "SKIPPED", "SUCCESS"],
        )

    def test_persist_refuses_baseline_when_a_stage_is_incomplete(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])
        original = self.path.read_text(encoding="utf-8")

        for status in [DiscoveryStatus.FAILED, DiscoveryStatus.PARTIAL]:
            with self.subTest(status=status):
                with self.assertRaises(
                    discovery_nodes.DiscoveryIncompleteError
                ) as caught:
                    self._persist([], _reports(runtime_status=status))
                self.assertIn(f"RUNTIME_TELEMETRY {status.value}", str(caught.exception))
                self.assertIn("query timed out", str(caught.exception))
                # The previous complete baseline is not overwritten.
                self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_persist_refuses_baseline_when_a_stage_did_not_report(self) -> None:
        with self.assertRaises(discovery_nodes.DiscoveryIncompleteError):
            self._persist([], _reports()[:2])
        self.assertFalse(self.path.exists())

    def test_persist_refuses_baseline_when_every_stage_skipped(self) -> None:
        skipped = [
            report.model_copy(update={"status": DiscoveryStatus.SKIPPED})
            for report in _reports()
        ]
        with self.assertRaises(discovery_nodes.DiscoveryIncompleteError):
            self._persist([], skipped)

    def test_static_radius_splits_upstream_and_downstream(self) -> None:
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

        self.assertEqual(result["upstream_services"], ["api-gateway", "order-service"])
        self.assertEqual(result["downstream_services"], ["ledger-service"])
        matched = result["matched_edges"]
        assert isinstance(matched, list)
        self.assertEqual(len(matched), 4)
        self.assertEqual(result["discovery_issues"], [])

    def test_static_radius_flags_graph_without_discovery_outcomes(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "services": [],
                    "edges": [
                        _edge("api-gateway", "payment-service").model_dump(
                            mode="json"
                        )
                    ],
                }
            ),
            encoding="utf-8",
        )
        state: Any = {"target_services": ["payment-service"]}

        result = coordinator_nodes.calculate_static_radius_node(state)

        self.assertEqual(result["upstream_services"], ["api-gateway"])
        issues = result["discovery_issues"]
        assert isinstance(issues, list)
        self.assertEqual(len(issues), 1)
        self.assertIn("does not record discovery outcomes", issues[0])

    def test_static_radius_flags_incomplete_discovery_in_graph(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "services": [],
                    "edges": [],
                    "discovery": [
                        r.model_dump(mode="json")
                        for r in _reports(runtime_status=DiscoveryStatus.FAILED)
                    ],
                }
            ),
            encoding="utf-8",
        )
        state: Any = {"target_services": ["payment-service"]}

        result = coordinator_nodes.calculate_static_radius_node(state)

        issues = result["discovery_issues"]
        assert isinstance(issues, list)
        self.assertEqual(len(issues), 1)
        self.assertIn("RUNTIME_TELEMETRY FAILED", issues[0])

    def test_static_radius_missing_file_is_a_discovery_issue(self) -> None:
        state: Any = {"target_services": ["payment-service"]}

        with self.assertLogs(coordinator_nodes.logger, level="WARNING"):
            result = coordinator_nodes.calculate_static_radius_node(state)

        self.assertEqual(
            result,
            {
                "upstream_services": [],
                "downstream_services": [],
                "matched_edges": [],
                "discovery_issues": [
                    "Knowledge graph not found; no discovery baseline exists."
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
