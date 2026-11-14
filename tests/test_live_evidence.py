"""Tests for trustworthy live evidence and the 2a-2 policy features.

Covers: backend telemetry normalization (G1), live evidence states and
coverage (G13), real per-service metrics, egress rules (G2), the
configurable lookback (G3), and unset environment variables (G6).
"""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from azure.monitor.query import LogsQueryResult, LogsTable
from pydantic import ValidationError

from src.coordinator import nodes as coordinator_nodes
from src.coordinator.project_policy import (
    dependency_exemption,
    egress_exemption,
    live_coverage,
    live_runtime_state,
)
from src.discovery_engine.nodes import _merge_edge
from src.core.models import (
    TargetDetail,
    TargetDetailKind,
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
    ServiceEnvironment,
    ServiceNode,
    TelemetryClusterFilter,
    TelemetryStatus,
    UnresolvedEntity,
    UnresolvedEntityKind,
    UnresolvedReach,
)
from src.discovery_engine import nodes as discovery_nodes
from src.tools.baseline_store import BaselineScope, StoredBaseline
from src.tools.k8s_extractor import PodTemplate, _container_env, _service_environment
from src.tools.policy_store import LoadedPolicy, ProjectPolicy, parse_policy
from src.tools.telemetry_extractor import TelemetryExtractor, build_dependency_query
from src.tools.telemetry_normalization import DropReason, normalize_edges, normalize_target
from src.core.service_ids import IdentityContext

URN = "urn:release-coord:acme:prod:aks-1:shop"
ORDER, FRONT, RABBIT, PAYMENTS = (f"{URN}:{n}" for n in ("order", "front", "rabbitmq", "payments"))
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
FRESH = NOW - timedelta(minutes=5)
WORKSPACE = "ws-1"

POLICY_YAML = """\
policy_version: 4
tenant: acme
project_id: shop
environment: prod
cluster: aks-1
required_sources: [STATIC_CODE]
runtime_evidence: live
runtime_lookback_days: 3
max_telemetry_age_minutes: 30
telemetry_cluster:
  value: aks-1
egress:
  - host: registry.npmjs.org
    reason: npm update check
  - host_suffix: .blob.core.windows.net
    reason: storage
  - cidr: 10.20.0.0/16
    reason: peered vnet
exemptions:
  silent_services:
    - service: shop/payments
      reason: called only at checkout
      expires_on: 2026-12-31
"""
POLICY = parse_policy(POLICY_YAML, BaselineScope(tenant="acme", project_id="shop"))
LOADED = LoadedPolicy(policy=POLICY, sha256="0" * 64)


def _edge(
    source: str,
    target: str,
    observed_at: datetime = FRESH,
    dependency_type: str | None = None,
    direction_uncertain: bool = False,
) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
        confidence_score=1.0,
        last_observed_at=observed_at,
        provenance="azure_monitor",
        dependency_type=dependency_type,
        direction_uncertain=direction_uncertain,
    )


GRAPH = KnowledgeGraph(
    services=[ServiceNode(service_id=s, name=s.rsplit(":", 1)[1]) for s in (ORDER, FRONT, RABBIT, PAYMENTS)],
    edges=[_edge(FRONT, ORDER), _edge(ORDER, RABBIT), _edge(ORDER, PAYMENTS)],
    scan=ScanContext(
        environment="prod",
        cluster="aks-1",
        namespaces=["shop"],
        telemetry_workspace_id=WORKSPACE,
        telemetry_cluster=TelemetryClusterFilter(value="aks-1"),
    ),
)


