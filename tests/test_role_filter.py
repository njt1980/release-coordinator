"""Tests for the opt-in telemetry role filter (F4).

A policy may keep only rows whose role name is in the inventory and/or
whose role instance is a scanned pod. It is a filter, not a scope: it never
satisfies the policy's cluster-proof requirement and the run says so.
"""

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from src.core.models import DiscoveryResult, DiscoveryStatus, ServiceNode
from src.core.telemetry_roles import (
    TelemetryRoleAllowList,
    TelemetryRoleFilter,
    role_allow_list,
)
from src.discovery_engine import nodes as discovery_nodes
from src.tools.baseline_store import BaselineScope
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.policy_store import LoadedPolicy, parse_policy
from src.tools.telemetry_extractor import build_dependency_query
from tests.test_coverage_and_shape import CONTEXT, _id, _items, _meta, _service

BOTH = TelemetryRoleFilter(role_names_in_inventory=True, role_instances_in_pods=True)


def _policy(extra: str = "") -> Any:
    return parse_policy(
        "policy_version: 1\ntenant: acme\nproject_id: shop\nenvironment: prod\n"
        "cluster: aks-1\nrequired_sources: [RUNTIME_TELEMETRY]\nruntime_evidence: live\n"
        "telemetry_cluster:\n  value: aks-1\n" + extra,
        BaselineScope(tenant="acme", project_id="shop"),
    )


class RoleFilterModelTests(unittest.TestCase):
    def test_off_by_default(self) -> None:
        self.assertFalse(_policy().telemetry_role_filter.enabled)

    def test_policy_opts_in(self) -> None:
        policy = _policy(
            "telemetry_role_filter:\n  role_names_in_inventory: true\n"
            "  role_instances_in_pods: true\n"
        )
        self.assertEqual(policy.telemetry_role_filter, BOTH)
        self.assertEqual(
            BOTH.describe(), "role name in the inventory and role instance among the scanned pods"
        )

    def test_filter_is_not_a_cluster_proof(self) -> None:
        with self.assertRaisesRegex(ValueError, "telemetry_cluster or telemetry_components"):
            parse_policy(
                "policy_version: 1\ntenant: acme\nproject_id: shop\nenvironment: prod\n"
                "cluster: aks-1\nrequired_sources: [RUNTIME_TELEMETRY]\nruntime_evidence: live\n"
                "telemetry_role_filter:\n  role_names_in_inventory: true\n",
                BaselineScope(tenant="acme", project_id="shop"),
            )

    def test_allow_list_keeps_only_query_safe_names(self) -> None:
        allow = TelemetryRoleAllowList(role_names=["Orders", "orders", 'x") or true //', "cart"])
        self.assertEqual(allow.role_names, ["cart", "orders"])
        with self.assertRaises(ValidationError):
            TelemetryRoleAllowList(role_names=['") or true'])

    def test_allow_list_from_inventory_and_pods(self) -> None:
        allow, notes = role_allow_list(BOTH, ["orders", "cart"], ["orders-7d9f-abc12"])
        self.assertEqual(notes, [])
        assert allow is not None
        self.assertEqual(allow.role_names, ["cart", "orders"])
        self.assertEqual(allow.role_instances, ["orders-7d9f-abc12"])
        self.assertEqual(allow.applied, BOTH)

    def test_part_that_cannot_apply_is_left_out_with_a_note(self) -> None:
        allow, notes = role_allow_list(BOTH, ["orders"], None)
        assert allow is not None
        self.assertIsNone(allow.role_instances)
        self.assertIn("pods were not read", notes[0])
        allow, notes = role_allow_list(BOTH, [], [])
        self.assertIsNone(allow)
        self.assertEqual(len(notes), 2)


class RoleFilterQueryTests(unittest.TestCase):
    def test_no_filter_no_clause(self) -> None:
        query = build_dependency_query(3)
        self.assertNotIn("AppRoleInstance", query)
        self.assertNotIn("in~ (", query)

    def test_filter_applies_to_dependencies_and_requests(self) -> None:
        allow = TelemetryRoleAllowList(role_names=["orders"], role_instances=["orders-abc"])
        query = build_dependency_query(3, roles=allow)
        self.assertIn('| where extract(@"[^/]*$", 0, SourceName) in~ ("orders")', query)
        self.assertIn('| where extract(@"[^/]*$", 0, CalleeName) in~ ("orders")', query)
        self.assertEqual(query.count('tolower(AppRoleInstance) in ("orders-abc")'), 2)


