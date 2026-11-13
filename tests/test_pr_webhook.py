"""Tests for PR webhook ingestion and changed-file to service mapping."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from src import webhooks
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.file_mapping import ChangedFileMapping, map_changed_files
from src.core.models import (
    TelemetryClusterFilter,
    AssessmentEvidence,
    ContractChange,
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    KnowledgeGraph,
    ReasonCode,
    ReleaseAssessment,
    RiskAssessment,
    RuntimeEvidenceState,
    ScanContext,
    ScannedRevision,
    ServiceNode,
)
from src.tools.baseline_store import BaselineScope, BaselineStore, new_snapshot_id
from src.tools.policy_store import LoadedPolicy, PolicyStore
from src.webhooks import PrAssessmentJob, PullRequestWebhook

URN = "urn:release-coord:acme:prod:aks-1:shop"
SHOP_REVISION = ScannedRevision(
    repository="Shop", branch="main", commit_sha="3f1c2aa" + "0" * 33
)
SCAN = ScanContext(
    environment="prod",
    cluster="aks-1",
    namespaces=["shop"],
    kubernetes_api_server="https://aks-1.example.azmk8s.io:443",
    telemetry_cluster=TelemetryClusterFilter(value="aks-1"),
    scanned_revisions=[
        SHOP_REVISION,
        ScannedRevision(repository="other-repo", branch="main", commit_sha="a" * 40),
    ],
)


def _id(name: str) -> str:
    return f"{URN}:{name}"


def _service(name: str, repository: str | None, root: str | None) -> ServiceNode:
    metadata = {"source": "STATIC_CODE"}
    if repository is not None:
        metadata["repository"] = repository
    if root is not None:
        metadata["service_root"] = root
    return ServiceNode(service_id=_id(name), name=name, metadata=metadata)


SERVICES = [
    _service("monorepo", "Shop", ""),
    _service("order-service", "Shop", "src/order-service"),
    _service("store-front", "Shop", "src/store-front"),
    _service("payments-api", "Shop", "services/payments/api"),
    _service("other-order", "other-repo", "src/order-service"),
    _service("rabbitmq", None, None),
]
GRAPH = KnowledgeGraph(services=SERVICES, edges=[])

REPORTS = [
    DiscoveryStageReport(source=method, status=DiscoveryStatus.SUCCESS)
    for method in DiscoveryMethod
]

POLICY_YAML = """\
policy_version: 3
tenant: acme
project_id: shop
environment: prod
cluster: aks-1
required_sources: [STATIC_CODE, K8S_MANIFEST, RUNTIME_TELEMETRY]
runtime_evidence: baseline
kubernetes_api_server: https://aks-1.example.azmk8s.io
telemetry_cluster:
  value: aks-1
exemptions:
  files:
    - pattern: "docs/*"
      reason: "Documentation only"