class NormalizationTests(unittest.TestCase):
    def test_targets_are_normalized_or_dropped_with_a_reason(self) -> None:
        cases = {
            "GET": DropReason.NO_HOST,
            "CONNECT": DropReason.NO_HOST,
            "/npm": DropReason.NO_HOST,
            "find orderdb.orders": DropReason.NO_HOST,
            "": DropReason.NO_HOST,
            "127.0.0.1:5432": DropReason.LOOPBACK,
            "localhost:80": DropReason.LOOPBACK,
            "[::1]:80": DropReason.LOOPBACK,
            # A high port alone proves nothing about direction (review #1).
            "makeline-service:56100": "makeline-service:56100",
            "payments:50051": "payments:50051",
            "database:40000": "database:40000",
            "https://Registry.NPMjs.org:443/npm?q=1": "registry.npmjs.org:443",
            "http://ledger:8080/x": "ledger:8080",
            "Rabbitmq:5672": "rabbitmq:5672",
            "order-service": "order-service",
            "[2001:db8::1]:8080": "[2001:db8::1]:8080",
            "nodeport-svc:32767": "nodeport-svc:32767",
            # Application Insights writes details after " | " (topic 03, 2026-11-14).
            "basket-api | cid-v1:7f3c0000-aaaa": "basket-api",
            "tcp:sql.database.windows.net,1433 | orders": "sql.database.windows.net:1433",
            "sqlhost\\SQLEXPRESS | orders": "sqlhost",
            "sb://bus.servicebus.windows.net/ | orders": "bus.servicebus.windows.net",
            "localhost | orders": DropReason.LOOPBACK,
            " | orders": DropReason.NO_HOST,
            # A service named like a SqlClient protocol keeps its host.
            "admin:8080": "admin:8080",
        }
        for target, expected in cases.items():
            with self.subTest(target=target):
                self.assertEqual(normalize_target(target), expected)

    def test_edges_are_rewritten_and_drops_counted(self) -> None:
        kept, dropped = normalize_edges(
            [
                _edge("shop/order", "http://rabbitmq:5672/"),
                _edge("shop/order", "GET"),
                _edge("shop/order", "localhost"),
                _edge("rabbitmq", "order:41000", dependency_type="Queue Message | rabbitmq"),
                _edge("rabbitmq", "order:41001", dependency_type="AMQP"),
            ]
        )
        self.assertEqual([e.target_service_id for e in kept], ["rabbitmq:5672"])
        self.assertEqual(dropped, {"no_host": 1, "loopback": 1, "reversed_delivery": 2})

    def test_app_insights_details_travel_on_the_edge(self) -> None:
        kept, dropped = normalize_edges(
            [
                _edge("shop/order", "basket-api | cid-v1:7f3c", dependency_type="Http (tracked component)"),
                _edge("shop/order", "tcp:sql.example.net,1433 | orders", dependency_type="SQL"),
                _edge("shop/order", "sb://bus.servicebus.windows.net/ | orders", dependency_type="Azure Service Bus"),
                _edge("shop/order", "bus.servicebus.windows.net/payments", dependency_type="Microsoft.ServiceBus"),
                # An HTTP path is not an entity.
                _edge("shop/order", "ledger:8080/api/v1", dependency_type="HTTP"),
            ]
        )
        self.assertEqual(dropped, {})
        component = TargetDetail(kind=TargetDetailKind.APP_COMPONENT, name="7f3c")
        orders = TargetDetail(kind=TargetDetailKind.RESOURCE, name="orders")
        payments = TargetDetail(kind=TargetDetailKind.RESOURCE, name="payments")
        self.assertEqual(
            [(e.target_service_id, e.target_details) for e in kept],
            [
                ("basket-api", [component]),
                ("sql.example.net:1433", [orders]),
                ("bus.servicebus.windows.net", [orders]),
                ("bus.servicebus.windows.net", [payments]),
                ("ledger:8080", None),
            ],
        )

    def test_merged_edges_union_target_details(self) -> None:
        orders = TargetDetail(kind=TargetDetailKind.RESOURCE, name="orders")
        payments = TargetDetail(kind=TargetDetailKind.RESOURCE, name="payments")
        first = _edge("shop/order", "bus").model_copy(update={"target_details": [payments]})
        second = _edge("shop/order", "bus").model_copy(update={"target_details": [orders, payments]})
        merged = _merge_edge(first, second)
        self.assertEqual(merged.target_details, [orders, payments])
        self.assertIsNone(_merge_edge(_edge("a", "b"), _edge("a", "b")).target_details)

    def test_high_port_calls_are_kept_and_qualified(self) -> None:
        kept, dropped = normalize_edges(
            [
                _edge("shop/order", "payments:50051", dependency_type="gRPC"),
                _edge("shop/order", "database:40000"),
                _edge("shop/order", "payments:8080", dependency_type="HTTP"),
            ]
        )
        self.assertEqual(dropped, {})
        self.assertEqual(
            [(e.target_service_id, e.direction_uncertain) for e in kept],
            [("payments:50051", True), ("database:40000", True), ("payments:8080", False)],
        )


def _table(rows: list[list[object]]) -> LogsTable:
    columns = [
        "Source", "SourceNamespace", "Target", "DependencyType", "LastObservedAt", "Calls", "Failures"
    ]
    return LogsTable(
        name="PrimaryResult",
        columns=columns,
        columns_types=["string", "string", "string", "string", "datetime", "long", "long"],
        rows=rows,
    )


