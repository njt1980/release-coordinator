"""Tests for the service inventory and criticality report (T4a)."""

import csv
import io
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src import api
from src.coordinator import nodes as coordinator_nodes
from src.core.models import (
    DependencyEdge,
    DeployedWorkload,
    DiscoveryMethod,
    KnowledgeGraph,
    RepositoryCoverage,
    ServiceCoverage,
    ServiceNode,
    UnscannedFile,
    UnscannedReason,
)
from src.core.service_ids import unresolved_entity
from src.orchestration.inventory import build_inventory, inventory_csv
from src.tools.baseline_store import BaselineScope, StoredBaseline

URN = "urn:release-coord:acme:prod:aks-1:shop"
T0 = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
SHA = "a" * 40


def _id(name: str) -> str:
    return f"{URN}:{name}"


def _edge(source: str, target: str, method: DiscoveryMethod, seen: datetime = T0) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source, target_service_id=target, discovery_methods=[method],
        confidence_score=1.0, last_observed_at=seen,
    )


def _service(name: str, **fields: Any) -> ServiceNode:
    return ServiceNode(service_id=_id(name), name=name, **fields)


GRAPH = KnowledgeGraph(
    services=[
        _service("front", metadata={"repository": "shop", "service_root": "src/front"}),
        _service("orders", metadata={"repository": "shop", "service_root": "src/orders"},
                 deployed_workloads=[DeployedWorkload(workload="Deployment/orders", images=["acr.io/orders:1"])]),
        _service("db"),
        _service("audit", metadata={"repository": "shop", "service_root": "src/audit"}),
    ],
    edges=[
        _edge(_id("front"), _id("orders"), DiscoveryMethod.STATIC_CODE),
        _edge(_id("orders"), _id("db"), DiscoveryMethod.RUNTIME_TELEMETRY, T1),
        _edge(_id("orders"), _id("db"), DiscoveryMethod.STATIC_CODE),
        _edge(_id("audit"), _id("db"), DiscoveryMethod.STATIC_CODE),
        _edge(_id("orders"), "unresolved:api.stripe.com", DiscoveryMethod.STATIC_CODE),
    ],
    unresolved_entities=[unresolved_entity("api.stripe.com")],
    static_coverage=[
        RepositoryCoverage(
            repository="shop",
            commit_sha=SHA,
            services=[
                ServiceCoverage(service_name="orders", root="src/orders", files_scanned=10,
                                unscanned=[UnscannedFile(path="src/orders/x.rb", reason=UnscannedReason.UNSUPPORTED_LANGUAGE)]),
                ServiceCoverage(service_name="front", root="src/front", files_scanned=5),
            ],
        )
    ],
)


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        report = build_inventory(GRAPH, "snap-1")
        self.rows = {row.name: row for row in report.rows}
        self.order = [row.name for row in report.rows]

    def test_most_depended_on_ranks_first(self) -> None:
        # db: orders, audit and (through orders) front depend on it.
        self.assertEqual(self.order, ["db", "orders", "audit", "front"])
        self.assertEqual([self.rows[n].criticality_rank for n in self.order], [1, 2, 3, 4])
        self.assertEqual((self.rows["db"].dependents, self.rows["db"].callers), (3, 2))

    def test_row_contents(self) -> None:
        orders = self.rows["orders"]
        self.assertEqual((orders.callers, orders.callees, orders.unresolved_targets), (1, 1, 1))
        self.assertEqual(orders.found_by, ["K8S_MANIFEST", "RUNTIME_TELEMETRY", "STATIC_CODE"])
        self.assertEqual(orders.images, ["acr.io/orders:1"])
        self.assertEqual(orders.runtime_last_seen, T1)
        self.assertEqual((orders.files_scanned, orders.unscanned_files), (10, 1))
        self.assertIsNone(self.rows["audit"].runtime_last_seen)
        self.assertIsNone(self.rows["audit"].unscanned_files)  # no coverage recorded

    def test_a_service_without_a_repository_came_from_the_cluster(self) -> None:
        self.assertIn("K8S_MANIFEST", self.rows["db"].found_by)
        self.assertEqual(self.rows["db"].repository, "")

    def test_cycles_do_not_loop(self) -> None:
        graph = GRAPH.model_copy(update={"edges": [*GRAPH.edges, _edge(_id("db"), _id("front"), DiscoveryMethod.STATIC_CODE)]})
        rows = {row.name: row for row in build_inventory(graph, "s").rows}
        self.assertEqual(rows["db"].dependents, 3)


class CsvTests(unittest.TestCase):
    def test_csv_lists_every_service_and_quotes_formulas(self) -> None:
        graph = GRAPH.model_copy(update={"services": [*GRAPH.services, _service("x", metadata={"repository": "=HYPERLINK(1)"})]})
        rows = list(csv.DictReader(io.StringIO(inventory_csv(build_inventory(graph, "s")))))
        self.assertEqual(len(rows), 5)
        self.assertEqual(next(r for r in rows if r["name"] == "orders")["found_by"], "K8S_MANIFEST;RUNTIME_TELEMETRY;STATIC_CODE")
        self.assertEqual(next(r for r in rows if r["name"] == "x")["repository"], "'=HYPERLINK(1)")


class InventoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        store = MagicMock()
        store.latest.return_value = StoredBaseline(scope=BaselineScope(tenant="acme", project_id="shop"), snapshot_id="snap-1", graph=GRAPH)
        patcher = patch.object(coordinator_nodes, "BASELINE_STORE", store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = store
        self.client = TestClient(api.app)

    def test_json_and_csv(self) -> None:
        body = self.client.get("/api/v1/projects/acme/shop/inventory").json()
        self.assertEqual(body["rows"][0]["name"], "db")
        response = self.client.get("/api/v1/projects/acme/shop/inventory?format=csv")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/csv"))
        self.assertIn('filename="inventory-acme-shop-snap-1.csv"', response.headers["content-disposition"])

    def test_no_baseline(self) -> None:
        self.store.latest.return_value = None
        self.assertEqual(self.client.get("/api/v1/projects/acme/shop/inventory").status_code, 404)


if __name__ == "__main__":
    unittest.main()
