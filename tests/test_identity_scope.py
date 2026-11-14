"""Tests for namespace-safe identity and proven cluster scope (PR 2a-1).

Covers: source-code services that cannot be tied to exactly one identity
(AMBIGUOUS_SERVICE), namespaces declared in ``release-coord.yaml``,
dependency targets resolving in their caller's namespace, namespaced
telemetry sources, the telemetry cluster filter, and the Kubernetes API
server recorded by discovery.
"""

import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from azure.monitor.query import LogsQueryResult, LogsTable
from pydantic import ValidationError

from src import api
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.runtime_drift import resolve_live_edges
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryResult,
    DiscoveryStageReport,
    DiscoveryStatus,
    KnowledgeGraph,
    ScanContext,
    ServiceNode,
    TelemetryClusterFilter,
    TelemetryScope,
    UnresolvedEntityKind,
)
from src.core.service_ids import IdentityContext
from src.discovery_engine import nodes as discovery_nodes
from src.tools.baseline_store import BaselineScope
from src.tools.devops_extractor import parse_release_coord_manifest
from src.tools.policy_store import LoadedPolicy, parse_policy
from src.tools.telemetry_extractor import (
    TelemetryExtractor,
    build_dependency_query,
    parse_dependency_response,
    source_reference,
)

URN = "urn:release-coord:acme:prod:aks-1"
CLUSTER = TelemetryClusterFilter(value="MC_rg_aks-1_eastus")
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _static(name: str, repository: str, root: str, namespace: str = "unknown") -> ServiceNode:
    return ServiceNode(
        service_id=f"{URN}:{namespace}:{name}",
        name=name,
        metadata={"repository": repository, "service_root": root},
    )


def _k8s(namespace: str, name: str) -> ServiceNode:
    return ServiceNode(service_id=f"{URN}:{namespace}:{name}", name=name)


def _edge(source: str, target: str, method: DiscoveryMethod) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[method],
        confidence_score=0.7,
        last_observed_at=T0,
    )


def _reconcile(
    services: list[ServiceNode],
    static_edges: list[DependencyEdge] = [],
    runtime_edges: list[DependencyEdge] = [],
    namespaces: list[str] = ["shop"],
) -> dict[str, Any]:
    state: Any = {
        "tenant": "acme",
        "environment": "prod",
        "cluster": "aks-1",
        "target_namespaces": namespaces,
        "discovered_services": services,
        "static_edges": static_edges,
        "runtime_edges": runtime_edges,
    }
    graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
    assert isinstance(graph, dict)
    # Whatever reconciliation produces must be a valid baseline graph.
    KnowledgeGraph(
        services=graph["services"],
        edges=graph["edges"],
        unresolved_entities=graph["unresolved_entities"],
    )
    return graph


def _pairs(graph: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e.source_service_id, e.target_service_id) for e in graph["edges"]]