def _azure_extractor(response: object) -> TelemetryExtractor:
    extractor = TelemetryExtractor.__new__(TelemetryExtractor)
    extractor._additional_workspaces = []
    extractor._workspace_id = WORKSPACE
    extractor._source = "azure"
    extractor._runtime_metrics = {}
    extractor._dropped_records = {}
    extractor._credential = None
    client = MagicMock()
    client.query_workspace.return_value = response
    extractor._client = client
    return extractor


class ExtractorTests(unittest.TestCase):
    def test_query_counts_calls_and_failures(self) -> None:
        query = build_dependency_query(7)
        # Each sampled row stands for ItemCount calls.
        self.assertIn("Calls = sum(ItemCount)", query)
        self.assertIn("Failures = sumif(ItemCount, Success == false)", query)
        self.assertIn(
            "by Source = SourceName, SourceNamespace = SourceNamespace, Target, DependencyType, Noise",
            query,
        )

    def test_metrics_and_drops_come_from_the_query(self) -> None:
        extractor = _azure_extractor(
            LogsQueryResult(
                tables=[
                    _table(
                        [
                            ["order", "shop", "rabbitmq:5672", "AMQP", "2026-11-13T11:55:00Z", 90, 9],
                            ["order", "shop", "GET", "HTTP", "2026-11-13T11:55:00Z", 10, 1],
                            ["rabbitmq", "shop", "order:45000", "Queue Message | rabbitmq",
                             "2026-11-13T11:55:00Z", 5, 0],
                        ]
                    )
                ]
            )
        )
        result = extractor.get_active_dependencies(days_back=3)

        self.assertEqual(
            [(e.source_service_id, e.target_service_id) for e in result.items],
            [("shop/order", "rabbitmq:5672")],
        )
        self.assertEqual(extractor.dropped_records, {"no_host": 1, "reversed_delivery": 1})
        self.assertEqual(result.items[0].dependency_type, "AMQP")
        self.assertEqual(
            extractor.get_runtime_metrics(),
            {
                "shop/order": {"request_count": 100, "error_rate": 0.1},
                "shop/rabbitmq": {"request_count": 5, "error_rate": 0.0},
            },
        )


class PolicyFieldTests(unittest.TestCase):
    def _policy(self, **changes: object) -> ProjectPolicy:
        data: dict[str, object] = {
            "policy_version": 4,
            "tenant": "acme",
            "project_id": "shop",
            "environment": "prod",
            "cluster": "aks-1",
            "required_sources": ["STATIC_CODE"],
            "runtime_evidence": "baseline",
        }
        data.update(changes)
        return ProjectPolicy.model_validate(data)

    def test_defaults(self) -> None:
        policy = self._policy()
        self.assertEqual(
            (policy.runtime_lookback_days, policy.max_telemetry_age_minutes, policy.egress),
            (7, 60, []),
        )

    def test_bounds_and_rule_shapes_are_enforced(self) -> None:
        for changes in [
            {"runtime_lookback_days": 0},
            {"runtime_lookback_days": 31},
            {"max_telemetry_age_minutes": 4},
            {"max_telemetry_age_minutes": 1441},
            {"egress": [{"host": "*.npmjs.org", "reason": "x"}]},
            {"egress": [{"host": "registry.npmjs.org:443", "reason": "x"}]},
            {"egress": [{"host_suffix": "npmjs.org", "reason": "x"}]},
            {"egress": [{"cidr": "0.0.0.0/0", "reason": "x"}]},
            {"egress": [{"cidr": "10.20.0.1/16", "reason": "x"}]},
            {"egress": [{"host": "a.com", "cidr": "10.0.0.0/8", "reason": "x"}]},
            {"egress": [{"host": "a.com"}]},
            {"exemptions": {"silent_services": [{"service": "payments", "reason": "x", "expires_on": "2026-12-31"}]}},
            {"exemptions": {"silent_services": [{"service": "shop/payments", "reason": "x"}]}},
            {"exemptions": {"silent_services": [{"service": "shop/payments", "reason": "x", "expires_on": "2026-12-31", "max_quiet_days": 8}]}},
            {
                "exemptions": {
                    "silent_services": [
                        {"service": "shop/a", "reason": "x", "expires_on": "2026-12-31"},
                        {"service": "shop/a", "reason": "y", "expires_on": "2026-12-31"},
                    ]
                }
            },
        ]:
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    self._policy(**changes)


