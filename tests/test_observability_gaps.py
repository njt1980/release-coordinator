"""Tests for the telemetry stack probe and observability gaps (T2a).

Profiles and edges are shaped like the Application Insights probe lab
(findings 05): classic SDK services with the Kubernetes enricher, Azure
Monitor Distro services without a namespace, Beyla roles with no requests,
startup rows without a role name.
"""

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from azure.monitor.query import LogsQueryResult, LogsTable
from fastapi.testclient import TestClient

from src import api
from src import observability_api
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.observability import gap_stacks, observability_gaps
from src.coordinator.runtime_drift import baseline_identity_context
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryResult,
    EdgeResolution,
    KnowledgeGraph,
    ObservabilityGap,
    ObservabilityGapKind,
    ScanContext,
    ServiceNode,
    TargetDetail,
    TargetDetailKind,
    TelemetryRoleProfile,
    TelemetryScope,
)
from src.core.telemetry_capability import TelemetryStack, telemetry_stack
from src.orchestration import briefing
from src.tools.baseline_store import BaselineScope, StoredBaseline
from src.tools.policy_store import PolicyMissingError
from src.tools.telemetry_extractor import (
    TelemetryExtractor,
    build_role_profile_query,
    parse_role_profile_response,
)

URN = "urn:release-coord:acme:prod:aks-1:probe"
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
SB = "rclabprobe.servicebus.windows.net"
COMPONENT = (
    "/subscriptions/48a7217e-eeb0-4625-96f1-8eb3a574481e/resourcegroups/lab"
    "/providers/microsoft.insights/components/rc-ai"
)


def _id(name: str) -> str:
    return f"{URN}:{name}"


GRAPH = KnowledgeGraph(
    services=[ServiceNode(service_id=_id(n), name=n) for n in ("gateway", "orders", "inventory", "fulfillment", "mssql")],
    edges=[],
    unresolved_entities=[],
    scan=ScanContext(environment="prod", cluster="aks-1", namespaces=["probe"], telemetry_workspace_id="ws-1"),
)
CONTEXT = baseline_identity_context("acme", GRAPH)


def _profile(role: str, **fields: Any) -> TelemetryRoleProfile:
    values: dict[str, Any] = dict(
        request_rows=100, dependency_rows=100, represented_calls=200, max_item_count=1,
        rows_with_namespace=200, namespace="probe", stacks=[TelemetryStack.APPINSIGHTS_CLASSIC],
    )
    values.update(fields)
    return TelemetryRoleProfile(role=role, **values)


PROFILES = [
    _profile("orders", request_rows=709, dependency_rows=4256, represented_calls=38868, max_item_count=15),
    _profile("inventory"),
    _profile("gateway", namespace="", rows_with_namespace=0, stacks=[TelemetryStack.AZURE_MONITOR_OTEL]),
    _profile("store-front", namespace="default", request_rows=0, stacks=[TelemetryStack.OTEL_COLLECTOR]),
    _profile("", namespace="", rows_with_namespace=0, request_rows=0, dependency_rows=15, represented_calls=15),
]


def _edge(source: str, target: str, kind: str, resolution: EdgeResolution, **fields: Any) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
        confidence_score=1.0,
        last_observed_at=fields.pop("seen", NOW - timedelta(hours=1)),
        dependency_type=kind,
        resolution=[resolution],
        **fields,
    )


SEND = "Queue Message | Azure Service Bus"
QUEUE = [TargetDetail(kind=TargetDetailKind.RESOURCE, name="orders")]


def _kinds(gaps: list[ObservabilityGap]) -> set[tuple[ObservabilityGapKind, str]]:
    return {(gap.kind, gap.subject.rsplit(":", 1)[-1]) for gap in gaps}


class StackTests(unittest.TestCase):
    def test_sdk_versions_seen_in_the_lab(self) -> None:
        for version, stack in [
            ("rdddsc:2.22.0-997", TelemetryStack.APPINSIGHTS_CLASSIC),
            ("aspnet5c:2.22.0+43825e06", TelemetryStack.APPINSIGHTS_CLASSIC),
            ("dotnet10.0.15-servicing.26:otel1.12.0:ext1.4.0-d", TelemetryStack.AZURE_MONITOR_OTEL),
            ("otelc-unknown-lin-amd64", TelemetryStack.OTEL_COLLECTOR),
            ("javascript:3.0.0", TelemetryStack.UNKNOWN),
        ]:
            with self.subTest(version=version):
                self.assertIs(telemetry_stack(version), stack)

    def test_stack_counts_skip_unnamed_rows(self) -> None:
        self.assertEqual(
            gap_stacks(PROFILES),
            {
                TelemetryStack.APPINSIGHTS_CLASSIC: 2,
                TelemetryStack.AZURE_MONITOR_OTEL: 1,
                TelemetryStack.OTEL_COLLECTOR: 1,
            },
        )