class AmbiguousServiceTests(unittest.TestCase):
    def test_two_repositories_claiming_one_service_are_ambiguous(self) -> None:
        a = _static("api", "repo-a", "src/api")
        b = _static("api", "repo-b", "api")
        graph = _reconcile(
            [a, b, _k8s("shop", "api"), _k8s("shop", "db")],
            static_edges=[
                _edge(a.service_id, "db", DiscoveryMethod.STATIC_CODE),
            ],
            runtime_edges=[_edge("shop/api", "db", DiscoveryMethod.RUNTIME_TELEMETRY)],
        )

        entity_id = f"unresolved:{URN}:shop:api"
        # The Kubernetes service stays; neither repository directory is attached.
        self.assertEqual(
            [(s.service_id, s.metadata) for s in graph["services"]],
            [(f"{URN}:shop:api", {}), (f"{URN}:shop:db", {})],
        )
        self.assertEqual(
            [(u.entity_id, u.kind) for u in graph["unresolved_entities"]],
            [(entity_id, UnresolvedEntityKind.AMBIGUOUS_SERVICE)],
        )
        # Code edges start from the ambiguity; observed traffic stays with
        # the Kubernetes service.
        self.assertEqual(
            _pairs(graph),
            [(entity_id, f"{URN}:shop:db"), (f"{URN}:shop:api", f"{URN}:shop:db")],
        )

    def test_name_in_several_namespaces_without_a_default_is_ambiguous(self) -> None:
        static = _static("api", "repo-a", "src/api")
        graph = _reconcile(
            [static, _k8s("blue", "api"), _k8s("green", "api")],
            static_edges=[_edge(static.service_id, "cache", DiscoveryMethod.STATIC_CODE)],
            namespaces=["blue", "green"],
        )

        self.assertEqual(
            [s.service_id for s in graph["services"]],
            [f"{URN}:blue:api", f"{URN}:green:api"],
        )
        entity_id = f"unresolved:{URN}:unknown:api"
        self.assertIn(
            (entity_id, UnresolvedEntityKind.AMBIGUOUS_SERVICE),
            [(u.entity_id, u.kind) for u in graph["unresolved_entities"]],
        )
        self.assertEqual(_pairs(graph), [(entity_id, "unresolved:cache")])

    def test_declared_namespace_resolves_the_ambiguity(self) -> None:
        static = _static("api", "repo-a", "src/api", namespace="green")
        graph = _reconcile(
            [static, _k8s("blue", "api"), _k8s("green", "api"), _k8s("green", "cache")],
            static_edges=[_edge(static.service_id, "cache", DiscoveryMethod.STATIC_CODE)],
            namespaces=["blue", "green"],
        )

        services = {s.service_id: s.metadata for s in graph["services"]}
        self.assertEqual(services[f"{URN}:green:api"]["repository"], "repo-a")
        self.assertNotIn("repository", services[f"{URN}:blue:api"])
        self.assertEqual(graph["unresolved_entities"], [])
        self.assertEqual(_pairs(graph), [(f"{URN}:green:api", f"{URN}:green:cache")])

    def test_one_repository_directory_per_service_still_merges(self) -> None:
        static = _static("api", "repo-a", "src/api")
        graph = _reconcile([static, _k8s("shop", "api")])
        ((service_id, metadata),) = [(s.service_id, s.metadata) for s in graph["services"]]
        self.assertEqual(service_id, f"{URN}:shop:api")
        self.assertEqual(metadata["repository"], "repo-a")


class TargetNamespaceTests(unittest.TestCase):
    def test_bare_targets_resolve_in_the_callers_namespace(self) -> None:
        graph = _reconcile(
            [_k8s("blue", "api"), _k8s("blue", "db"), _k8s("green", "db")],
            runtime_edges=[_edge("blue/api", "db:5432", DiscoveryMethod.RUNTIME_TELEMETRY)],
            namespaces=["blue", "green"],
        )
        self.assertEqual(_pairs(graph), [(f"{URN}:blue:api", f"{URN}:blue:db")])

    def test_target_only_in_another_namespace_is_unresolved(self) -> None:
        graph = _reconcile(
            [_k8s("blue", "api"), _k8s("green", "db")],
            runtime_edges=[_edge("blue/api", "db", DiscoveryMethod.RUNTIME_TELEMETRY)],
            namespaces=["blue", "green"],
        )
        self.assertEqual(_pairs(graph), [(f"{URN}:blue:api", "unresolved:db")])

    def test_live_edges_resolve_targets_like_discovery(self) -> None:
        graph = KnowledgeGraph(
            services=[_k8s("blue", "api"), _k8s("blue", "db"), _k8s("green", "db")],
            edges=[],
            scan=ScanContext(environment="prod", cluster="aks-1", namespaces=["blue", "green"]),
        )
        context = IdentityContext.for_scan(
            tenant="acme", namespaces=["blue", "green"], environment="prod", cluster="aks-1"
        )
        live = [_edge("blue/api", "db", DiscoveryMethod.RUNTIME_TELEMETRY)]
        (edge,) = resolve_live_edges(graph, context, live)
        self.assertEqual((edge.source_id, edge.target_id), (f"{URN}:blue:api", f"{URN}:blue:db"))