class EgressTests(unittest.TestCase):
    def test_rules_match_by_entity_kind(self) -> None:
        cases = {
            "unresolved:registry.npmjs.org:443": "egress host registry.npmjs.org",
            "unresolved:registry.npmjs.org": "egress host registry.npmjs.org",
            "unresolved:acct.blob.core.windows.net": "egress host_suffix .blob.core.windows.net",
            "unresolved:10.20.3.4:5432": "egress cidr 10.20.0.0/16",
            # An IP-only sighting of npm is not allowed by the host rule.
            "unresolved:104.16.8.34:443": None,
            "unresolved:evil-registry.npmjs.org.example.com": None,
            "unresolved:blob.core.windows.net": None,
            # Service-shaped names and env vars are never egress.
            "unresolved:rabbitmq": None,
            "unresolved:env:order/db_url": None,
            "urn:release-coord:acme:prod:aks-1:shop:order": None,
        }
        for entity_id, rule in cases.items():
            with self.subTest(entity_id=entity_id):
                exemption = egress_exemption(POLICY, entity_id)
                self.assertEqual(exemption.rule if exemption else None, rule)
        self.assertIsNone(egress_exemption(None, "unresolved:registry.npmjs.org"))

    def test_dependency_exemption_falls_back_to_egress(self) -> None:
        exemption = dependency_exemption(POLICY, "unresolved:registry.npmjs.org:443")
        assert exemption is not None
        self.assertEqual(exemption.reason, "npm update check")


def _observed(source: str, target: str, at: datetime = FRESH) -> ObservedEdge:
    return ObservedEdge(source_id=source, target_id=target, last_observed_at=at)


class LiveStateTests(unittest.TestCase):
    def test_states_in_order_of_precedence(self) -> None:
        radius = {ORDER, FRONT, RABBIT}
        stale = NOW - timedelta(minutes=31)
        cases = [
            ([], RuntimeEvidenceState.LIVE_EMPTY, "no usable rows"),
            ([_observed(FRONT, ORDER, stale)], RuntimeEvidenceState.LIVE_STALE, "31 minutes old"),
            ([_observed(FRONT, ORDER)], RuntimeEvidenceState.LIVE_UNCOVERED, RABBIT),
            ([_observed(FRONT, ORDER), _observed(ORDER, RABBIT)], RuntimeEvidenceState.LIVE, ""),
        ]
        for observed, expected, why_part in cases:
            with self.subTest(expected=expected):
                coverage = live_coverage(POLICY, observed, radius, NOW)
                state, why = live_runtime_state(POLICY, coverage, NOW)
                self.assertIs(state, expected)
                self.assertIn(why_part, why)

    def test_freshness_is_judged_per_service(self) -> None:
        # The order -> front edge is fresh, but rabbitmq was last seen two
        # days ago: one busy service must not make a quiet one look fresh.
        two_days = NOW - timedelta(days=2)
        coverage = live_coverage(
            POLICY,
            [_observed(FRONT, ORDER), _observed(ORDER, RABBIT, two_days)],
            {ORDER, FRONT, RABBIT},
            NOW,
        )
        self.assertEqual(coverage.observed_services, sorted([FRONT, ORDER]))
        self.assertEqual(coverage.uncovered_services, [RABBIT])
        self.assertEqual(coverage.service_last_seen[RABBIT], two_days)
        state, why = live_runtime_state(POLICY, coverage, NOW)
        self.assertIs(state, RuntimeEvidenceState.LIVE_UNCOVERED)
        self.assertIn(f"{RABBIT} (last seen 2 days ago)", why)

    def test_allowlisted_silent_services_dont_count_as_uncovered(self) -> None:
        coverage = live_coverage(POLICY, [_observed(FRONT, ORDER)], {FRONT, ORDER, PAYMENTS}, NOW)
        self.assertEqual(coverage.uncovered_services, [])
        self.assertEqual(coverage.silent_services, [PAYMENTS])
        self.assertEqual(coverage.observed_services, sorted([FRONT, ORDER]))
        self.assertIs(live_runtime_state(POLICY, coverage, NOW)[0], RuntimeEvidenceState.LIVE)

    def test_max_quiet_days_bounds_the_allowance(self) -> None:
        policy = POLICY.model_copy(
            update={
                "exemptions": POLICY.exemptions.model_copy(
                    update={
                        "silent_services": [
                            POLICY.exemptions.silent_services[0].model_copy(
                                update={"max_quiet_days": 2}
                            )
                        ]
                    }
                )
            }
        )
        radius = {FRONT, ORDER, PAYMENTS}
        for last, expected in [
            (NOW - timedelta(days=1), "silent"),
            (NOW - timedelta(days=3), "uncovered"),
            (None, "uncovered"),
        ]:
            with self.subTest(last=last):
                observed = [_observed(FRONT, ORDER)]
                if last is not None:
                    observed.append(_observed(ORDER, PAYMENTS, last))
                coverage = live_coverage(policy, observed, radius, NOW)
                self.assertEqual(
                    PAYMENTS in coverage.silent_services, expected == "silent"
                )
                self.assertEqual(
                    PAYMENTS in coverage.uncovered_services, expected == "uncovered"
                )

    def test_expired_entries_are_ignored_and_reported(self) -> None:
        later = NOW.replace(year=2027)
        coverage = live_coverage(
            POLICY, [_observed(FRONT, ORDER, later)], {FRONT, ORDER, PAYMENTS}, later
        )
        self.assertEqual(coverage.uncovered_services, [PAYMENTS])
        self.assertEqual(coverage.expired_exemptions, [PAYMENTS])
        state, why = live_runtime_state(POLICY, coverage, later)
        self.assertIs(state, RuntimeEvidenceState.LIVE_UNCOVERED)
        self.assertIn("Expired silent-service entries were ignored", why)


