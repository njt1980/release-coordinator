"""Tests for holding a release when live telemetry finds edges the baseline lacks."""

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from src.coordinator import nodes as coordinator_nodes
from src.coordinator.runtime_drift import (
    baseline_identity_context,
    drift_issue,
    resolve_live_edges,
    unbaselined_edges,
    workspace_mismatch_issue,
)
from src.core.models import (
    TelemetryClusterFilter,
    UNKNOWN_LABEL,
    ContractChange,
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryResult,
    KnowledgeGraph,
    ObservedEdge,
    PullRequestPayload,
    ReasonCode,
    RuntimeEvidenceState,
    ScanContext,
    ServiceNode,
    TelemetryStatus,
)
from src.core.service_ids import unresolved_entity
from src.tools.baseline_store import BaselineScope
from src.tools.policy_store import LoadedPolicy, parse_policy

URN = "urn:release-coord:acme:prod:aks-1:shop"
ORDER, FRONT, RABBIT, PAYMENTS = (
    f"{URN}:{name}" for name in ("order-service", "store-front", "rabbitmq", "payments")
)
STRIPE = unresolved_entity("api.stripe.com")
WORKSPACE = "ws-prod"
SCAN = ScanContext(
    environment="prod",
    cluster="aks-1",
    namespaces=["shop"],
    telemetry_workspace_id=WORKSPACE,
    telemetry_cluster=TelemetryClusterFilter(value="aks-1"),
)
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 2, tzinfo=timezone.utc)


def _edge(
    source: str, target: str, observed_at: datetime = T0, provenance: str = "azure_monitor"
) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
        confidence_score=0.0 if provenance == "mock_fixture" else 1.0,
        last_observed_at=observed_at,
        provenance=provenance,
    )


GRAPH = KnowledgeGraph(
    services=[
        ServiceNode(service_id=service_id, name=service_id.rsplit(":", 1)[1])
        for service_id in (ORDER, FRONT, RABBIT, PAYMENTS)
    ],
    edges=[
        # Known from static code only: still part of the baseline.
        _edge(FRONT, ORDER).model_copy(
            update={"discovery_methods": [DiscoveryMethod.STATIC_CODE]}
        ),
        _edge(ORDER, RABBIT),
    ],
    unresolved_entities=[STRIPE],
    scan=SCAN,
)
RADIUS = {ORDER, FRONT, RABBIT}
CONTEXT = baseline_identity_context("acme", GRAPH)
POLICY_YAML = """\
policy_version: 1
tenant: acme
project_id: shop
environment: prod
cluster: aks-1
required_sources: [STATIC_CODE]
runtime_evidence: live
telemetry_cluster:
  value: aks-1
exemptions:
  dependencies:
    - entity_id: "unresolved:api.stripe.com"
      reason: "External payment API"
"""
POLICY = LoadedPolicy(
    policy=parse_policy(POLICY_YAML, BaselineScope(tenant="acme", project_id="shop")),
    sha256="0" * 64,
)


class ResolveLiveEdgesTests(unittest.TestCase):
    def test_context_comes_from_the_snapshot(self) -> None:
        self.assertEqual(
            (CONTEXT.environment, CONTEXT.cluster, CONTEXT.default_namespace),
            ("prod", "aks-1", "shop"),
        )
        legacy = baseline_identity_context(
            "acme", GRAPH.model_copy(update={"scan": None})
        )
        self.assertEqual(
            (legacy.environment, legacy.cluster, legacy.default_namespace),
            (UNKNOWN_LABEL, UNKNOWN_LABEL, None),
        )

    def test_raw_names_resolve_like_discovery(self) -> None:
        observed = resolve_live_edges(
            GRAPH,
            CONTEXT,
            [
                _edge("store-front", "order-service"),
                _edge("order-service", "payments.shop.svc.cluster.local:8080"),
                _edge("order-service", "API.Stripe.com"),
                _edge("order-service", "order-service"),
            ],
        )

        self.assertEqual(
            [(edge.source_id, edge.target_id) for edge in observed],
            [(FRONT, ORDER), (ORDER, PAYMENTS), (ORDER, STRIPE.entity_id)],
        )

    def test_duplicates_keep_the_latest_observation(self) -> None:
        naive_later = T1.replace(tzinfo=None)
        observed = resolve_live_edges(
            GRAPH,
            CONTEXT,
            [
                _edge("order-service", "payments", T0),
                _edge("order-service", "payments", naive_later),
                _edge("order-service", "payments", T0),
            ],
        )
        self.assertEqual(
            observed,
            [ObservedEdge(source_id=ORDER, target_id=PAYMENTS, last_observed_at=T1)],
        )


