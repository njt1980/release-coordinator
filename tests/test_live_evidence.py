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
from src.core.models import (
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
from src.tools.k8s_extractor import _container_env, _service_environments
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


def _edge(source: str, target: str, observed_at: datetime = FRESH) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
        confidence_score=1.0,
        last_observed_at=observed_at,
        provenance="azure_monitor",
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
            "": DropReason.NO_HOST,
            "127.0.0.1:5432": DropReason.LOOPBACK,
            "localhost:80": DropReason.LOOPBACK,
            "[::1]:80": DropReason.LOOPBACK,
            "makeline-service:56100": DropReason.EPHEMERAL_PORT,
            "https://Registry.NPMjs.org:443/npm?q=1": "registry.npmjs.org:443",
            "http://ledger:8080/x": "ledger:8080",
            "Rabbitmq:5672": "rabbitmq:5672",
            "order-service": "order-service",
            "[2001:db8::1]:8080": "[2001:db8::1]:8080",
            "nodeport-svc:32767": "nodeport-svc:32767",
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
                _edge("rabbitmq", "order:41000"),
                _edge("rabbitmq", "order:41001"),
            ]
        )
        self.assertEqual([e.target_service_id for e in kept], ["rabbitmq:5672"])
        self.assertEqual(dropped, {"no_host": 1, "loopback": 1, "ephemeral_port": 2})


def _table(rows: list[list[object]]) -> LogsTable:
    columns = ["Source", "SourceNamespace", "Target", "LastObservedAt", "Calls", "Failures"]
    return LogsTable(
        name="PrimaryResult",
        columns=columns,
        columns_types=["string", "string", "string", "datetime", "long", "long"],
        rows=rows,
    )


def _azure_extractor(response: object) -> TelemetryExtractor:
    extractor = TelemetryExtractor.__new__(TelemetryExtractor)
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
        self.assertIn("Calls=count()", query)
        self.assertIn("Failures=countif(Success == false)", query)

    def test_metrics_and_drops_come_from_the_query(self) -> None:
        extractor = _azure_extractor(
            LogsQueryResult(
                tables=[
                    _table(
                        [
                            ["order", "shop", "rabbitmq:5672", "2026-11-13T11:55:00Z", 90, 9],
                            ["order", "shop", "GET", "2026-11-13T11:55:00Z", 10, 1],
                            ["rabbitmq", "shop", "order:45000", "2026-11-13T11:55:00Z", 5, 0],
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
        self.assertEqual(extractor.dropped_records, {"no_host": 1, "ephemeral_port": 1})
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
            days_back=3, cluster_filter=POLICY.telemetry_cluster
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

    def test_unset_env_var_is_accepted_only_with_live_evidence(self) -> None:
        reach = UnresolvedReach(node="unresolved:env:order/db_url", detail="db_url unset")
        covered = [_edge("shop/front", "order"), _edge("shop/order", "rabbitmq:5672")]

        live, _ = self._run(covered, unset_env_reaches=[reach])
        self.assertEqual(live["discovery_issues"], [])
        (exemption,) = live["exempted_dependencies"]  # type: ignore[misc]
        self.assertEqual(exemption.subject, reach.node)

        stale, _ = self._run(
            [_edge("shop/front", "order", NOW - timedelta(hours=2))], unset_env_reaches=[reach]
        )
        codes = [issue.code for issue in stale["discovery_issues"]]  # type: ignore[union-attr]
        self.assertEqual(codes, [ReasonCode.REQUIRED_SOURCE_MISSING, ReasonCode.DISCOVERY_GAP])
        self.assertIn("live_stale", stale["discovery_issues"][1].detail)  # type: ignore[index]
        self.assertEqual(stale["exempted_dependencies"], [])

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


class RadiusDeferralTests(unittest.TestCase):
    def test_unset_env_vars_in_the_radius_are_deferred(self) -> None:
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
        state: Any = {"tenant": "acme", "project_id": "shop", "project_policy": LOADED, "target_services": [ORDER]}
        with patch.object(coordinator_nodes, "load_baseline", return_value=(baseline, None)):
            update = coordinator_nodes.calculate_static_radius_node(state)

        self.assertEqual([r.node for r in update["unset_env_reaches"]], [unset.entity_id])  # type: ignore[union-attr]
        gaps = [i.detail for i in update["discovery_issues"] if i.code is ReasonCode.DISCOVERY_GAP]  # type: ignore[union-attr]
        self.assertEqual(len(gaps), 1)
        self.assertIn(unknown.entity_id, gaps[0])


def _env_var(name: str, value: str | None = None, secret: bool = False) -> SimpleNamespace:
    value_from = SimpleNamespace(config_map_key_ref=None, secret_key_ref=object()) if secret else None
    return SimpleNamespace(name=name, value=value, value_from=value_from)


def _spec(*env: SimpleNamespace, env_from: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    return SimpleNamespace(containers=[SimpleNamespace(env=list(env), env_from=env_from or [])])


class UnsetEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        logger = __import__("logging").getLogger("src.tools.k8s_extractor")
        previous = logger.level
        logger.setLevel(100)
        self.addCleanup(logger.setLevel, previous)

    def test_container_env_tracks_opaque_names_and_sources(self) -> None:
        env = _container_env(_spec(_env_var("A", "1"), _env_var("TOKEN", secret=True)), {})
        self.assertEqual((env.pairs, env.opaque, env.has_opaque_sources), ([("A", "1")], {"TOKEN"}, False))
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
        service = SimpleNamespace(metadata=SimpleNamespace(name="order"), spec=SimpleNamespace(selector={"app": "order"}))
        templates = [({"app": "order"}, _spec(_env_var("A", "1"))), ({"app": "order"}, _spec(_env_var("A", "2")))]
        (environment,) = _service_environments(
            [service], templates, {}, "shop", IdentityContext(tenant="acme", environment="prod", cluster="aks-1")
        )
        self.assertEqual((environment.variables, environment.opaque_variables), ({}, ["A"]))

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
            days_back=3, cluster_filter=POLICY.telemetry_cluster
        )
        (report,) = result["discovery_reports"]  # type: ignore[misc]
        self.assertEqual(report.dropped_records, {"no_host": 3})


if __name__ == "__main__":
    unittest.main()