class TelemetryNodeTests(unittest.TestCase):
    def _run(
        self,
        edges: list[DependencyEdge],
        metrics: dict[str, dict[str, float]] | None = None,
        **state_overrides: object,
    ) -> tuple[dict[str, object], MagicMock]:
        state: dict[str, object] = {
            "workspace_id": WORKSPACE,
            "tenant": "acme",
            "project_id": "shop",
            "project_policy": LOADED,
            "baseline_snapshot_id": "snap",
            "baseline_graph": GRAPH,
            "target_services": [ORDER],
            "upstream_services": [FRONT],
            "downstream_services": [RABBIT],
        }
        state.update(state_overrides)
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls, patch.object(
            coordinator_nodes, "utc_now", return_value=NOW
        ):
            extractor = extractor_cls.return_value
            extractor.is_mock = False
            extractor.get_active_dependencies.return_value = DiscoveryResult.success(edges)
            extractor.get_runtime_metrics.return_value = metrics or {}
            extractor.dropped_records = {"loopback": 2}
            update = coordinator_nodes.validate_runtime_telemetry_node(state)  # type: ignore[arg-type]
        return update, extractor

    def test_empty_query_is_not_live_evidence(self) -> None:
        update, extractor = self._run([])
        status = update["telemetry_status"]
        assert isinstance(status, TelemetryStatus)
        self.assertIs(status.runtime_evidence, RuntimeEvidenceState.LIVE_EMPTY)
        (issue,) = update["discovery_issues"]  # type: ignore[misc]
        self.assertIs(issue.code, ReasonCode.REQUIRED_SOURCE_MISSING)
        self.assertIn("no usable rows", issue.detail)
        extractor.get_active_dependencies.assert_called_once_with(
            days_back=3, scope=POLICY.telemetry_scope
        )

    def test_covered_fresh_query_is_live_and_reports_live_edges(self) -> None:
        update, _ = self._run(
            [
                _edge("shop/front", "order"),
                _edge("shop/order", "rabbitmq:5672"),
                _edge("shop/order", "registry.npmjs.org:443"),
            ],
            metrics={"shop/order": {"request_count": 10, "error_rate": 0.1}, "other": {"request_count": 1}},
        )
        status = update["telemetry_status"]
        assert isinstance(status, TelemetryStatus)
        self.assertIs(status.runtime_evidence, RuntimeEvidenceState.LIVE)
        # The npm call is allowed egress, so it is not drift.
        self.assertEqual(update["discovery_issues"], [])
        self.assertEqual(
            status.runtime_observed_edges,
            [
                f"{FRONT} -> {ORDER}",
                f"{ORDER} -> {RABBIT}",
                f"{ORDER} -> unresolved:registry.npmjs.org:443",
            ],
        )
        self.assertEqual(status.runtime_metrics, {ORDER: {"request_count": 10.0, "error_rate": 0.1}})
        self.assertEqual(status.dropped_records, {"loopback": 2})
        assert status.coverage is not None
        self.assertEqual(status.coverage.newest_observed_at, FRESH)

    def test_live_evidence_does_not_accept_unset_env_vars(self) -> None:
        # Review #3: live traffic shows services are active, not that the
        # path reading the variable ran. Unset variables are no longer
        # deferred to the live check; it leaves them alone.
        covered = [_edge("shop/front", "order"), _edge("shop/order", "rabbitmq:5672")]
        live, _ = self._run(covered)
        self.assertEqual(live["discovery_issues"], [])
        self.assertNotIn("exempted_dependencies", live)

    def test_report_shows_live_coverage(self) -> None:
        update, _ = self._run([_edge("shop/front", "order")])
        state: Any = {
            "pull_request": PullRequestPayload(
                pr_id="1", title="t", target_service=ORDER, change_type=ContractChange.ADDITIVE, diff="d"
            ),
            "telemetry_status": update["telemetry_status"],
        }
        evidence = coordinator_nodes.assessment_evidence(state)
        assert evidence.live_coverage is not None
        self.assertEqual(evidence.live_coverage.uncovered_services, [RABBIT])


