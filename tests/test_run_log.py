"""Tests for live run logs: store, runner, narration and the HTTP surface."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from src import api, runs, webhooks
from src.coordinator.sequencing import DeploymentPlan
from src.core.models import (
    AssessmentEvidence,
    ContractChange,
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    EvidenceIssue,
    Exemption,
    KnowledgeGraph,
    LiveCoverage,
    ReasonCode,
    ReleaseAssessment,
    RunEventStatus,
    RuntimeEvidenceState,
    ScanContext,
    ScannedRevision,
    ServiceNode,
    TelemetryClusterFilter,
    TelemetryScope,
    TelemetryStatus,
    UnresolvedEntity,
    UnresolvedEntityKind,
    UnresolvedReach,
)
from src.orchestration.narration import narrate_assessment, narrate_discovery, short_name
from src.orchestration.run_log import EventDraft, RunLogStore, run_graph
from src.tools.baseline_store import BaselineScope, BaselineStore

URN = "urn:release-coord:acme:prod:aks-1:shop"
S = RunEventStatus


def _draft(message: str, status: RunEventStatus = S.INFO) -> EventDraft:
    return EventDraft(stage="Test", status=status, message=message)


class StoreTests(unittest.TestCase):
    def test_events_are_numbered_and_paged(self) -> None:
        store = RunLogStore()
        store.start("r1", "onboarding", "Onboard", "acme", "shop")
        for text in ("one", "two", "three"):
            store.add("r1", _draft(text))
        found = store.get("r1", after=1)
        assert found is not None
        summary, events = found
        self.assertEqual([(e.seq, e.message) for e in events], [(2, "two"), (3, "three")])
        self.assertEqual((summary.state, summary.event_count), ("running", 3))

        store.finish("r1", "completed", "Baseline saved")
        summary, _ = store.get("r1") or (None, None)
        assert summary is not None
        self.assertEqual((summary.state, summary.outcome), ("completed", "Baseline saved"))
        self.assertIsNotNone(summary.finished_at)

    def test_unknown_runs_and_bounded_memory(self) -> None:
        store = RunLogStore(max_runs=2)
        self.assertIsNone(store.add("missing", _draft("x")))
        self.assertIsNone(store.get("missing"))
        for run_id in ("a", "b", "c"):
            store.start(run_id, "assessment", run_id, "acme", "shop")
        self.assertEqual([s.run_id for s in store.summaries()], ["c", "b"])


class _FakeGraph:
    def __init__(self, chunks: list[dict[str, Any]], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error

    def stream(self, state: Any, stream_mode: str) -> Any:
        assert stream_mode == "updates"
        yield from self.chunks
        if self.error is not None:
            raise self.error


class RunnerTests(unittest.TestCase):
    def test_updates_are_applied_in_order_and_narrated(self) -> None:
        store = RunLogStore()
        store.start("r", "onboarding", "t", "acme", "shop")
        seen: list[tuple[str, Any]] = []

        def narrate(node: str, before: Any, update: Any) -> list[EventDraft]:
            seen.append((node, before.get("count")))
            return [_draft(f"{node} ran")]

        graph = _FakeGraph([{"a": {"count": 1}}, {"b": {"count": 2, "done": True}}])
        final = run_graph(graph, {"count": 0, "keep": "x"}, narrate, store, "r")  # type: ignore[arg-type]

        self.assertEqual(final, {"count": 2, "keep": "x", "done": True})
        self.assertEqual(seen, [("a", 0), ("b", 1)])
        _, events = store.get("r") or (None, [])
        self.assertEqual([e.message for e in events], ["a ran", "b ran"])

    def test_errors_are_logged_then_raised(self) -> None:
        store = RunLogStore()
        store.start("r", "onboarding", "t", "acme", "shop")
        graph = _FakeGraph([{"a": {}}], RuntimeError("boom"))
        with self.assertRaisesRegex(RuntimeError, "boom"):
            run_graph(graph, {}, lambda *_: [], store, "r")  # type: ignore[arg-type]
        _, events = store.get("r") or (None, [])
        self.assertEqual(events[-1].status, S.FAILED)
        self.assertIn("after step 'a': boom", events[-1].message)


def _report(source: DiscoveryMethod, status: DiscoveryStatus, **fields: Any) -> DiscoveryStageReport:
    return DiscoveryStageReport(source=source, status=status, **fields)


class DiscoveryNarrationTests(unittest.TestCase):
    def test_skipped_and_failed_stages(self) -> None:
        skipped = narrate_discovery(
            "extract_static_code_baseline",
            {"discovery_reports": []},
            {"discovery_reports": [_report(DiscoveryMethod.STATIC_CODE, DiscoveryStatus.SKIPPED)]},
        )
        self.assertEqual([(e.status, e.message) for e in skipped], [(S.INFO, "Skipped: no Azure DevOps project configured.")])
        failed = narrate_discovery(
            "extract_k8s_topology",
            {"discovery_reports": []},
            {
                "discovery_reports": [
                    _report(DiscoveryMethod.K8S_MANIFEST, DiscoveryStatus.FAILED, errors=["shop: 403"])
                ]
            },
        )
        self.assertEqual(failed[0].status, S.FAILED)
        self.assertEqual(failed[0].details, ["shop: 403"])

    def test_successful_stages_say_what_was_read(self) -> None:
        code = narrate_discovery(
            "extract_static_code_baseline",
            {"discovery_reports": [], "discovered_services": []},
            {
                "discovery_reports": [_report(DiscoveryMethod.STATIC_CODE, DiscoveryStatus.SUCCESS, item_count=37)],
                "discovered_services": [ServiceNode(service_id=f"{URN}:a", name="a")] * 2,
                "scanned_revisions": [ScannedRevision(repository="Shop", branch="main", commit_sha="1d61b202" + "0" * 32)],
            },
        )
        self.assertEqual(code[0].message, "Read 1 repository: 2 service directories, 37 dependency references in code.")
        self.assertEqual(code[0].details, ["Shop @ 1d61b2020000 (main)"])

        k8s = narrate_discovery(
            "extract_k8s_topology",
            {"discovery_reports": [], "target_namespaces": ["shop"]},
            {"discovery_reports": [_report(DiscoveryMethod.K8S_MANIFEST, DiscoveryStatus.SUCCESS, item_count=8)], "kubernetes_api_server": ""},
        )
        self.assertEqual([e.status for e in k8s], [S.PASSED, S.WARNING])

        telemetry = narrate_discovery(
            "extract_runtime_telemetry",
            {"discovery_reports": []},
            {
                "discovery_reports": [
                    _report(DiscoveryMethod.RUNTIME_TELEMETRY, DiscoveryStatus.SUCCESS, item_count=5, dropped_records={"no_host": 1})
                ],
                "telemetry_scope": TelemetryScope(cluster=TelemetryClusterFilter(value="aks-1")),
            },
        )
        self.assertEqual(len(telemetry), 1)
        self.assertIn("5 observed dependencies scoped to k8s.cluster.name == 'aks-1'", telemetry[0].message)
        self.assertEqual(telemetry[0].details, ["Dropped as noise: 1 × no_host"])

    def test_demo_telemetry_is_called_out(self) -> None:
        mock_edge = DependencyEdge(
            source_service_id="a",
            target_service_id="b",
            discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
            confidence_score=0.0,
            last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            provenance="mock_fixture",
            environment="mock",
        )
        events = narrate_discovery(
            "extract_runtime_telemetry",
            {"discovery_reports": []},
            {
                "discovery_reports": [_report(DiscoveryMethod.RUNTIME_TELEMETRY, DiscoveryStatus.SUCCESS, item_count=1)],
                "runtime_edges": [mock_edge],
                "telemetry_scope": None,
            },
        )
        self.assertEqual([e.status for e in events], [S.PASSED, S.WARNING, S.WARNING])
        self.assertIn("demo fixture rows", events[1].message)

    def test_reconcile_warns_about_unresolved_endpoints(self) -> None:
        unset = UnresolvedEntity(
            entity_id="unresolved:env:a/x", reference="env:a/x", kind=UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET
        )
        events = narrate_discovery(
            "reconcile_knowledge_graph",
            {},
            {"reconciled_graph": {"services": [], "edges": [], "unresolved_entities": [unset]}},
        )
        self.assertEqual([e.status for e in events], [S.PASSED, S.WARNING])
        self.assertIn("(environment variable unset: 1)", events[1].message)


def _policy() -> Any:
    from src.tools.policy_store import LoadedPolicy, parse_policy

    policy = parse_policy(
        "policy_version: 4\ntenant: acme\nproject_id: shop\nenvironment: prod\ncluster: aks-1\n"
        "required_sources: [STATIC_CODE]\nruntime_evidence: baseline\n",
        BaselineScope(tenant="acme", project_id="shop"),
    )
    return LoadedPolicy(policy=policy, sha256="a" * 64)


class AssessmentNarrationTests(unittest.TestCase):
    def test_policy_and_new_issues(self) -> None:
        issue = EvidenceIssue(code=ReasonCode.BASELINE_STALE, detail="Baseline is 30h old.")
        events = narrate_assessment(
            "load_policy_node",
            {"discovery_issues": []},
            {"project_policy": _policy(), "discovery_issues": [issue]},
        )
        self.assertEqual([e.status for e in events], [S.PASSED, S.BLOCKED])
        self.assertIn("v4 for acme/shop", events[0].message)
        self.assertEqual(events[1].message, "BASELINE_STALE: Baseline is 30h old.")

    def test_only_issues_added_by_the_step_are_reported(self) -> None:
        old = EvidenceIssue(code=ReasonCode.POLICY_MISSING, detail="old")
        new = EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail="new")
        events = narrate_assessment(
            "calculate_static_radius_node",
            {"discovery_issues": [old]},
            {
                "discovery_issues": [old, new],
                "baseline_snapshot_id": "snap",
                "target_services": [f"{URN}:order"],
                "upstream_services": [f"{URN}:front"],
                "downstream_services": [],
            },
        )
        self.assertEqual([e.status for e in events], [S.PASSED, S.BLOCKED])
        self.assertIn("shop/order changes; 1 caller and 0 dependencies", events[0].message)
        self.assertEqual(events[1].message, "DISCOVERY_GAP: new")

    def test_live_check_states(self) -> None:
        coverage = LiveCoverage(edge_count=3, observed_services=[f"{URN}:order"], uncovered_services=[f"{URN}:admin"])
        uncovered = narrate_assessment(
            "validate_runtime_telemetry_node",
            {},
            {
                "telemetry_status": TelemetryStatus(
                    source="live", runtime_evidence=RuntimeEvidenceState.LIVE_UNCOVERED, coverage=coverage
                )
            },
        )
        self.assertEqual(uncovered[0].status, S.WARNING)
        self.assertIn("Not seen recently enough: shop/admin", uncovered[0].details)

        live = narrate_assessment(
            "validate_runtime_telemetry_node",
            {},
            {
                "telemetry_status": TelemetryStatus(
                    source="live",
                    runtime_evidence=RuntimeEvidenceState.LIVE,
                    coverage=coverage.model_copy(update={"uncovered_services": []}),
                ),
            },
        )
        self.assertEqual([e.status for e in live], [S.PASSED])

    def test_order_risk_and_verdict(self) -> None:
        plan = DeploymentPlan(change_type=ContractChange.ADDITIVE, sequence=[f"{URN}:db", f"{URN}:order"])
        (order,) = narrate_assessment("sequence_deployment_node", {}, {"deployment_plan": plan})
        self.assertEqual(order.message, "Additive change: deploy shop/db → shop/order.")
        cyclic = plan.model_copy(update={"cyclic_services": [f"{URN}:db"]})
        self.assertEqual(narrate_assessment("sequence_deployment_node", {}, {"deployment_plan": cyclic})[0].status, S.BLOCKED)
        (risk,) = narrate_assessment("assess_risk_and_sequence_node", {}, {"advisory_assessment": None})
        self.assertIn("Not consulted", risk.message)
        assessment = ReleaseAssessment(
            approved=False,
            actionable=False,
            reason_codes=[ReasonCode.DISCOVERY_GAP, ReasonCode.COMPATIBILITY_UNVERIFIED],
            deployment_sequence=[f"{URN}:order"],
            justification="j",
            evidence=AssessmentEvidence(pr_id="PR-1"),
        )
        (verdict,) = narrate_assessment("compile_evidence_node", {}, {"release_assessment": assessment})
        self.assertEqual((verdict.status, verdict.message), (S.BLOCKED, "Not approved: DISCOVERY_GAP, COMPATIBILITY_UNVERIFIED."))

    def test_long_messages_keep_the_full_text_in_details(self) -> None:
        issue = EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail="x" * 400)
        (event,) = narrate_assessment("extract_trigger_node", {"discovery_issues": []}, {"discovery_issues": [issue]})
        self.assertLessEqual(len(event.message), 280)
        self.assertEqual(event.details[0], f"DISCOVERY_GAP: {'x' * 400}")

    def test_short_names(self) -> None:
        self.assertEqual(short_name(f"{URN}:order"), "shop/order")
        self.assertEqual(short_name("unresolved:10.0.0.1"), "unresolved:10.0.0.1")


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RunLogStore()
        for module in (runs, api, webhooks):
            patcher = patch.object(module, "RUN_LOGS", self.store)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.baselines = BaselineStore(Path(tmp.name))
        patcher = patch.object(api, "BASELINE_STORE", self.baselines)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_runs_endpoints(self) -> None:
        self.store.start("r1", "assessment", "Assess PR-1", "acme", "shop")
        self.store.add("r1", _draft("one"))
        self.store.add("r1", _draft("two"))
        self.assertEqual([s.run_id for s in runs.list_runs()], ["r1"])
        page = runs.run_events("r1", after=1)
        self.assertEqual([e.message for e in page.events], ["two"])
        with self.assertRaises(HTTPException) as caught:
            runs.run_events("nope", after=0)
        self.assertEqual(caught.exception.status_code, 404)

    def _graph(self) -> KnowledgeGraph:
        return KnowledgeGraph(
            services=[ServiceNode(service_id=f"{URN}:order", name="order")],
            edges=[],
            discovery=[
                DiscoveryStageReport(source=method, status=DiscoveryStatus.SUCCESS)
                for method in DiscoveryMethod
            ],
            scan=ScanContext(environment="prod", cluster="aks-1", namespaces=["shop"]),
        )

    def test_onboarding_job_logs_steps_and_keeps_the_log_with_the_snapshot(self) -> None:
        scope = BaselineScope(tenant="acme", project_id="shop")

        def stream(state: Any, stream_mode: str) -> Any:
            stored = self.baselines.write(scope, self._graph())
            yield {"persist_to_graph_db": {"baseline_snapshot_id": stored.snapshot_id}}

        state: Any = {"tenant": "acme", "project_id": "shop"}
        self.store.start("job", "onboarding", "Onboard acme/shop", "acme", "shop")
        with patch.object(api, "discovery_app", MagicMock(stream=stream)):
            asyncio.run(api.run_discovery_job("job", state))

        summary, events = self.store.get("job") or (None, [])
        assert summary is not None
        self.assertEqual(summary.state, "completed")
        self.assertIn("1 services, 0 dependencies", summary.outcome)
        (snapshot_id,) = self.baselines.snapshot_ids(scope)
        self.assertEqual([e.message for e in events], [f"Saved baseline snapshot {snapshot_id}."])
        self.assertEqual(api.get_baseline_run_log("acme", "shop", snapshot_id), events)
        # The kept log is not mistaken for a snapshot.
        self.assertEqual(self.baselines.snapshot_ids(scope), [snapshot_id])

    def test_baseline_run_log_errors(self) -> None:
        for snapshot_id, code in [("20260101T000000000000Z-abcdef12", 404), ("../x", 422)]:
            with self.subTest(snapshot_id=snapshot_id):
                with self.assertRaises(HTTPException) as caught:
                    api.get_baseline_run_log("acme", "shop", snapshot_id)
                self.assertEqual(caught.exception.status_code, code)

    def test_failed_assessment_is_logged_and_finished(self) -> None:
        coordinator = MagicMock()
        coordinator.stream.side_effect = RuntimeError("anthropic down")
        self.store.start("job-a", "assessment", "Assess", "acme", "shop")
        with patch.object(webhooks, "coordinator_app", coordinator):
            asyncio.run(webhooks.run_assessment_job("job-a", "PR-1", {}))  # type: ignore[arg-type]
        summary, events = self.store.get("job-a") or (None, [])
        assert summary is not None
        self.assertEqual((summary.state, summary.outcome), ("failed", "anthropic down"))
        self.assertEqual(events[-1].status, S.FAILED)


if __name__ == "__main__":
    unittest.main()


class RevisionNarrationTests(unittest.TestCase):
    def test_head_scan_and_revision_issues_are_narrated(self) -> None:
        from src.coordinator.revision import HeadTopology
        from src.core.models import DependencyChange, EvidenceIssue, KnowledgeGraph, ReasonCode

        changes = HeadTopology(
            graph=KnowledgeGraph(services=[], edges=[]),
            added=[DependencyChange(source_id=f"{URN}:order", target_id="unresolved:fraud-check")],
        )
        issue = EvidenceIssue(code=ReasonCode.REVISION_UNVERIFIED, detail="base is not an ancestor")
        events = narrate_assessment(
            "verify_revision_node", {}, {"dependency_changes": changes, "discovery_issues": [issue]}
        )
        self.assertEqual([e.status for e in events], [S.PASSED, S.BLOCKED])
        self.assertIn("1 dependency added, 0 removed", events[0].message)
        self.assertEqual(events[1].message, "REVISION_UNVERIFIED: base is not an ancestor")
