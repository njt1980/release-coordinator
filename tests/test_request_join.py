"""Tests for callee resolution by request join (T2b).

Rows are modelled on the Application Insights probe lab (findings 05):
classic SDK callers whose targets are ``host | cid-v1:<appId>`` or a
Service Bus entity, Distro callers with bare hosts, and the classic
Kubernetes enricher's calls to the API server.
"""

import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import MagicMock

from azure.monitor.query import LogsQueryResult, LogsTable

from src.coordinator.runtime_drift import baseline_identity_context, resolve_live_edges
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    EdgeResolution,
    KnowledgeGraph,
    ScanContext,
    ServiceNode,
    TargetDetail,
    TargetDetailKind,
    TelemetryScope,
)
from src.discovery_engine.nodes import _merge_edge
from src.tools.telemetry_extractor import (
    TelemetryExtractor,
    build_dependency_query,
    parse_dependency_response,
)
from src.tools.telemetry_normalization import normalize_edges

COMPONENT = (
    "/subscriptions/48a7217e-eeb0-4625-96f1-8eb3a574481e/resourcegroups/lab"
    "/providers/microsoft.insights/components/rc-ai"
)
COLUMNS = [
    "Source", "SourceNamespace", "Target", "DependencyType", "Noise", "LastObservedAt",
    "Calls", "Failures", "Callee", "CalleeNamespace", "ResolvedCalls",
]
SEEN = "2026-11-14T08:25:00Z"
SB = "rclabprobe.servicebus.windows.net"


def _response(rows: list[list[object]]) -> LogsQueryResult:
    table = LogsTable(
        name="PrimaryResult", columns=COLUMNS, columns_types=["string"] * len(COLUMNS), rows=rows
    )
    return LogsQueryResult(tables=[table])


ROWS: list[list[object]] = [
    # classic orders -> inventory in another component, resolved by join
    ["orders", "probe", "inventory | cid-v1:fadaaf94", "Http (tracked component)", "", SEEN,
     6289, 0, "inventory", "probe", 6288],
    # classic send to a queue, consumed by a Distro service (no namespace)
    ["orders", "probe", f"{SB}/orders", "Queue Message | Azure Service Bus", "", SEEN,
     6288, 0, "fulfillment", "", 6172],
    # Distro gateway -> orders; only some calls joined (sampling)
    ["gateway", "", "orders", "HTTP", "", SEEN, 6446, 19, "orders", "probe", 942],
    # the same target reaching two callees (a shared ingress)
    ["gateway", "", "ingress.shop", "HTTP", "", SEEN, 100, 0, "orders", "probe", 40],
    ["gateway", "", "ingress.shop", "HTTP", "", SEEN, 100, 0, "inventory", "probe", 30],
    # no callee: a database
    ["orders", "probe", "tcp:mssql,1433 | orders", "SQL", "", SEEN, 6288, 0, None, None, None],
    # the classic Kubernetes enricher calling the API server
    ["orders", "probe", "10.0.0.1", "HTTP", "kubernetes_api", SEEN, 40, 7, None, None, None],
]


class RequestJoinQueryTests(unittest.TestCase):
    def test_joins_dependency_ids_to_request_parents(self) -> None:
        query = build_dependency_query(7)
        self.assertIn("let callees = AppRequests", query)
        self.assertIn("$left.OperationId == $right.OperationId, $left.Id == $right.ParentId", query)
        self.assertIn("ResolvedCalls = sum(ItemCount)", query)
        self.assertIn("join kind=leftouter (resolved)", query)

    def test_join_window_is_bounded_and_lags_ingestion(self) -> None:
        query = build_dependency_query(7, join_days=2, join_lag_minutes=10)
        self.assertIn("TimeGenerated between (ago(2d) .. ago(10m))", query)
        self.assertIn("| where TimeGenerated >= ago(2d)\n| extend CalleeName", query)
        short = build_dependency_query(1, join_days=2)
        self.assertIn("between (ago(1d) ..", short)
        with self.assertRaises(ValueError):
            build_dependency_query(7, join_days=0)

    def test_scope_applies_to_callers_and_callees(self) -> None:
        query = build_dependency_query(7, TelemetryScope(components=[COMPONENT]))
        self.assertEqual(query.count(f'_ResourceId in~ ("{COMPONENT}")'), 2)

    def test_kubernetes_api_calls_are_marked_noise(self) -> None:
        query = build_dependency_query(7)
        self.assertIn('Noise = iff(Data matches regex @"^https?://[^/]+/(api/v1/', query)
        self.assertIn("where isempty(Noise)", query)


class RequestJoinParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metrics: dict[str, dict[str, int | float]] = {}
        self.noise: Counter[str] = Counter()
        result = parse_dependency_response(_response(ROWS), self.metrics, self.noise)
        self.edges = {
            (edge.source_service_id, edge.target_service_id, edge.dependency_type): edge
            for edge in result.items
        }

    def test_joined_rows_add_an_edge_to_the_callee(self) -> None:
        joined = self.edges[("probe/orders", "probe/inventory", "Http (tracked component)")]
        self.assertEqual(joined.resolution, [EdgeResolution.REQUEST_JOIN])
        self.assertEqual(
            joined.target_details,
            [
                TargetDetail(kind=TargetDetailKind.ADDRESS, name="inventory"),
                TargetDetail(kind=TargetDetailKind.APP_COMPONENT, name="fadaaf94"),
            ],
        )
        by_target = self.edges[("probe/orders", "inventory | cid-v1:fadaaf94", "Http (tracked component)")]
        self.assertEqual(by_target.resolution, [EdgeResolution.TARGET])

    def test_messaging_consumer_becomes_the_target_with_the_queue(self) -> None:
        joined = self.edges[("probe/orders", "fulfillment", "Queue Message | Azure Service Bus")]
        self.assertIn(TargetDetail(kind=TargetDetailKind.RESOURCE, name="orders"), joined.target_details or [])
        self.assertIn(TargetDetail(kind=TargetDetailKind.ADDRESS, name=SB), joined.target_details or [])

    def test_a_target_with_two_callees_counts_its_calls_once(self) -> None:
        self.assertIn(("gateway", "probe/orders", "HTTP"), self.edges)
        self.assertIn(("gateway", "probe/inventory", "HTTP"), self.edges)
        self.assertEqual(self.metrics["gateway"]["request_count"], 6446 + 100)

    def test_noise_rows_are_counted_not_kept(self) -> None:
        self.assertEqual(self.noise, Counter({"kubernetes_api": 1}))
        self.assertNotIn(("probe/orders", "10.0.0.1", "HTTP"), self.edges)
        self.assertEqual(self.metrics["probe/orders"]["request_count"], 6289 + 6288 + 6288)

    def test_unjoined_rows_keep_only_the_target_edge(self) -> None:
        sql = [key for key in self.edges if key[2] == "SQL"]
        self.assertEqual(sql, [("probe/orders", "tcp:mssql,1433 | orders", "SQL")])


class FullyJoinedTargetTests(unittest.TestCase):
    """Seen in the OpenTelemetry Demo run: an exporter without an address
    writes the span name as Target; when the join names the callee of every
    call, that span name is not kept as a dependency of its own."""

    COLUMNS_WITH_WINDOW = [*COLUMNS, "JoinWindowCalls"]

    def _edges(self, rows: list[list[object]]) -> set[tuple[str, str]]:
        table = LogsTable(
            name="PrimaryResult", columns=self.COLUMNS_WITH_WINDOW,
            columns_types=["string"] * len(self.COLUMNS_WITH_WINDOW), rows=rows,
        )
        result = parse_dependency_response(LogsQueryResult(tables=[table]), {}, Counter())
        return {(edge.source_service_id, edge.target_service_id) for edge in result.items}

    def test_query_counts_calls_in_the_join_window(self) -> None:
        query = build_dependency_query(7)
        self.assertIn("JoinWindowCalls = sum(ItemCount)", query)
        self.assertIn("join kind=leftouter (window)", query)

    def test_a_fully_joined_span_name_target_leaves_only_the_callee(self) -> None:
        span = "grpc.oteldemo.CartService/GetCart"
        edges = self._edges([
            # every call in the window joined, split over two callee rows
            ["frontend", "shop", span, "GRPC", "", SEEN, 500, 0, "cart", "shop", 30, 40],
            ["frontend", "shop", span, "GRPC", "", SEEN, 500, 0, "cart-canary", "shop", 10, 40],
            # only some joined: the target edge stays
            ["checkout", "shop", "oteldemo.CartService/GetCart", "GRPC", "", SEEN, 50, 0, "cart", "shop", 3, 6],
            # nothing in the join window: the target edge stays
            ["quote", "shop", "HGET", "redis", "", SEEN, 9, 0, None, None, None, 0],
        ])
        self.assertNotIn(("shop/frontend", span), edges)
        self.assertIn(("shop/frontend", "shop/cart"), edges)
        self.assertIn(("shop/frontend", "shop/cart-canary"), edges)
        self.assertIn(("shop/checkout", "oteldemo.CartService/GetCart"), edges)
        self.assertIn(("shop/quote", "HGET"), edges)

    def test_without_the_window_column_every_target_is_kept(self) -> None:
        table = LogsTable(name="PrimaryResult", columns=COLUMNS, columns_types=["string"] * len(COLUMNS),
                          rows=[["gateway", "", "orders", "HTTP", "", SEEN, 10, 0, "orders", "probe", 10]])
        result = parse_dependency_response(LogsQueryResult(tables=[table]), {}, Counter())
        self.assertIn("orders", {edge.target_service_id for edge in result.items})