class UnsetEnvironmentRadiusTests(unittest.TestCase):
    def _radius(self, policy: LoadedPolicy = LOADED) -> dict[str, object]:
        unset = UnresolvedEntity(
            entity_id="unresolved:env:order/db_url",
            reference="env:order/db_url",
            kind=UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET,
        )
        unknown = UnresolvedEntity(
            entity_id="unresolved:env:order/queue_url",
            reference="env:order/queue_url",
            kind=UnresolvedEntityKind.ENVIRONMENT_VARIABLE,
        )
        graph = GRAPH.model_copy(
            update={
                "edges": [*GRAPH.edges, _edge(ORDER, unset.entity_id), _edge(ORDER, unknown.entity_id)],
                "unresolved_entities": [unset, unknown],
            }
        )
        baseline = StoredBaseline(
            scope=BaselineScope(tenant="acme", project_id="shop"),
            snapshot_id="20261113T110000000000Z-abcdef12",
            graph=graph,
        )
        state: Any = {"tenant": "acme", "project_id": "shop", "project_policy": policy, "target_services": [ORDER]}
        with patch.object(coordinator_nodes, "load_baseline", return_value=(baseline, None)):
            return coordinator_nodes.calculate_static_radius_node(state)

    def test_unset_env_vars_are_gaps_that_explain_why(self) -> None:
        update = self._radius()
        gaps = [i.detail for i in update["discovery_issues"] if i.code is ReasonCode.DISCOVERY_GAP]  # type: ignore[union-attr]
        self.assertEqual(len(gaps), 2)
        (unset_gap,) = [g for g in gaps if "db_url" in g]
        self.assertIn("container image", unset_gap)
        self.assertIn("exemptions.dependencies", unset_gap)

    def test_a_reviewed_exemption_accepts_an_unset_env_var(self) -> None:
        policy = parse_policy(
            POLICY_YAML.replace(
                "exemptions:\n",
                "exemptions:\n  dependencies:\n    - entity_id: unresolved:env:order/db_url\n"
                "      reason: reviewed, default points at shop/postgres\n",
            ),
            BaselineScope(tenant="acme", project_id="shop"),
        )
        update = self._radius(LoadedPolicy(policy=policy, sha256="1" * 64))
        gaps = [i.detail for i in update["discovery_issues"] if i.code is ReasonCode.DISCOVERY_GAP]  # type: ignore[union-attr]
        self.assertEqual(len(gaps), 1)
        self.assertIn("queue_url", gaps[0])
        self.assertEqual(
            [e.subject for e in update["exempted_dependencies"]],  # type: ignore[union-attr]
            ["unresolved:env:order/db_url"],
        )


def _env_var(name: str, value: str | None = None, secret: bool = False) -> SimpleNamespace:
    value_from = SimpleNamespace(config_map_key_ref=None, secret_key_ref=object()) if secret else None
    return SimpleNamespace(name=name, value=value, value_from=value_from)


