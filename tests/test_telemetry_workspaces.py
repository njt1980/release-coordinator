"""Tests for several telemetry workspaces per project and sampling-aware coverage (T2d)."""

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

from azure.monitor.query import LogsQueryResult, LogsTable
from src.coordinator.project_policy import baseline_scope_issue, live_runtime_state
from src.core.models import (
    LiveCoverage,
    ReasonCode,
    RuntimeEvidenceState,
    ScanContext,
    TelemetryClusterFilter,
    normalize_workspace_ids,
)
from src.discovery_engine import nodes as discovery_nodes
from src.tools.baseline_store import BaselineScope
from src.tools.policy_store import PolicyInvalidError, parse_policy
from src.tools.telemetry_extractor import TelemetryExtractor

PRIMARY = "b430b9a6-1491-4b12-a744-49a5b9176580"
OTHER = "4f04e987-de47-4c90-9e73-a57a60e39bfc"
THIRD = "00000000-0000-4000-8000-000000000003"
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _policy(extra: str = "") -> Any:
    return parse_policy(
        "policy_version: 1\ntenant: acme\nproject_id: shop\nenvironment: prod\n"
        "cluster: aks-1\nrequired_sources: [RUNTIME_TELEMETRY]\nruntime_evidence: live\n"
        "telemetry_cluster:\n  value: aks-1\n" + extra,
        BaselineScope(tenant="acme", project_id="shop"),
    )


def _extractor(additional: list[str], response: object) -> TelemetryExtractor:
    extractor = TelemetryExtractor(PRIMARY, additional)
    extractor._source = "azure"
    extractor._client = MagicMock()
    extractor._client.query_workspace.return_value = response
    return extractor


class WorkspaceIdTests(unittest.TestCase):
    def test_ids_are_normalized(self) -> None:
        self.assertEqual(normalize_workspace_ids([OTHER.upper(), THIRD, f" {OTHER} "]), [THIRD, OTHER])

    def test_rejects_non_ids_and_too_many(self) -> None:
        for bad in [["rc-lab-logs"], [OTHER + '" | union x']]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_workspace_ids(bad)
        many = [f"00000000-0000-4000-8000-{index:012d}" for index in range(100)]
        with self.assertRaises(ValueError):
            normalize_workspace_ids(many)

    def test_policy_lists_additional_workspaces(self) -> None:
        policy = _policy(f"telemetry_additional_workspaces: [{OTHER.upper()}]\n")
        self.assertEqual(policy.telemetry_additional_workspaces, [OTHER])
        with self.assertRaises(PolicyInvalidError):
            _policy("telemetry_additional_workspaces: [not-a-workspace]\n")


class MultiWorkspaceQueryTests(unittest.TestCase):
    def test_one_query_reads_every_workspace(self) -> None:
        extractor = _extractor([OTHER, PRIMARY.upper()], LogsQueryResult(tables=[]))
        extractor.get_active_dependencies(days_back=3)
        kwargs = extractor._client.query_workspace.call_args.kwargs  # type: ignore[union-attr]
        # The primary workspace is not repeated as an additional one.
        self.assertEqual(kwargs["additional_workspaces"], [OTHER])
        self.assertEqual(kwargs["timespan"], timedelta(days=3))

    def test_single_workspace_sends_none(self) -> None:
        extractor = _extractor([], LogsQueryResult(tables=[]))
        extractor.get_active_dependencies(days_back=3)
        kwargs = extractor._client.query_workspace.call_args.kwargs  # type: ignore[union-attr]
        self.assertIsNone(kwargs["additional_workspaces"])

    def test_snapshot_must_have_read_the_policy_workspaces(self) -> None:
        policy = _policy(f"telemetry_additional_workspaces: [{OTHER}]\n")
        base = {
            "environment": "prod",
            "cluster": "aks-1",
            "telemetry_cluster": TelemetryClusterFilter(value="aks-1"),
            "telemetry_workspace_id": PRIMARY,
        }
        same = ScanContext.model_validate({**base, "telemetry_additional_workspaces": [OTHER]})
        self.assertIsNone(baseline_scope_issue(policy, "snap", same))
        issue = baseline_scope_issue(policy, "snap", ScanContext.model_validate(base))
        assert issue is not None
        self.assertIs(issue.code, ReasonCode.BASELINE_SCOPE_MISMATCH)
        self.assertIn(f"additional workspaces none, but the project policy lists {OTHER}", issue.detail)

    def test_discovery_records_the_workspaces(self) -> None:
        state: Any = {
            "environment": "prod",
            "cluster": "aks-1",
            "workspace_id": PRIMARY,
            "telemetry_additional_workspaces": [OTHER],
        }
        scan = discovery_nodes.scan_context(state)
        self.assertEqual(scan.telemetry_additional_workspaces, [OTHER])


class SamplingTests(unittest.TestCase):
    def test_extractor_reports_sampled_sources(self) -> None:
        columns = ["Source", "SourceNamespace", "Target", "LastObservedAt", "Calls", "MaxItemCount"]
        table = LogsTable(
            name="PrimaryResult",
            columns=columns,
            columns_types=["string"] * len(columns),
            rows=[
                ["orders", "probe", "inventory", "2026-11-14T08:20:00Z", 33314, 15],
                ["orders", "probe", "mssql:1433", "2026-11-14T08:20:00Z", 900, 4],
                ["gateway", "", "orders", "2026-11-14T08:20:00Z", 5801, 1],
            ],
        )
        extractor = _extractor([], LogsQueryResult(tables=[table]))
        extractor.get_active_dependencies(days_back=1)
        self.assertEqual(extractor.sampled_sources, {"probe/orders": 15})

    def _coverage(self, sampled: dict[str, int]) -> LiveCoverage:
        return LiveCoverage(
            edge_count=3,
            newest_observed_at=NOW - timedelta(minutes=5),
            observed_services=["orders"],
            uncovered_services=["audit"],
            sampled_sources=sampled,
        )

    def test_uncovered_explanation_mentions_sampling(self) -> None:
        state, why = live_runtime_state(None, self._coverage({"probe/orders": 15}), NOW)
        self.assertIs(state, RuntimeEvidenceState.LIVE_UNCOVERED)
        self.assertIn("This telemetry is sampled (probe/orders (1 row per up to 15 calls))", why)
        self.assertIn("a service called rarely can have no row in the window", why)

    def test_unsampled_explanation_says_nothing_about_sampling(self) -> None:
        _, why = live_runtime_state(None, self._coverage({}), NOW)
        self.assertNotIn("sampled", why)

    def test_many_sampled_sources_are_summarized(self) -> None:
        sampled = {f"svc-{index}": index + 2 for index in range(8)}
        _, why = live_runtime_state(None, self._coverage(sampled), NOW)
        self.assertIn("svc-7 (1 row per up to 9 calls)", why)
        self.assertIn("and 3 more", why)
        self.assertNotIn("svc-0 ", why)


if __name__ == "__main__":
    unittest.main()