class RequestJoinPipelineTests(unittest.TestCase):
    def test_extractor_reports_noise_with_other_drops(self) -> None:
        extractor = TelemetryExtractor.__new__(TelemetryExtractor)
        extractor._additional_workspaces = []
        extractor._workspace_id = "ws"
        extractor._source = "azure"
        extractor._runtime_metrics = {}
        extractor._dropped_records = {}
        extractor._client = MagicMock()
        extractor._client.query_workspace.return_value = _response(
            [*ROWS, ["orders", "probe", "GET", "HTTP", "", SEEN, 3, 0, None, None, None]]
        )
        result = extractor.get_active_dependencies(days_back=7)
        self.assertEqual(extractor.dropped_records, {"kubernetes_api": 1, "no_host": 1})
        # Joined targets are roles, not addresses: normalization leaves them alone.
        self.assertIn(
            ("probe/orders", "probe/inventory"),
            {(edge.source_service_id, edge.target_service_id) for edge in result.items},
        )

    def test_normalization_keeps_joined_edges_as_they_are(self) -> None:
        edge = DependencyEdge(
            source_service_id="gateway",
            target_service_id="probe/orders",
            discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
            confidence_score=1.0,
            last_observed_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
            resolution=[EdgeResolution.REQUEST_JOIN],
        )
        kept, dropped = normalize_edges([edge])
        self.assertEqual((kept, dropped), ([edge], {}))

    def test_join_and_address_edges_resolve_to_one_baseline_edge(self) -> None:
        urn = "urn:release-coord:acme:prod:aks-1:probe"
        services = {name: f"{urn}:{name}" for name in ("gateway", "orders", "inventory", "fulfillment")}
        graph = KnowledgeGraph(
            services=[ServiceNode(service_id=uri, name=name) for name, uri in services.items()],
            edges=[],
            unresolved_entities=[],
            scan=ScanContext(environment="prod", cluster="aks-1", namespaces=["probe"]),
        )
        edges, _ = normalize_edges(parse_dependency_response(_response(ROWS)).items)
        observed = {
            (edge.source_id, edge.target_id)
            for edge in resolve_live_edges(graph, baseline_identity_context("acme", graph), edges)
        }
        self.assertIn((services["orders"], services["fulfillment"]), observed)
        self.assertIn((services["orders"], services["inventory"]), observed)
        self.assertIn((services["gateway"], services["orders"]), observed)

    def test_merged_edges_keep_both_resolutions(self) -> None:
        base = dict(
            source_service_id="a",
            target_service_id="b",
            discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
            confidence_score=1.0,
            last_observed_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
        )
        merged = _merge_edge(
            DependencyEdge(**base, resolution=[EdgeResolution.TARGET]),  # type: ignore[arg-type]
            DependencyEdge(**base, resolution=[EdgeResolution.REQUEST_JOIN]),  # type: ignore[arg-type]
        )
        self.assertEqual(merged.resolution, [EdgeResolution.REQUEST_JOIN, EdgeResolution.TARGET])


if __name__ == "__main__":
    unittest.main()
