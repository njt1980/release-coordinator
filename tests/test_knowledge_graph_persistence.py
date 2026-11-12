"""Tests for versioned baseline snapshots bridging discovery and coordination."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError

from src.coordinator import nodes as coordinator_nodes
from src.core.models import (
    DependencyEdge,
    DiscoveryIncompleteError,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    KnowledgeGraph,
    ServiceNode,
)
from src.discovery_engine import nodes as discovery_nodes
from src.tools.baseline_store import BaselineScope, BaselineStore

COMPLETE = {DiscoveryStatus.SUCCESS, DiscoveryStatus.SKIPPED}
SCOPE = BaselineScope(tenant="acme", project_id="shop")


def _edge(source: str, target: str, provenance: str = "azure_devops") -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.STATIC_CODE],
        confidence_score=0.0 if provenance == "mock_fixture" else 0.7,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        provenance=provenance,
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


class _StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = BaselineStore(self.root)
        for module in (discovery_nodes, coordinator_nodes):
            patcher = patch.object(module, "BASELINE_STORE", self.store)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _persist(
        self,
        edges: list[DependencyEdge],
        reports: list[DiscoveryStageReport] | None = None,
        scope: BaselineScope = SCOPE,
    ) -> str:
        state: Any = {
            "tenant": scope.tenant,
            "project_id": scope.project_id,
            "discovery_reports": _reports() if reports is None else reports,
            "reconciled_graph": {
                "services": [ServiceNode(service_id="payment-service", name="Payment")],
                "edges": edges,
            },
        }
        result = discovery_nodes.persist_to_graph_db(state)
        snapshot_id = result["baseline_snapshot_id"]
        assert isinstance(snapshot_id, str)
        return snapshot_id

    def _snapshot_files(self, scope: BaselineScope = SCOPE) -> list[Path]:
        directory = self.root / scope.tenant / scope.project_id
        return sorted(directory.glob("*.json")) if directory.exists() else []


class BaselinePersistenceTests(_StoreTestCase):
    def test_persist_writes_versioned_snapshot_under_tenant_and_project(self) -> None:
        snapshot_id = self._persist([_edge("api-gateway", "payment-service")])

        path = self.root / "acme" / "shop" / f"{snapshot_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["services"][0]["service_id"], "payment-service")
        self.assertEqual(data["edges"][0]["discovery_methods"], ["STATIC_CODE"])
        self.assertEqual(data["edges"][0]["provenance"], "azure_devops")
        self.assertEqual(
            DependencyEdge.model_validate(data["edges"][0]),
            _edge("api-gateway", "payment-service"),
        )
        self.assertEqual(
            [report["status"] for report in data["discovery"]],
            ["SUCCESS", "SKIPPED", "SUCCESS"],
        )

    def test_persist_defaults_scope_when_state_names_none(self) -> None:
        state: Any = {"discovery_reports": _reports(), "reconciled_graph": {}}
        discovery_nodes.persist_to_graph_db(state)
        self.assertEqual(len(self._snapshot_files(BaselineScope())), 1)

    def test_each_run_writes_its_own_snapshot(self) -> None:
        first = self._persist([_edge("a", "b")])
        second = self._persist([_edge("a", "c")])

        self.assertNotEqual(first, second)
        self.assertEqual(len(self._snapshot_files()), 2)
        latest = self.store.latest(SCOPE)
        assert latest is not None
        self.assertEqual(latest.snapshot_id, max(first, second))
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_projects_are_isolated(self) -> None:
        other = BaselineScope(tenant="acme", project_id="billing")
        self._persist([_edge("a", "b")])
        self._persist([_edge("x", "y")], scope=other)

        latest = self.store.latest(other)
        assert latest is not None
        self.assertEqual(latest.graph.edges[0].source_service_id, "x")
        self.assertEqual(len(self._snapshot_files()), 1)

    def test_persist_refuses_baseline_when_a_stage_is_incomplete(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])
        original = self._snapshot_files()

        for status in [DiscoveryStatus.FAILED, DiscoveryStatus.PARTIAL]:
            with self.subTest(status=status):
                with self.assertRaises(DiscoveryIncompleteError) as caught:
                    self._persist([], _reports(runtime_status=status))
                self.assertIn(f"RUNTIME_TELEMETRY {status.value}", str(caught.exception))
                self.assertIn("query timed out", str(caught.exception))
                # No snapshot is written, so the good baseline stays latest.
                self.assertEqual(self._snapshot_files(), original)

    def test_persist_refuses_baseline_when_a_stage_did_not_report(self) -> None:
        with self.assertRaises(DiscoveryIncompleteError):
            self._persist([], _reports()[:2])
        self.assertEqual(self._snapshot_files(), [])

    def test_persist_refuses_baseline_when_every_stage_skipped(self) -> None:
        skipped = [
            report.model_copy(update={"status": DiscoveryStatus.SKIPPED})
            for report in _reports()
        ]
        with self.assertRaises(DiscoveryIncompleteError):
            self._persist([], skipped)

    def test_store_refuses_incomplete_graph_directly(self) -> None:
        with self.assertRaises(DiscoveryIncompleteError):
            self.store.write(SCOPE, KnowledgeGraph(services=[], edges=[]))
        self.assertEqual(self._snapshot_files(), [])

    def test_latest_skips_newer_unusable_snapshots(self) -> None:
        good = self._persist([_edge("a", "b")])
        directory = self.root / "acme" / "shop"
        incomplete = {
            "services": [],
            "edges": [],
            "discovery": [
                r.model_dump(mode="json")
                for r in _reports(runtime_status=DiscoveryStatus.FAILED)
            ],
        }
        (directory / "99990101T000000000000Z-incompl.json").write_text(
            json.dumps(incomplete), encoding="utf-8"
        )
        (directory / "99990101T000000000000Z-legacy0.json").write_text(
            json.dumps({"services": [], "edges": []}), encoding="utf-8"
        )
        (directory / "99990101T000000000000Z-garbage.json").write_text(
            "{not json", encoding="utf-8"
        )

        with self.assertLogs("src.tools.baseline_store", level="WARNING") as logs:
            latest = self.store.latest(SCOPE)

        assert latest is not None
        self.assertEqual(latest.snapshot_id, good)
        self.assertEqual(len(logs.records), 3)

    def test_latest_ignores_in_progress_temp_files(self) -> None:
        directory = self.root / "acme" / "shop"
        directory.mkdir(parents=True)
        (directory / ".20260101T000000000000Z-abc.x.tmp").write_text("{}")
        self.assertIsNone(self.store.latest(SCOPE))

    def test_scope_and_snapshot_ids_reject_path_traversal(self) -> None:
        for tenant in ["..", "../x", "a/b", ".hidden", ""]:
            with self.subTest(tenant=tenant):
                with self.assertRaises(ValidationError):
                    BaselineScope(tenant=tenant)
        with self.assertRaises(ValueError):
            self.store.read(SCOPE, "../escape")

    def test_demo_mode_reflects_mock_fixture_edges(self) -> None:
        self._persist([_edge("a", "b")])
        latest = self.store.latest(SCOPE)
        assert latest is not None
        self.assertFalse(latest.demo_mode)

        self._persist([_edge("a", "b"), _edge("c", "d", provenance="mock_fixture")])
        latest = self.store.latest(SCOPE)
        assert latest is not None
        self.assertTrue(latest.demo_mode)


class StaticRadiusTests(_StoreTestCase):
    def _radius(self, scope: BaselineScope = SCOPE) -> dict[str, object]:
        state: Any = {
            "tenant": scope.tenant,
            "project_id": scope.project_id,
            "target_services": ["payment-service"],
        }
        return coordinator_nodes.calculate_static_radius_node(state)

    def test_static_radius_splits_upstream_and_downstream(self) -> None:
        snapshot_id = self._persist(
            [
                _edge("api-gateway", "payment-service"),
                _edge("payment-service", "ledger-service"),
                _edge("order-service", "payment-service"),
                _edge("api-gateway", "payment-service"),
                _edge("order-service", "inventory-service"),
            ]
        )

        result = self._radius()

        self.assertEqual(result["upstream_services"], ["api-gateway", "order-service"])
        self.assertEqual(result["downstream_services"], ["ledger-service"])
        matched = result["matched_edges"]
        assert isinstance(matched, list)
        self.assertEqual(len(matched), 4)
        self.assertEqual(result["discovery_issues"], [])
        self.assertEqual(result["baseline_snapshot_id"], snapshot_id)
        self.assertFalse(result["baseline_demo_mode"])

    def test_static_radius_uses_latest_good_snapshot_after_failed_run(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])
        with self.assertRaises(DiscoveryIncompleteError):
            self._persist([], _reports(runtime_status=DiscoveryStatus.FAILED))

        result = self._radius()

        self.assertEqual(result["upstream_services"], ["api-gateway"])
        self.assertEqual(result["discovery_issues"], [])

    def test_static_radius_reports_demo_mode(self) -> None:
        self._persist([_edge("api-gateway", "payment-service", "mock_fixture")])
        self.assertTrue(self._radius()["baseline_demo_mode"])

    def test_static_radius_missing_baseline_is_a_discovery_issue(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])
        other = BaselineScope(tenant="acme", project_id="unknown-project")

        with self.assertLogs(coordinator_nodes.logger, level="WARNING"):
            result = self._radius(other)

        self.assertEqual(
            result,
            {
                "upstream_services": [],
                "downstream_services": [],
                "matched_edges": [],
                "discovery_issues": [
                    "No complete discovery baseline found for "
                    "acme/unknown-project; run discovery first."
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