class ProfileQueryTests(unittest.TestCase):
    def test_query_summarizes_requests_and_dependencies_per_role(self) -> None:
        query = build_role_profile_query(7, TelemetryScope(components=[COMPONENT]))
        self.assertTrue(query.startswith("union AppRequests, AppDependencies\n"))
        self.assertIn(f'_ResourceId in~ ("{COMPONENT}")', query)
        self.assertIn("Represented = sum(ItemCount), MaxItemCount = max(ItemCount)", query)
        self.assertIn("by Role\n", query)

    def test_parse_rows(self) -> None:
        columns = [
            "Role", "RequestRows", "DependencyRows", "Represented", "MaxItemCount",
            "RowsWithNamespace", "Namespace", "SdkVersions", "Components", "LastSeen",
        ]
        table = LogsTable(
            name="PrimaryResult",
            columns=columns,
            columns_types=["string"] * len(columns),
            rows=[
                ["orders", 943, 5695, 44069, 15, 6638, "probe",
                 '["rdddsc:2.22.0-997","aspnet5c:2.22.0+4382"]', f'["{COMPONENT}"]', "2026-11-14T08:31:00Z"],
                ["", 0, 15, 15, 1, 0, None, '["rdddsc:2.22.0-997"]', "[]", "2026-11-14T08:00:00Z"],
            ],
        )
        orders, unnamed = parse_role_profile_response(LogsQueryResult(tables=[table]))
        self.assertEqual((orders.role, orders.namespace, orders.rows, orders.max_item_count), ("orders", "probe", 6638, 15))
        self.assertEqual(orders.stacks, [TelemetryStack.APPINSIGHTS_CLASSIC])
        self.assertEqual(orders.components, ["rc-ai"])
        self.assertEqual((unnamed.role, unnamed.namespace), ("", ""))

    def test_probe_never_raises(self) -> None:
        extractor = TelemetryExtractor.__new__(TelemetryExtractor)
        extractor._additional_workspaces = []
        extractor._source = "azure"
        extractor._workspace_id = "ws"
        extractor._client = MagicMock()
        extractor._client.query_workspace.side_effect = RuntimeError("boom")
        self.assertIsNone(extractor.get_role_profiles(7, None))
        extractor._source = "mock"
        self.assertIsNone(extractor.get_role_profiles(7, None))


class GapRuleTests(unittest.TestCase):
    def test_estate_report(self) -> None:
        gaps = observability_gaps(GRAPH, CONTEXT, PROFILES, [], NOW)
        self.assertEqual(
            _kinds(gaps),
            {
                (ObservabilityGapKind.SAMPLED, "orders"),
                (ObservabilityGapKind.NO_NAMESPACE, "gateway"),
                (ObservabilityGapKind.ROLE_NOT_IN_INVENTORY, "default/store-front"),
                (ObservabilityGapKind.NO_ROLE_NAME, ""),
                (ObservabilityGapKind.SERVICE_WITHOUT_TELEMETRY, "fulfillment"),
                (ObservabilityGapKind.SERVICE_WITHOUT_TELEMETRY, "mssql"),
            },
        )
        sampled = next(g for g in gaps if g.kind is ObservabilityGapKind.SAMPLED)
        self.assertIn("4965 rows stand for 38868 calls (about 13% kept, up to 15 calls per row)", sampled.detail)

    def test_radius_report_keeps_only_radius_services(self) -> None:
        gaps = observability_gaps(GRAPH, CONTEXT, PROFILES, [], NOW, {_id("gateway")})
        self.assertEqual(
            _kinds(gaps),
            {(ObservabilityGapKind.NO_NAMESPACE, "gateway"), (ObservabilityGapKind.NO_ROLE_NAME, "")},
        )

    def test_role_without_requests(self) -> None:
        profiles = [_profile("inventory", request_rows=0, stacks=[TelemetryStack.OTEL_COLLECTOR])]
        gaps = observability_gaps(GRAPH, CONTEXT, profiles, [], NOW, {_id("inventory")})
        self.assertEqual(_kinds(gaps), {(ObservabilityGapKind.NO_REQUEST_TELEMETRY, "inventory")})

    def test_send_without_traced_consumer(self) -> None:
        edges = [_edge("probe/orders", SB, SEND, EdgeResolution.TARGET, target_details=QUEUE)]
        gaps = observability_gaps(GRAPH, CONTEXT, [], edges, NOW, {_id("orders")})
        self.assertEqual(_kinds(gaps), {(ObservabilityGapKind.CONSUMER_NOT_TRACED, "orders")})
        self.assertIn(f"sends messages to {SB}/orders", gaps[0].detail)

    def test_send_joined_to_its_consumer_is_no_gap(self) -> None:
        edges = [
            _edge("probe/orders", SB, SEND, EdgeResolution.TARGET, target_details=QUEUE),
            _edge(
                "probe/orders", "fulfillment", SEND, EdgeResolution.REQUEST_JOIN,
                target_details=[*QUEUE, TargetDetail(kind=TargetDetailKind.ADDRESS, name=SB)],
            ),
        ]
        self.assertEqual(observability_gaps(GRAPH, CONTEXT, [], edges, NOW, {_id("orders")}), [])

    def test_old_sends_and_consumer_receives_are_not_judged(self) -> None:
        edges = [
            _edge("probe/orders", SB, SEND, EdgeResolution.TARGET, seen=NOW - timedelta(days=5)),
            # A consumer's own receive span is not a send.
            _edge("fulfillment", SB, "Microsoft.ServiceBus", EdgeResolution.TARGET),
        ]
        self.assertEqual(observability_gaps(GRAPH, CONTEXT, [], edges, NOW), _estate_without_sends())