def _spec(*env: SimpleNamespace, env_from: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    return SimpleNamespace(containers=[SimpleNamespace(name="app", env=list(env), env_from=env_from or [])])


def _env_from(config_map: str | None = None, secret: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        secret_ref=object() if secret else None,
        config_map_ref=SimpleNamespace(name=config_map) if config_map else None,
        prefix=None,
    )


class UnsetEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        logger = __import__("logging").getLogger("src.tools.k8s_extractor")
        previous = logger.level
        logger.setLevel(100)
        self.addCleanup(logger.setLevel, previous)

    def test_container_env_tracks_opaque_names_and_sources(self) -> None:
        env = _container_env(_spec(_env_var("A", "1"), _env_var("TOKEN", secret=True)), {})
        self.assertEqual(
            (env.pairs, env.opaque, env.has_opaque_sources), ([("A", "1", "app:env")], {"TOKEN"}, False)
        )
        for source in [
            SimpleNamespace(secret_ref=object(), config_map_ref=None),
            SimpleNamespace(secret_ref=None, config_map_ref=SimpleNamespace(name="missing")),
        ]:
            with self.subTest(source=source):
                self.assertTrue(_container_env(_spec(env_from=[source]), {}).has_opaque_sources)

    def test_is_unset_needs_proof(self) -> None:
        plain = ServiceEnvironment(service_id=ORDER, variables={"A": "1"}, opaque_variables=["TOKEN"])
        self.assertTrue(plain.is_unset("DB_URL"))
        self.assertFalse(plain.is_unset("A"))
        self.assertFalse(plain.is_unset("TOKEN"))
        self.assertFalse(
            ServiceEnvironment(service_id=ORDER, has_opaque_sources=True).is_unset("DB_URL")
        )

    def test_service_with_conflicting_values_marks_them_opaque(self) -> None:
        templates = [
            PodTemplate({"app": "order"}, "deployment/order", _spec(_env_var("A", "1"))),
            PodTemplate({"app": "order"}, "deployment/order-canary", _spec(_env_var("A", "2"))),
        ]
        with self.assertLogs("src.tools.k8s_extractor", level="WARNING"):
            environment = _service_environment(ORDER, templates, {})
        self.assertEqual((environment.variables, environment.opaque_variables), ({}, ["A"]))

    def _service_env(self, *specs: SimpleNamespace, config_maps: dict[str, dict[str, str]] | None = None) -> ServiceEnvironment:
        templates = [
            PodTemplate({"app": "order"}, f"deployment/order-{i}", spec) for i, spec in enumerate(specs)
        ]
        return _service_environment(ORDER, templates, config_maps or {})

    def test_a_readable_value_beside_an_opaque_one_is_not_definite(self) -> None:
        # Review #2, first reproduction: one container sets a literal, the
        # other the same name from a Secret.
        environment = self._service_env(
            _spec(_env_var("PAYMENTS_URL", "http://payments")),
            _spec(_env_var("PAYMENTS_URL", secret=True)),
        )
        self.assertEqual(environment.variables, {})
        self.assertEqual(environment.opaque_variables, ["PAYMENTS_URL"])
        self.assertFalse(environment.is_unset("PAYMENTS_URL"))

    def test_a_later_secret_env_from_makes_config_map_values_opaque(self) -> None:
        # Review #2, second reproduction: a Secret imported after a config
        # map may override any of its keys.
        maps = {"base": {"PAYMENTS_URL": "http://payments"}, "late": {"QUEUE": "rabbitmq"}}
        environment = self._service_env(
            _spec(env_from=[_env_from("base"), _env_from(secret=True), _env_from("late")]),
            config_maps=maps,
        )
        self.assertEqual(environment.variables, {"QUEUE": "rabbitmq"})
        self.assertEqual(environment.opaque_variables, ["PAYMENTS_URL"])
        self.assertTrue(environment.has_opaque_sources)

    def test_an_explicit_env_entry_still_wins_over_an_opaque_env_from(self) -> None:
        maps = {"base": {"PAYMENTS_URL": "http://old"}}
        environment = self._service_env(
            _spec(
                _env_var("PAYMENTS_URL", "http://payments"),
                env_from=[_env_from("base"), _env_from(secret=True)],
            ),
            config_maps=maps,
        )
        self.assertEqual(environment.variables, {"PAYMENTS_URL": "http://payments"})
        self.assertEqual(environment.variable_sources, {"PAYMENTS_URL": ["deployment/order-0/app:env"]})

    def test_reconciliation_classifies_unset_and_unknown_variables(self) -> None:
        static_order = ServiceNode(
            service_id="urn:release-coord:acme:prod:aks-1:unknown:order",
            name="order",
            metadata={"repository": "shop", "service_root": "order"},
        )
        state: Any = {
            "tenant": "acme",
            "environment": "prod",
            "cluster": "aks-1",
            "target_namespaces": ["shop"],
            "discovered_services": [static_order, ServiceNode(service_id=ORDER, name="order")],
            "service_environments": [
                ServiceEnvironment(service_id=ORDER, variables={"A": "1"}, opaque_variables=["TOKEN_URL"])
            ],
            "static_edges": [
                DependencyEdge(
                    source_service_id=static_order.service_id,
                    target_service_id=target,
                    discovery_methods=[DiscoveryMethod.STATIC_CODE],
                    confidence_score=0.7,
                    last_observed_at=NOW,
                )
                for target in ("env:order/DB_URL", "env:order/TOKEN_URL")
            ],
            "runtime_edges": [],
        }
        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)
        self.assertEqual(
            [(u.entity_id, u.kind) for u in graph["unresolved_entities"]],
            [
                ("unresolved:env:order/db_url", UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET),
                ("unresolved:env:order/token_url", UnresolvedEntityKind.ENVIRONMENT_VARIABLE),
            ],
        )