class UnbaselinedEdgesTests(unittest.TestCase):
    def _drift(self, *pairs: tuple[str, str], exempt: set[str] | None = None) -> list[tuple[str, str]]:
        observed = [
            ObservedEdge(source_id=source, target_id=target, last_observed_at=T0)
            for source, target in pairs
        ]
        return [
            (edge.source_id, edge.target_id)
            for edge in unbaselined_edges(GRAPH, RADIUS, observed, exempt or set())
        ]

    def test_known_edges_are_not_drift_whatever_found_them(self) -> None:
        self.assertEqual(self._drift((FRONT, ORDER), (ORDER, RABBIT)), [])

    def test_new_edges_touching_the_radius_are_drift(self) -> None:
        ledger = unresolved_entity("ledger").entity_id
        self.assertEqual(
            self._drift((ORDER, PAYMENTS), (PAYMENTS, FRONT), (RABBIT, ORDER), (ledger, RABBIT)),
            [(ORDER, PAYMENTS), (PAYMENTS, FRONT), (RABBIT, ORDER), (ledger, RABBIT)],
        )

    def test_edges_outside_the_radius_are_ignored(self) -> None:
        ledger = unresolved_entity("ledger").entity_id
        self.assertEqual(self._drift((PAYMENTS, ledger)), [])

    def test_exempt_unresolved_dependencies_are_not_drift(self) -> None:
        pair = (ORDER, STRIPE.entity_id)
        self.assertEqual(self._drift(pair), [pair])
        self.assertEqual(self._drift(pair, exempt={STRIPE.entity_id}), [])

    def test_drift_issue_names_every_edge(self) -> None:
        self.assertIsNone(drift_issue("snap", []))
        edges = [
            ObservedEdge(source_id=ORDER, target_id=PAYMENTS, last_observed_at=T0),
            ObservedEdge(source_id=FRONT, target_id=RABBIT, last_observed_at=T0),
        ]
        issue = drift_issue("snap-1", edges)
        assert issue is not None
        self.assertIs(issue.code, ReasonCode.BASELINE_DRIFT)
        self.assertIn("snap-1", issue.detail)
        self.assertIn(f"{ORDER} -> {PAYMENTS}", issue.detail)
        self.assertIn(f"{FRONT} -> {RABBIT}", issue.detail)


class WorkspaceMismatchTests(unittest.TestCase):
    def test_only_the_baselines_own_workspace_is_comparable(self) -> None:
        self.assertIsNone(workspace_mismatch_issue(GRAPH, WORKSPACE))
        no_workspace = GRAPH.model_copy(
            update={"scan": SCAN.model_copy(update={"telemetry_workspace_id": ""})}
        )
        for name, graph in [
            ("other workspace", GRAPH),
            ("baseline queried none", no_workspace),
            ("legacy snapshot", GRAPH.model_copy(update={"scan": None})),
        ]:
            with self.subTest(name):
                issue = workspace_mismatch_issue(graph, "ws-staging")
                assert issue is not None
                self.assertIs(issue.code, ReasonCode.DISCOVERY_GAP)
                self.assertIn("ws-staging", issue.detail)