class KubernetesNamesTests(unittest.TestCase):
    def _extractor(self, pods: Any) -> KubernetesExtractor:
        core, apps, batch = MagicMock(), MagicMock(), MagicMock()
        core.list_namespaced_service.return_value = _items(_service("orders", {"app": "orders"}))
        core.list_namespaced_config_map.return_value = _items()
        core.list_namespaced_pod.side_effect = pods
        spec = SimpleNamespace(
            containers=[SimpleNamespace(name="orders-api", image="acr.io/orders:1", env=None, env_from=None)]
        )
        template = SimpleNamespace(metadata=_meta("", {"app": "orders"}), spec=spec)
        apps.list_namespaced_deployment.return_value = _items(
            SimpleNamespace(metadata=_meta("orders"), spec=SimpleNamespace(template=template))
        )
        apps.list_namespaced_replica_set.return_value = _items(
            SimpleNamespace(metadata=_meta("orders-7d9f", None, ("Deployment", "orders")), spec=None)
        )
        for empty in (
            apps.list_namespaced_stateful_set,
            apps.list_namespaced_daemon_set,
            batch.list_namespaced_cron_job,
            batch.list_namespaced_job,
        ):
            empty.return_value = _items()
        extractor = KubernetesExtractor.__new__(KubernetesExtractor)
        extractor._core_v1_api = core  # type: ignore[assignment]
        extractor._apps_v1_api = apps  # type: ignore[assignment]
        extractor._batch_v1_api = batch  # type: ignore[assignment]
        extractor._api_server = "https://aks-1.example.azmk8s.io:443"
        return extractor

    def test_topology_records_workload_container_and_pod_names(self) -> None:
        pod = SimpleNamespace(metadata=_meta("orders-7d9f-abc12", {"app": "orders"}, ("ReplicaSet", "orders-7d9f")))
        (topology,) = self._extractor([_items(pod)]).get_namespace_topology("shop", CONTEXT).items
        self.assertEqual(topology.workload_names, ["orders", "orders-api"])
        self.assertEqual(topology.pod_names, ["orders-7d9f-abc12"])

    def test_unlistable_pods_are_unknown_not_empty(self) -> None:
        result = self._extractor(RuntimeError("forbidden")).get_namespace_topology("shop", CONTEXT)
        (topology,) = result.items
        self.assertIsNone(topology.pod_names)
        self.assertIs(result.status, DiscoveryStatus.PARTIAL)


class DiscoveryStageTests(unittest.TestCase):
    POLICY = _policy(
        "telemetry_role_filter:\n  role_names_in_inventory: true\n  role_instances_in_pods: true\n"
    )

    def _run(self, state: dict[str, Any]) -> tuple[dict[str, object], MagicMock]:
        store = MagicMock()
        store.load.return_value = LoadedPolicy(policy=self.POLICY, sha256="0" * 64)
        with patch.object(discovery_nodes, "POLICY_STORE", store), patch.object(
            discovery_nodes, "TelemetryExtractor"
        ) as telemetry_cls:
            telemetry_cls.return_value.dropped_records = {}
            telemetry_cls.return_value.get_active_dependencies.return_value = DiscoveryResult.success([])
            result = discovery_nodes.extract_runtime_telemetry(
                {
                    "tenant": "acme",
                    "project_id": "shop",
                    "workspace_id": "ws-1",
                    "discovery_reports": [],
                    **state,
                }  # type: ignore[typeddict-item]
            )
        return result, telemetry_cls.return_value.get_active_dependencies

    def test_applies_inventory_and_pods_and_records_it(self) -> None:
        result, call = self._run(
            {
                "discovered_services": [ServiceNode(service_id=_id("orders"), name="orders")],
                "kubernetes_workload_names": ["orders-api"],
                "kubernetes_pod_names": ["orders-7d9f-abc12"],
            }
        )
        roles = call.call_args.kwargs["roles"]
        self.assertEqual(roles.role_names, ["orders", "orders-api"])
        self.assertEqual(roles.role_instances, ["orders-7d9f-abc12"])
        self.assertEqual(result["telemetry_role_filter"], BOTH)
        (report,) = result["discovery_reports"]  # type: ignore[misc]
        self.assertIs(report.status, DiscoveryStatus.SUCCESS)

    def test_unread_pods_make_the_stage_partial(self) -> None:
        result, call = self._run(
            {"discovered_services": [ServiceNode(service_id=_id("orders"), name="orders")]}
        )
        self.assertIsNone(call.call_args.kwargs["roles"].role_instances)
        self.assertEqual(
            result["telemetry_role_filter"], TelemetryRoleFilter(role_names_in_inventory=True)
        )
        (report,) = result["discovery_reports"]  # type: ignore[misc]
        self.assertIs(report.status, DiscoveryStatus.PARTIAL)
        self.assertIn("Role-instance telemetry filter not applied", report.errors[0])

    def test_scan_context_records_the_applied_filter(self) -> None:
        scan = discovery_nodes.scan_context(
            {"environment": "prod", "cluster": "aks-1", "telemetry_role_filter": BOTH}  # type: ignore[typeddict-item]
        )
        self.assertEqual(scan.telemetry_role_filter, BOTH)


if __name__ == "__main__":
    unittest.main()