"""


class MapChangedFilesTests(unittest.TestCase):
    def test_files_map_to_deepest_service_root_in_the_same_repository(self) -> None:
        mapping = map_changed_files(
            GRAPH,
            "shop",  # repository names compare case-insensitively
            [
                "src/order-service/routes/root.js",
                "/src/order-service/schema/order.json",
                "./src/store-front/src/App.vue",
                "services/payments/api/main.py",
                "README.md",
            ],
        )

        self.assertEqual(
            mapping,
            ChangedFileMapping(
                target_services=sorted(
                    [
                        _id("monorepo"),
                        _id("order-service"),
                        _id("payments-api"),
                        _id("store-front"),
                    ]
                ),
                files_by_service={
                    _id("monorepo"): ["README.md"],
                    _id("order-service"): [
                        "src/order-service/routes/root.js",
                        "/src/order-service/schema/order.json",
                    ],
                    _id("payments-api"): ["services/payments/api/main.py"],
                    _id("store-front"): ["./src/store-front/src/App.vue"],
                },
                unmapped_files=[],
            ),
        )

    def test_root_prefix_must_match_whole_path_segments(self) -> None:
        graph = KnowledgeGraph(
            services=[_service("order-service", "shop", "src/order-service")],
            edges=[],
        )
        mapping = map_changed_files(
            graph, "shop", ["src/order-service-v2/app.js", "src/order-service"]
        )
        self.assertEqual(mapping.unmapped_files, ["src/order-service-v2/app.js"])
        self.assertEqual(mapping.target_services, [_id("order-service")])

    def test_other_repositories_and_services_without_roots_are_ignored(self) -> None:
        mapping = map_changed_files(GRAPH, "other-repo", ["src/order-service/a.js", "x.js"])
        self.assertEqual(mapping.target_services, [_id("other-order")])
        self.assertEqual(mapping.unmapped_files, ["x.js"])

        self.assertEqual(
            map_changed_files(GRAPH, "unknown-repo", ["src/order-service/a.js"]),
            ChangedFileMapping(unmapped_files=["src/order-service/a.js"]),
        )

    def test_escaping_and_ambiguous_paths_are_never_guessed(self) -> None:
        graph = KnowledgeGraph(
            services=[
                _service("api-a", "shop", "api"),
                _service("api-b", "shop", "api/"),
            ],
            edges=[],
        )
        mapping = map_changed_files(graph, "shop", ["api/main.py", "api/../etc/passwd"])
        self.assertEqual(mapping.target_services, [])
        self.assertEqual(mapping.unmapped_files, ["api/main.py", "api/../etc/passwd"])


def _payload(**overrides: Any) -> PullRequestWebhook:
    fields: dict[str, Any] = {
        "pr_id": "PR-2048",
        "base_sha": "3f1c2aa",
        "head_sha": "9b7e410c0ffee",
        "repository": "shop",
        "changed_files": ["src/order-service/routes/root.js", "docs/../../x"],
        "change_type": "breaking",
        "diff": "--- a\n+++ b\n",
        "tenant": "acme",
        "project_id": "shop",
    }
    fields.update(overrides)
    return PullRequestWebhook.model_validate(fields)


def _mapping(targets: list[str] | None = None) -> ChangedFileMapping:
    return ChangedFileMapping(target_services=targets or [_id("order-service")])


class PullRequestWebhookPayloadTests(unittest.TestCase):
    def test_rejects_malformed_payloads(self) -> None:
        for overrides in [
            {"base_sha": "not-a-sha"},
            {"changed_files": []},
            {"changed_files": ["  "]},
            {"change_type": "maybe"},
            {"diff": ""},
        ]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    _payload(**overrides)
        fields = _payload().model_dump()
        del fields["change_type"]
        with self.assertRaises(ValidationError):
            PullRequestWebhook.model_validate(fields)


class PrAssessmentWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        jobs_patch = patch.dict(webhooks._assessment_jobs, clear=True)
        jobs_patch.start()
        self.addCleanup(jobs_patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = BaselineStore(Path(tmp.name))
        store_patch = patch.object(webhooks, "BASELINE_STORE", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.scope = BaselineScope(tenant="acme", project_id="shop")
        policies = tempfile.TemporaryDirectory()
        self.addCleanup(policies.cleanup)
        self.policy_store = PolicyStore(Path(policies.name))
        for module in (coordinator_nodes, webhooks):
            policy_patch = patch.object(module, "POLICY_STORE", self.policy_store)
            policy_patch.start()
            self.addCleanup(policy_patch.stop)
        self.policy = self._write_policy(POLICY_YAML)

    def _write_policy(self, text: str) -> LoadedPolicy:
        path = self.policy_store.policy_path(self.scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self.policy_store.load(self.scope)

    def _graph(self, scan: ScanContext | None = SCAN) -> KnowledgeGraph:
        edge = DependencyEdge(
            source_service_id=_id("store-front"),
            target_service_id=_id("order-service"),
            discovery_methods=[DiscoveryMethod.STATIC_CODE],
            confidence_score=0.7,
            last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return KnowledgeGraph(services=SERVICES, edges=[edge], discovery=REPORTS, scan=scan)

    def _write_baseline(self) -> str:
        return self.store.write(self.scope, self._graph()).snapshot_id

    def _write_legacy_baseline(self) -> str:
        """Write a snapshot as 2a did, without a scan context."""
        snapshot_id = new_snapshot_id()
        directory = self.store.project_dir(self.scope)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{snapshot_id}.json").write_text(
            self._graph(scan=None).model_dump_json(), encoding="utf-8"
        )
        return snapshot_id

    def test_maps_files_and_schedules_coordinator_run(self) -> None:
        snapshot_id = self._write_baseline()
        tasks = BackgroundTasks()

        response = webhooks.pr_assessment_webhook(_payload(), tasks)

        self.assertEqual(response.baseline_snapshot_id, snapshot_id)
        self.assertEqual(response.target_services, [_id("order-service")])
        self.assertEqual(
            response.files_by_service,
            {_id("order-service"): ["src/order-service/routes/root.js"]},
        )
        self.assertEqual(response.unmapped_files, ["docs/../../x"])
        self.assertEqual(response.exempted_files, [])
        self.assertEqual(
            webhooks._assessment_jobs[response.job_id],
            PrAssessmentJob(job_id=response.job_id, pr_id="PR-2048", status="queued"),
        )
        self.assertEqual(len(tasks.tasks), 1)
        task = tasks.tasks[0]
        self.assertIs(task.func, webhooks.run_assessment_job)
        state = task.args[2]
        self.assertEqual(state["target_services"], [_id("order-service")])
        self.assertEqual(state["tenant"], "acme")
        self.assertEqual(state["baseline_snapshot_id"], snapshot_id)
        self.assertEqual(state["unmapped_files"], ["docs/../../x"])
        event = state["trigger_event"]
        self.assertEqual(event["change_type"], "breaking")
        self.assertEqual(event["target_service"], _id("order-service"))
        self.assertEqual(
            (event["repository"], event["base_sha"], event["head_sha"]),
            ("shop", "3f1c2aa", "9b7e410c0ffee"),
        )
        self.assertEqual(
            event["changed_files"], ["src/order-service/routes/root.js", "docs/../../x"]
        )

    def test_missing_baseline_is_a_conflict(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            webhooks.pr_assessment_webhook(_payload(), BackgroundTasks())
        self.assertEqual(raised.exception.status_code, 409)

    def test_no_mapped_service_is_unprocessable(self) -> None:
        self._write_baseline()
        tasks = BackgroundTasks()
        with self.assertRaises(HTTPException) as raised:
            webhooks.pr_assessment_webhook(
                _payload(repository="unknown-repo"), tasks
            )
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(tasks.tasks, [])
        self.assertEqual(webhooks._assessment_jobs, {})

    def test_job_records_completed_assessment(self) -> None:
        assessment = ReleaseAssessment(
            risk_score=0.8,
            approved=False,
            actionable=True,
            reason_codes=[ReasonCode.MODEL_REJECTED],
            deployment_sequence=[_id("store-front"), _id("order-service")],
            justification="store-front must send customerId first.",
            evidence=AssessmentEvidence(pr_id="PR-2048"),
        )
        coordinator = MagicMock()
        coordinator.invoke.return_value = {
            "release_assessment": assessment,
            "evidence_report": "## report",
        }
        state = webhooks.build_coordinator_state(_payload(), "snap-1", _mapping())
        with patch.object(webhooks, "coordinator_app", coordinator):
            asyncio.run(webhooks.run_assessment_job("job-1", "PR-2048", state))

        coordinator.invoke.assert_called_once_with(state)
        record = webhooks.get_pr_assessment("job-1")
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.assessment, assessment)
        self.assertEqual(record.evidence_report, "## report")

    def test_job_records_failure_and_unknown_job_is_404(self) -> None:
        coordinator = MagicMock()
        coordinator.invoke.side_effect = RuntimeError("anthropic down")
        state = webhooks.build_coordinator_state(_payload(), "snap-1", _mapping())
        with patch.object(webhooks, "coordinator_app", coordinator):
            asyncio.run(webhooks.run_assessment_job("job-2", "PR-2048", state))

        record = webhooks.get_pr_assessment("job-2")
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error_message, "anthropic down")
        with self.assertRaises(HTTPException) as raised:
            webhooks.get_pr_assessment("missing")
        self.assertEqual(raised.exception.status_code, 404)

    def _run_graph(
        self, payload: PullRequestWebhook, verdict: RiskAssessment
    ) -> tuple[dict[str, Any], MagicMock]:
        tasks = BackgroundTasks()
        webhooks.pr_assessment_webhook(payload, tasks)
        state = tasks.tasks[0].args[2]
        with (
            patch.object(coordinator_nodes, "BASELINE_STORE", self.store),
            patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls,
        ):
            reasoner_cls.return_value.assess.return_value = verdict
            final = webhooks.coordinator_app.invoke(state)
        return final, reasoner_cls

    def test_end_to_end_graph_uses_deterministic_sequence(self) -> None:
        snapshot_id = self._write_baseline()
        verdict = RiskAssessment(risk_score=0.8, approved=True, justification="why")
        payload = _payload(changed_files=["src/order-service/routes/root.js"])

        final, reasoner_cls = self._run_graph(payload, verdict)

        breaking_order = [_id("store-front"), _id("order-service")]
        request = reasoner_cls.return_value.assess.call_args.args[0]
        self.assertEqual(request.deployment_sequence, breaking_order)
        assessment = final["release_assessment"]
        self.assertEqual(assessment.deployment_sequence, breaking_order)
        self.assertEqual(assessment.services_to_deploy, [_id("order-service")])
        self.assertEqual(assessment.stale_consumers, [_id("store-front")])
        # The model approved, but no contract evidence proves the order safe.
        self.assertEqual(assessment.advisory_assessment, verdict)
        self.assertFalse(assessment.approved)
        self.assertFalse(assessment.actionable)
        self.assertFalse(assessment.safe_order_established)
        self.assertEqual(assessment.reason_codes, [ReasonCode.COMPATIBILITY_UNVERIFIED])
        self.assertEqual(assessment.risk_score, 0.8)
        self.assertEqual(
            assessment.evidence,
            AssessmentEvidence(
                pr_id="PR-2048",
                repository="shop",
                base_sha="3f1c2aa",
                head_sha="9b7e410c0ffee",
                baseline_snapshot_id=snapshot_id,
                baseline_created_at=final["baseline_created_at"],
                changed_files=["src/order-service/routes/root.js"],
                runtime_evidence=RuntimeEvidenceState.BASELINE,
                policy_version=3,
                policy_sha256=self.policy.sha256,
                baseline_environment="prod",
                baseline_cluster="aks-1",
                scanned_revisions=SCAN.scanned_revisions,
            ),
        )
        self.assertIsNotNone(assessment.evidence.baseline_created_at)
        report = final["evidence_report"]
        self.assertIn("**Release Policy:** version 3", report)
        self.assertIn("**Baseline Environment/Cluster:** prod/aks-1", report)
        # The PR's repository is "shop"; the scanned one is "Shop", so the
        # base comparison is not made, and both revisions are cited.
        self.assertIn(f"Shop@{SHOP_REVISION.commit_sha} (main)", report)
        self.assertIn(f"other-repo@{'a' * 40} (main)", report)

    def test_unmapped_files_block_assessment_without_llm(self) -> None:
        self._write_baseline()
        verdict = RiskAssessment(risk_score=0.1, approved=True, justification="why")

        final, reasoner_cls = self._run_graph(_payload(), verdict)

        reasoner_cls.assert_not_called()
        assessment = final["release_assessment"]
        self.assertFalse(assessment.approved)
        self.assertFalse(assessment.actionable)
        self.assertIsNone(assessment.risk_score)
        self.assertIn(ReasonCode.UNMAPPED_FILES, assessment.reason_codes)
        # The policy exempts docs/*, but a traversal path is never exempt.
        self.assertEqual(assessment.evidence.unmapped_files, ["docs/../../x"])
        self.assertEqual(assessment.evidence.exempted_files, [])
        self.assertIn("docs/../../x", assessment.justification)

    def test_missing_policy_blocks_assessment_without_llm(self) -> None:
        self._write_baseline()
        self.policy_store.policy_path(self.scope).unlink()
        verdict = RiskAssessment(risk_score=0.1, approved=True, justification="why")
        payload = _payload(changed_files=["src/order-service/routes/root.js"])

        final, reasoner_cls = self._run_graph(payload, verdict)

        reasoner_cls.assert_not_called()
        assessment = final["release_assessment"]
        self.assertFalse(assessment.approved)
        self.assertFalse(assessment.actionable)
        self.assertIn(ReasonCode.POLICY_MISSING, assessment.reason_codes)
        self.assertIsNone(assessment.evidence.policy_version)
        self.assertIn("**Release Policy:** None loaded", final["evidence_report"])

    def test_invalid_policy_blocks_assessment(self) -> None:
        self._write_baseline()
        path = self.policy_store.policy_path(self.scope)
        path.write_text(POLICY_YAML + "approve_everything: true\n", encoding="utf-8")
        verdict = RiskAssessment(risk_score=0.1, approved=True, justification="why")
        payload = _payload(changed_files=["src/order-service/routes/root.js"])

        final, reasoner_cls = self._run_graph(payload, verdict)

        reasoner_cls.assert_not_called()
        self.assertIn(
            ReasonCode.POLICY_INVALID, final["release_assessment"].reason_codes
        )

    def test_stale_pinned_baseline_blocks_assessment(self) -> None:
        self._write_baseline()
        tasks = BackgroundTasks()
        webhooks.pr_assessment_webhook(
            _payload(changed_files=["src/order-service/routes/root.js"]), tasks
        )
        state = tasks.tasks[0].args[2]
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        with (
            patch.object(coordinator_nodes, "BASELINE_STORE", self.store),
            patch.object(coordinator_nodes, "utc_now", return_value=later),
            patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls,
        ):
            final = webhooks.coordinator_app.invoke(state)

        reasoner_cls.assert_not_called()
        assessment = final["release_assessment"]
        self.assertFalse(assessment.actionable)
        self.assertEqual(
            assessment.reason_codes,
            [ReasonCode.BASELINE_STALE, ReasonCode.COMPATIBILITY_UNVERIFIED],
        )

    def test_assessment_stays_pinned_when_newer_baseline_appears(self) -> None:
        pinned = self._write_baseline()
        tasks = BackgroundTasks()
        webhooks.pr_assessment_webhook(
            _payload(changed_files=["src/order-service/routes/root.js"]), tasks
        )
        state = tasks.tasks[0].args[2]
        newer = self._write_baseline()
        self.assertNotEqual(newer, pinned)
        with (
            patch.object(coordinator_nodes, "BASELINE_STORE", self.store),
            patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls,
        ):
            reasoner_cls.return_value.assess.return_value = RiskAssessment(
                risk_score=0.1, approved=True, justification="why"
            )
            final = webhooks.coordinator_app.invoke(state)

        self.assertEqual(
            final["release_assessment"].evidence.baseline_snapshot_id, pinned
        )

    def test_response_reports_exempt_files_separately(self) -> None:
        self._write_baseline()
        # other-repo has no root-level service, so docs/ is owned by nobody.
        files = ["src/order-service/a.js", "docs/guide.md", "docs/a/b.md"]

        response = webhooks.pr_assessment_webhook(
            _payload(repository="other-repo", changed_files=files), BackgroundTasks()
        )

        # docs/* covers one directory level only.
        self.assertEqual(response.unmapped_files, ["docs/a/b.md"])
        self.assertEqual(
            [(e.subject, e.rule) for e in response.exempted_files],
            [("docs/guide.md", "docs/*")],
        )

    def test_response_exempts_nothing_without_a_usable_policy(self) -> None:
        self._write_baseline()
        files = ["src/order-service/a.js", "docs/guide.md"]
        path = self.policy_store.policy_path(self.scope)
        for name, write in [
            ("missing", path.unlink),
            ("invalid", lambda: path.write_text("policy_version: [", encoding="utf-8")),
        ]:
            with self.subTest(name):
                write()
                response = webhooks.pr_assessment_webhook(
                    _payload(repository="other-repo", changed_files=files),
                    BackgroundTasks(),
                )
                self.assertEqual(response.unmapped_files, ["docs/guide.md"])
                self.assertEqual(response.exempted_files, [])

    def test_legacy_snapshot_without_scan_context_blocks_assessment(self) -> None:
        snapshot_id = self._write_legacy_baseline()
        verdict = RiskAssessment(risk_score=0.1, approved=True, justification="why")
        payload = _payload(changed_files=["src/order-service/routes/root.js"])

        final, reasoner_cls = self._run_graph(payload, verdict)

        reasoner_cls.assert_not_called()
        assessment = final["release_assessment"]
        self.assertEqual(assessment.evidence.baseline_snapshot_id, snapshot_id)
        self.assertFalse(assessment.actionable)
        self.assertIn(ReasonCode.BASELINE_SCOPE_MISMATCH, assessment.reason_codes)
        self.assertIsNone(assessment.evidence.baseline_environment)
        self.assertEqual(assessment.evidence.scanned_revisions, [])
        self.assertIn("Not recorded", final["evidence_report"])

    def test_baseline_from_another_cluster_blocks_assessment(self) -> None:
        self._write_baseline()
        self.policy = self._write_policy(
            POLICY_YAML.replace("cluster: aks-1", "cluster: aks-2")
        )
        verdict = RiskAssessment(risk_score=0.1, approved=True, justification="why")
        payload = _payload(changed_files=["src/order-service/routes/root.js"])

        final, reasoner_cls = self._run_graph(payload, verdict)

        reasoner_cls.assert_not_called()
        assessment = final["release_assessment"]
        self.assertEqual(
            assessment.reason_codes,
            [ReasonCode.BASELINE_SCOPE_MISMATCH, ReasonCode.COMPATIBILITY_UNVERIFIED],
        )
        issue = assessment.issues[0]
        self.assertIn("scanned prod/aks-1", issue.detail)
        self.assertIn("releases to prod/aks-2", issue.detail)

    def test_report_flags_a_pr_base_other_than_the_scanned_revision(self) -> None:
        self._write_baseline()
        verdict = RiskAssessment(risk_score=0.1, approved=True, justification="why")
        same_base = _payload(
            repository="Shop", changed_files=["src/order-service/routes/root.js"]
        )
        other_base = _payload(
            repository="Shop",
            base_sha="beefcafe",
            changed_files=["src/order-service/routes/root.js"],
        )

        same, _ = self._run_graph(same_base, verdict)
        other, _ = self._run_graph(other_base, verdict)

        self.assertNotIn("differs", same["evidence_report"])
        self.assertIn("[PR base beefcafe differs]", other["evidence_report"])

    def test_coordinator_state_is_a_valid_pull_request(self) -> None:
        state = webhooks.build_coordinator_state(
            _payload(change_type=ContractChange.ADDITIVE),
            "snap-1",
            _mapping([_id("order-service"), _id("store-front")]),
        )
        self.assertEqual(state["trigger_event"]["title"], "PR PR-2048")
        self.assertEqual(
            state["target_services"], [_id("order-service"), _id("store-front")]
        )


if __name__ == "__main__":
    unittest.main()