class ManifestNamespaceTests(unittest.TestCase):
    def test_namespace_is_optional_and_validated(self) -> None:
        manifest = parse_release_coord_manifest(
            "services:\n  - name: api\n    root: src/api\n    namespace: shop\n"
            "  - name: web\n"
        )
        self.assertEqual([s.namespace for s in manifest.services], ["shop", None])
        with self.assertRaises((ValidationError, ValueError)):
            parse_release_coord_manifest(
                "services:\n  - name: api\n    namespace: Not_A_Label\n"
            )


def _columns_table(rows: list[list[object]]) -> LogsTable:
    columns = ["Source", "SourceNamespace", "Target", "LastObservedAt"]
    return LogsTable(
        name="PrimaryResult",
        columns=columns,
        columns_types=["string", "string", "string", "datetime"],
        rows=rows,
    )


class TelemetryQueryTests(unittest.TestCase):
    def test_query_without_filter_reads_every_cluster(self) -> None:
        query = build_dependency_query(7)
        self.assertIn("ago(7d)", query)
        self.assertNotIn("k8s.cluster.name", query)
        self.assertIn('Properties["k8s.namespace.name"]', query)
        self.assertIn('coalesce(tostring(Properties["service.name"]), AppRoleName)', query)

    def test_query_with_filter_keeps_only_that_cluster(self) -> None:
        query = build_dependency_query(30, TelemetryScope(cluster=CLUSTER))
        self.assertIn(
            '| where tostring(Properties["k8s.cluster.name"]) == "MC_rg_aks-1_eastus"',
            query,
        )

    def test_filter_values_cannot_inject_kql(self) -> None:
        for value in ['x" or 1==1 //', "x\n| take 1", "x'y"]:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    TelemetryClusterFilter(value=value)

    def test_rows_with_a_namespace_name_their_source_in_it(self) -> None:
        result = parse_dependency_response(
            LogsQueryResult(
                tables=[
                    _columns_table(
                        [
                            ["order-service", "default", "rabbitmq:5672", "2026-11-01T00:00:00Z"],
                            ["external-app", "", "order-service", "2026-11-01T00:00:00Z"],
                        ]
                    )
                ]
            )
        )
        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        self.assertEqual(
            [e.source_service_id for e in result.items],
            ["default/order-service", "external-app"],
        )

    def test_source_reference_only_qualifies_dns_labels(self) -> None:
        self.assertEqual(source_reference("Order-Service", "Default"), "default/order-service")
        self.assertEqual(source_reference("order.service", "default"), "order.service")
        self.assertEqual(source_reference("api", "bad_ns"), "api")
        self.assertEqual(source_reference("api", None), "api")

    def test_extractor_sends_the_filtered_query(self) -> None:
        extractor = TelemetryExtractor.__new__(TelemetryExtractor)
        extractor._workspace_id = "ws-1"
        extractor._source = "azure"
        extractor._runtime_metrics = {}
        extractor._credential = None
        client = MagicMock()
        client.query_workspace.return_value = LogsQueryResult(tables=[_columns_table([])])
        extractor._client = client

        extractor.get_active_dependencies(days_back=7, scope=TelemetryScope(cluster=CLUSTER))

        query = client.query_workspace.call_args.args[1]
        self.assertIn('"MC_rg_aks-1_eastus"', query)


POLICY = parse_policy(
    """\
policy_version: 3
tenant: acme
project_id: shop
environment: prod
cluster: aks-1
required_sources: [K8S_MANIFEST, RUNTIME_TELEMETRY]
runtime_evidence: live
kubernetes_api_server: https://aks-1.example.io
telemetry_cluster:
  value: MC_rg_aks-1_eastus
""",
    BaselineScope(tenant="acme", project_id="shop"),
)


