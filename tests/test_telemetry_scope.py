"""Tests for telemetry scoping by Application Insights component (T2c).

Classic SDK and Azure Monitor Distro rows carry no cluster attribute, so a
project can scope its telemetry by the components (``_ResourceId``) that
write it, alone or together with a cluster filter. The scope is applied to
every query, recorded in the baseline and checked at assessment.
"""

import unittest
from typing import Any
from unittest.mock import MagicMock

from pydantic import ValidationError

from src.coordinator.project_policy import baseline_scope_issue
from src.core.models import (
    ReasonCode,
    ScanContext,
    TelemetryClusterFilter,
    TelemetryScope,
)
from src.discovery_engine import nodes as discovery_nodes
from src.tools.baseline_store import BaselineScope
from src.tools.policy_store import parse_policy
from src.tools.telemetry_extractor import (
    TelemetryExtractor,
    build_dependency_query,
    build_unscoped_query,
)

SUBSCRIPTION = "/subscriptions/48a7217e-eeb0-4625-96f1-8eb3a574481e"
COMPONENT_A = f"{SUBSCRIPTION}/resourceGroups/Lab/providers/Microsoft.Insights/components/rc-ai"
COMPONENT_B = f"{SUBSCRIPTION}/resourcegroups/lab/providers/microsoft.insights/components/rc-ai-b"


def _policy(extra: str) -> Any:
    return parse_policy(
        "policy_version: 1\ntenant: acme\nproject_id: shop\nenvironment: prod\n"
        "cluster: aks-1\nrequired_sources: [RUNTIME_TELEMETRY]\nruntime_evidence: live\n"
        + extra,
        BaselineScope(tenant="acme", project_id="shop"),
    )


class TelemetryScopeModelTests(unittest.TestCase):
    def test_components_are_lower_cased_sorted_and_unique(self) -> None:
        scope = TelemetryScope(components=[COMPONENT_B, COMPONENT_A, COMPONENT_A.upper() + "/"])
        self.assertEqual(scope.components, sorted({COMPONENT_A.lower(), COMPONENT_B}))

    def test_rejects_anything_but_a_component_id(self) -> None:
        for bad in [
            "rc-ai",
            f"{SUBSCRIPTION}/resourcegroups/lab/providers/microsoft.web/sites/app",
            COMPONENT_B + '") or true //',
            COMPONENT_B + "/../x",
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                TelemetryScope(components=[bad])

    def test_needs_a_cluster_or_components(self) -> None:
        with self.assertRaises(ValidationError):
            TelemetryScope()

    def test_describe_names_both_parts(self) -> None:
        scope = TelemetryScope(
            cluster=TelemetryClusterFilter(value="aks-1"), components=[COMPONENT_A, COMPONENT_B]
        )
        self.assertEqual(
            scope.describe(),
            "k8s.cluster.name == 'aks-1' and Application Insights components rc-ai, rc-ai-b",
        )


class PolicyScopeTests(unittest.TestCase):
    def test_components_alone_prove_the_cluster(self) -> None:
        policy = _policy(f"telemetry_components:\n  - {COMPONENT_A}\n")
        self.assertEqual(policy.telemetry_scope, TelemetryScope(components=[COMPONENT_A]))

    def test_live_evidence_needs_some_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "telemetry_components"):
            _policy("")

    def test_cluster_only_policy_keeps_its_scope(self) -> None:
        policy = _policy("telemetry_cluster:\n  value: aks-1\n")
        self.assertEqual(
            policy.telemetry_scope, TelemetryScope(cluster=TelemetryClusterFilter(value="aks-1"))
        )


class ScopedQueryTests(unittest.TestCase):
    def test_component_scope_filters_on_resource_id_only(self) -> None:
        query = build_dependency_query(7, TelemetryScope(components=[COMPONENT_A, COMPONENT_B]))
        self.assertIn(
            f'| where _ResourceId in~ ("{COMPONENT_A.lower()}", "{COMPONENT_B}")', query
        )
        self.assertNotIn("k8s.cluster.name", query)

    def test_both_scopes_apply_together(self) -> None:
        scope = TelemetryScope(
            cluster=TelemetryClusterFilter(value="aks-1"), components=[COMPONENT_B]
        )
        query = build_dependency_query(7, scope)
        self.assertIn("_ResourceId in~", query)
        self.assertIn('Properties["k8s.cluster.name"]) == "aks-1"', query)
        unscoped = build_unscoped_query(7, scope)
        self.assertIn("_ResourceId in~", unscoped)
        self.assertIn('isempty(tostring(Properties["k8s.cluster.name"]))', unscoped)

    def test_namespace_comes_from_otel_or_the_classic_enricher(self) -> None:
        query = build_dependency_query(7)
        self.assertIn(
            'SourceNamespace = coalesce(tostring(Properties["k8s.namespace.name"]), '
            'tostring(Properties["Kubernetes.Pod.Namespace"]))',
            query,
        )

    def test_unscoped_diagnostic_needs_a_cluster_filter(self) -> None:
        scope = TelemetryScope(components=[COMPONENT_B])
        with self.assertRaises(ValueError):
            build_unscoped_query(7, scope)
        extractor = TelemetryExtractor.__new__(TelemetryExtractor)
        extractor._source = "azure"
        extractor._workspace_id = "ws"
        extractor._client = MagicMock()
        self.assertIsNone(extractor.get_unscoped_sources(7, scope))
        extractor._client.query_workspace.assert_not_called()


class BaselineComponentScopeTests(unittest.TestCase):
    POLICY = _policy(f"telemetry_components:\n  - {COMPONENT_A}\n")

    def test_snapshot_must_have_read_the_same_components(self) -> None:
        same = ScanContext(environment="prod", cluster="aks-1", telemetry_components=[COMPONENT_A])
        self.assertIsNone(baseline_scope_issue(self.POLICY, "snap", same))
        for components, shown in [
            ([], "no telemetry scope"),
            ([COMPONENT_A, COMPONENT_B], "components rc-ai, rc-ai-b"),
        ]:
            with self.subTest(components=components):
                scan = ScanContext(
                    environment="prod", cluster="aks-1", telemetry_components=components
                )
                issue = baseline_scope_issue(self.POLICY, "snap", scan)
                assert issue is not None
                self.assertIs(issue.code, ReasonCode.BASELINE_SCOPE_MISMATCH)
                self.assertIn(shown, issue.detail)
                self.assertIn("component rc-ai.", issue.detail)

    def test_discovery_records_the_components(self) -> None:
        state: Any = {
            "environment": "prod",
            "cluster": "aks-1",
            "telemetry_scope": self.POLICY.telemetry_scope,
        }
        scan = discovery_nodes.scan_context(state)
        self.assertEqual(scan.telemetry_components, [COMPONENT_A.lower()])
        self.assertIsNone(scan.telemetry_cluster)
        self.assertEqual(scan.telemetry_scope, self.POLICY.telemetry_scope)


if __name__ == "__main__":
    unittest.main()