def _estate_without_sends() -> list[ObservabilityGap]:
    return observability_gaps(GRAPH, CONTEXT, [], [], NOW)


class BriefingTests(unittest.TestCase):
    def test_gaps_are_a_context_group(self) -> None:
        gap = ObservabilityGap(kind=ObservabilityGapKind.NO_NAMESPACE, subject=_id("gateway"), detail="No namespace.")
        assessment = MagicMock()
        assessment.evidence.exempted_dependencies = []
        assessment.evidence.exempted_files = []
        assessment.evidence.exempted_unscanned_files = []
        assessment.evidence.live_coverage = None
        assessment.evidence.review = None
        assessment.evidence.shape_findings = []
        assessment.evidence.observability_gaps = [gap]
        (group,) = briefing._context_groups(assessment)
        self.assertEqual(group.root_key, "info:observability")
        self.assertIs(group.rank, briefing.BriefingRank.INFO)
        self.assertEqual(group.what_to_check, ["gateway: no namespace. No namespace."])


class ObservabilityApiTests(unittest.TestCase):
    URL = "/api/v1/projects/acme/shop/observability"

    def setUp(self) -> None:
        store = MagicMock()
        store.latest.return_value = StoredBaseline(
            scope=BaselineScope(tenant="acme", project_id="shop"), snapshot_id="snap-1", graph=GRAPH
        )
        policies = MagicMock()
        policies.load.side_effect = PolicyMissingError("none")
        for name, value in (("BASELINE_STORE", store), ("POLICY_STORE", policies)):
            patcher = patch.object(coordinator_nodes, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = store
        self.client = TestClient(api.app)

    def _extractor(self, profiles: list[TelemetryRoleProfile] | None) -> Any:
        patcher = patch.object(observability_api, "TelemetryExtractor")
        extractor_cls = patcher.start()
        self.addCleanup(patcher.stop)
        extractor = extractor_cls.return_value
        extractor.is_mock = False
        extractor.get_role_profiles.return_value = profiles
        extractor.get_active_dependencies.return_value = DiscoveryResult.success([])
        return extractor_cls

    def test_report_probes_the_baseline_workspace(self) -> None:
        extractor_cls = self._extractor(PROFILES)
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        extractor_cls.assert_called_once_with("ws-1", ())
        self.assertEqual(body["snapshot_id"], "snap-1")
        self.assertEqual(body["stacks"]["appinsights_classic"], 2)
        self.assertIn("sampled", {gap["kind"] for gap in body["gaps"]})

    def test_failed_probe_is_a_bad_gateway(self) -> None:
        self._extractor(None)
        self.assertEqual(self.client.get(self.URL).status_code, 502)

    def test_baseline_without_telemetry_is_a_conflict(self) -> None:
        self._extractor(PROFILES)
        scan = ScanContext(environment="prod", cluster="aks-1")
        self.store.latest.return_value = StoredBaseline(
            scope=BaselineScope(tenant="acme", project_id="shop"),
            snapshot_id="snap-1",
            graph=GRAPH.model_copy(update={"scan": scan}),
        )
        self.assertEqual(self.client.get(self.URL).status_code, 409)

    def test_no_baseline_is_not_found(self) -> None:
        self.store.latest.return_value = None
        self.assertEqual(self.client.get(self.URL).status_code, 404)


if __name__ == "__main__":
    unittest.main()