class TelemetryNodeDriftTests(unittest.TestCase):
    def _run(
        self,
        result: DiscoveryResult[DependencyEdge],
        workspace_id: str = WORKSPACE,
        is_mock: bool = False,
        graph: KnowledgeGraph | None = GRAPH,
    ) -> tuple[TelemetryStatus, list[ReasonCode], MagicMock]:
        state: Any = {
            "workspace_id": workspace_id,
            "tenant": "acme",
            "project_id": "shop",
            "project_policy": POLICY,
            "baseline_snapshot_id": "snap-1",
            "baseline_graph": graph,
            "target_services": [ORDER],
            "upstream_services": [FRONT],
            "downstream_services": [RABBIT],
        }
        # Live rows are observed at T0 or T1; assess shortly after.
        with patch.object(
            coordinator_nodes, "TelemetryExtractor"
        ) as extractor_cls, patch.object(
            coordinator_nodes, "utc_now", return_value=T1 + timedelta(minutes=5)
        ):
            extractor = extractor_cls.return_value
            extractor.is_mock = is_mock
            extractor.get_active_dependencies.return_value = result
            extractor.get_runtime_metrics.return_value = {}
            extractor.dropped_records = {}
            update = coordinator_nodes.validate_runtime_telemetry_node(state)
        status = update["telemetry_status"]
        assert isinstance(status, TelemetryStatus)
        issues = update["discovery_issues"]
        assert isinstance(issues, list)
        return status, [issue.code for issue in issues], extractor

    def test_new_edge_in_radius_holds_the_release(self) -> None:
        result = DiscoveryResult.success(
            [
                _edge("store-front", "order-service", T1),
                _edge("order-service", "payments", T1),
                _edge("order-service", "api.stripe.com", T1),
                _edge("order-service", "rabbitmq", T1),
            ]
        )

        status, codes, _ = self._run(result)

        self.assertIs(status.runtime_evidence, RuntimeEvidenceState.LIVE)
        self.assertEqual(codes, [ReasonCode.BASELINE_DRIFT])
        # The exempt Stripe dependency is not drift.
        self.assertEqual(
            [(edge.source_id, edge.target_id) for edge in status.unbaselined_edges],
            [(ORDER, PAYMENTS)],
        )

    def test_matching_live_edges_add_no_issue(self) -> None:
        status, codes, _ = self._run(
            DiscoveryResult.success(
                [_edge("order-service", "rabbitmq", T1), _edge("store-front", "order-service", T1)]
            )
        )
        self.assertEqual(codes, [])
        self.assertEqual(status.unbaselined_edges, [])
        self.assertIs(status.runtime_evidence, RuntimeEvidenceState.LIVE)
        self.assertEqual(
            status.runtime_observed_edges,
            [f"{ORDER} -> {RABBIT}", f"{FRONT} -> {ORDER}"],
        )

    def test_incomplete_query_still_reports_the_drift_it_saw(self) -> None:
        result = DiscoveryResult.from_items(
            [_edge("order-service", "payments")], ["skipped 2 malformed row(s)"]
        )

        status, codes, _ = self._run(result)

        self.assertIs(status.runtime_evidence, RuntimeEvidenceState.LIVE_INCOMPLETE)
        self.assertEqual(
            codes,
            [
                ReasonCode.DISCOVERY_GAP,
                ReasonCode.BASELINE_DRIFT,
                ReasonCode.REQUIRED_SOURCE_MISSING,
            ],
        )

    def test_mock_edges_are_never_compared(self) -> None:
        status, codes, _ = self._run(
            DiscoveryResult.success([_edge("order-service", "payments")]), is_mock=True
        )
        self.assertNotIn(ReasonCode.BASELINE_DRIFT, codes)
        self.assertIn(ReasonCode.DEMO_EVIDENCE, codes)
        self.assertEqual(status.unbaselined_edges, [])

    def test_other_workspace_is_a_gap_not_drift(self) -> None:
        status, codes, _ = self._run(
            DiscoveryResult.success([_edge("order-service", "payments")]),
            workspace_id="ws-staging",
        )
        # Partial coverage is reported too; the edges are never compared.
        self.assertEqual(codes, [ReasonCode.DISCOVERY_GAP, ReasonCode.REQUIRED_SOURCE_MISSING])
        self.assertEqual(status.unbaselined_edges, [])

    def test_no_baseline_means_nothing_to_compare(self) -> None:
        status, codes, _ = self._run(
            DiscoveryResult.success([_edge("order-service", "payments")]), graph=None
        )
        # Without a baseline (already BASELINE_UNAVAILABLE) live edges can't
        # be resolved, so they can't count as covering the radius either.
        self.assertIs(status.runtime_evidence, RuntimeEvidenceState.LIVE_UNCOVERED)
        self.assertEqual(codes, [ReasonCode.REQUIRED_SOURCE_MISSING])
        self.assertEqual(status.unbaselined_edges, [])

    def test_assessment_evidence_cites_the_drift(self) -> None:
        status, _, _ = self._run(
            DiscoveryResult.success([_edge("order-service", "payments")])
        )
        state: Any = {
            "pull_request": _pull_request(),
            "telemetry_status": status,
            "baseline_graph": GRAPH,
        }

        evidence = coordinator_nodes.assessment_evidence(state)

        self.assertEqual(evidence.unbaselined_edges, status.unbaselined_edges)
        self.assertEqual(
            (evidence.baseline_environment, evidence.baseline_cluster), ("prod", "aks-1")
        )


def _pull_request() -> PullRequestPayload:
    return PullRequestPayload(
        pr_id="PR-1",
        title="t",
        target_service=ORDER,
        change_type=ContractChange.ADDITIVE,
        diff="d",
    )


if __name__ == "__main__":
    unittest.main()