class DiscoveryScopeTests(unittest.TestCase):
    def _state(self, **overrides: object) -> Any:
        state: dict[str, object] = {
            "tenant": "acme",
            "project_id": "shop",
            "environment": "prod",
            "cluster": "aks-1",
            "workspace_id": "ws-1",
            "target_namespaces": ["shop"],
            "discovery_reports": [],
        }
        state.update(overrides)
        return state

    def test_k8s_stage_records_the_api_server_it_read(self) -> None:
        with patch.object(discovery_nodes, "KubernetesExtractor") as k8s_cls:
            extractor = k8s_cls.return_value
            extractor.api_server = "https://aks-1.example.io:443"
            extractor.get_namespace_topology.return_value = DiscoveryResult.success([])
            result = discovery_nodes.extract_k8s_topology(self._state())
        self.assertEqual(result["kubernetes_api_server"], "https://aks-1.example.io:443")

    def test_telemetry_stage_applies_and_records_the_policy_filter(self) -> None:
        store = MagicMock()
        store.load.return_value = LoadedPolicy(policy=POLICY, sha256="0" * 64)
        with patch.object(discovery_nodes, "POLICY_STORE", store), patch.object(
            discovery_nodes, "TelemetryExtractor"
        ) as telemetry_cls:
            telemetry_cls.return_value.dropped_records = {}
            telemetry_cls.return_value.get_active_dependencies.return_value = (
                DiscoveryResult.success([])
            )
            result = discovery_nodes.extract_runtime_telemetry(self._state())

        store.load.assert_called_once_with(BaselineScope(tenant="acme", project_id="shop"))
        telemetry_cls.return_value.get_active_dependencies.assert_called_once_with(
            days_back=POLICY.runtime_lookback_days, scope=POLICY.telemetry_scope
        )
        self.assertEqual(result["telemetry_scope"], POLICY.telemetry_scope)

    def test_scan_context_records_both_proofs(self) -> None:
        scan = discovery_nodes.scan_context(
            self._state(
                kubernetes_api_server="https://aks-1.example.io:443",
                telemetry_scope=POLICY.telemetry_scope,
            )
        )
        self.assertEqual(scan.kubernetes_api_server, "https://aks-1.example.io:443")
        self.assertEqual(scan.telemetry_cluster, POLICY.telemetry_cluster)
        legacy = ScanContext.model_validate({"environment": "prod", "cluster": "aks-1"})
        self.assertEqual((legacy.kubernetes_api_server, legacy.telemetry_cluster), ("", None))


class AssessmentScopeTests(unittest.TestCase):
    def test_live_query_uses_the_policy_filter(self) -> None:
        loaded = LoadedPolicy(policy=POLICY, sha256="0" * 64)
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls:
            extractor = extractor_cls.return_value
            extractor.is_mock = False
            extractor.get_active_dependencies.return_value = DiscoveryResult.success([])
            extractor.get_runtime_metrics.return_value = {}
            extractor.dropped_records = {}
            state: Any = {"workspace_id": "ws-1", "project_policy": loaded}
            coordinator_nodes.validate_runtime_telemetry_node(state)

        extractor.get_active_dependencies.assert_called_once_with(
            days_back=7, scope=POLICY.telemetry_scope
        )


class VerifyKubernetesTests(unittest.TestCase):
    def test_success_names_the_api_server(self) -> None:
        with patch.object(api, "KubernetesExtractor") as k8s_cls:
            k8s_cls.return_value.verify_access.return_value = (True, "Success")
            k8s_cls.return_value.api_server = "https://aks-1.example.io:443"
            response = api.verify_kubernetes(["shop"])
        self.assertTrue(response.status)
        self.assertEqual(response.message, "Success: read from https://aks-1.example.io:443")


if __name__ == "__main__":
    unittest.main()