class DiscoveryLookbackTests(unittest.TestCase):
    def test_discovery_uses_the_policy_lookback_and_records_drops(self) -> None:
        store = MagicMock()
        store.load.return_value = LOADED
        with patch.object(discovery_nodes, "POLICY_STORE", store), patch.object(
            discovery_nodes, "TelemetryExtractor"
        ) as telemetry_cls:
            telemetry_cls.return_value.dropped_records = {"no_host": 3}
            telemetry_cls.return_value.get_active_dependencies.return_value = DiscoveryResult.success([])
            result = discovery_nodes.extract_runtime_telemetry(
                {"tenant": "acme", "project_id": "shop", "workspace_id": WORKSPACE, "discovery_reports": []}  # type: ignore[typeddict-item]
            )
        telemetry_cls.return_value.get_active_dependencies.assert_called_once_with(
            days_back=3, scope=POLICY.telemetry_scope, roles=None
        )
        (report,) = result["discovery_reports"]  # type: ignore[misc]
        self.assertEqual(report.dropped_records, {"no_host": 3})


if __name__ == "__main__":
    unittest.main()


class DirectionUncertaintyTests(unittest.TestCase):
    """Review #1: edges to high ports are kept, and qualified, not dropped."""

    def test_reconciliation_drops_only_reversed_sightings_of_known_edges(self) -> None:
        reply = _edge(RABBIT, ORDER, direction_uncertain=True)
        grpc = _edge(ORDER, PAYMENTS, direction_uncertain=True)
        edges = {
            (ORDER, RABBIT): _edge(ORDER, RABBIT),
            (RABBIT, ORDER): reply,
            (ORDER, PAYMENTS): grpc,
        }
        kept = discovery_nodes.drop_reversed_sightings(edges)
        self.assertEqual(
            [(e.source_service_id, e.target_service_id, e.direction_uncertain) for e in kept],
            [(ORDER, RABBIT, False), (ORDER, PAYMENTS, True)],
        )

    def test_merging_with_certain_evidence_clears_the_qualification(self) -> None:
        merged = discovery_nodes._merge_edge(
            _edge(ORDER, PAYMENTS, direction_uncertain=True), _edge(ORDER, PAYMENTS)
        )
        self.assertFalse(merged.direction_uncertain)

    def test_drift_ignores_reversed_sightings_but_flags_new_uncertain_edges(self) -> None:
        from src.coordinator.runtime_drift import drift_issue, unbaselined_edges

        observed = [
            ObservedEdge(source_id=RABBIT, target_id=ORDER, last_observed_at=FRESH, direction_uncertain=True),
            ObservedEdge(source_id=RABBIT, target_id=PAYMENTS, last_observed_at=FRESH, direction_uncertain=True),
            ObservedEdge(source_id=PAYMENTS, target_id=ORDER, last_observed_at=FRESH),
        ]
        drift = unbaselined_edges(GRAPH, {ORDER, FRONT, RABBIT, PAYMENTS}, observed, set())
        self.assertEqual(
            [(e.source_id, e.target_id) for e in drift], [(RABBIT, PAYMENTS), (PAYMENTS, ORDER)]
        )
        issue = drift_issue("snap", drift)
        assert issue is not None
        self.assertIn("direction uncertain", issue.detail)

    def test_live_edges_are_uncertain_only_if_every_sighting_is(self) -> None:
        from src.coordinator.runtime_drift import baseline_identity_context, resolve_live_edges

        context = baseline_identity_context("acme", GRAPH)
        (edge,) = resolve_live_edges(
            GRAPH,
            context,
            [
                _edge("shop/order", "payments", direction_uncertain=True),
                _edge("shop/order", "payments"),
            ],
        )
        self.assertFalse(edge.direction_uncertain)
