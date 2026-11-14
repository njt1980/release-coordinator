"""Tests for the LLM-backed risk reasoning stage of the release coordinator."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from src.coordinator.test_scope import suggested_test_scope
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.policy import decide_release
from src.coordinator.sequencing import DeploymentPlan
from src.core.models import (
    MOCK_FIXTURE_PROVENANCE,
    AssessmentEvidence,
    ContractChange,
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryResult,
    EvidenceIssue,
    PullRequestPayload,
    ReasonCode,
    ReleaseAssessment,
    RiskAssessment,
    TelemetryStatus,
)
from src.tools.risk_reasoner import (
    RiskAssessmentRequest,
    RiskReasoner,
    RiskReasoningError,
    SYSTEM_PROMPT,
)

URN = "urn:release-coord:default:unknown:unknown:default"
TARGET = f"{URN}:order-service"
PR = PullRequestPayload(
    pr_id="PR-1",
    title="Change order schema",
    target_service=TARGET,
    change_type=ContractChange.ADDITIVE,
    diff="--- a\n+++ b\n",
)
SEQUENCE = [f"{URN}:rabbitmq", TARGET, f"{URN}:store-front"]


def _gap(detail: str) -> EvidenceIssue:
    return EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail=detail)


def _live_extractor(extractor_cls: MagicMock, result: DiscoveryResult) -> MagicMock:
    extractor = extractor_cls.return_value
    extractor.is_mock = False
    extractor.get_active_dependencies.return_value = result
    extractor.get_runtime_metrics.return_value = {}
    extractor.dropped_records = {}
    return extractor


def _edge(source: str, target: str, method: DiscoveryMethod) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[method],
        confidence_score=1.0,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _request() -> RiskAssessmentRequest:
    return RiskAssessmentRequest(
        pull_request=PR,
        change_type=ContractChange.ADDITIVE,
        target_services=[TARGET],
        upstream_services=[f"{URN}:store-front"],
        downstream_services=[f"{URN}:rabbitmq"],
        deployment_sequence=SEQUENCE,
        matched_edges=[],
        telemetry=TelemetryStatus(source="knowledge_graph"),
    )


def _fake_client(stop_reason: str, parsed: RiskAssessment | None) -> MagicMock:
    client = MagicMock()
    client.beta.messages.parse.return_value = SimpleNamespace(
        stop_reason=stop_reason, parsed_output=parsed
    )
    return client


class RiskReasonerTests(unittest.TestCase):
    def test_returns_assessment_and_sends_diff_and_fixed_sequence(self) -> None:
        verdict = RiskAssessment(
            risk_score=0.75,
            approved=True,
            justification="store-front posts orders without customerId.",
        )
        client = _fake_client("end_turn", verdict)

        result = RiskReasoner(model="test-model", client=client).assess(_request())

        self.assertEqual(result, verdict)
        kwargs = client.beta.messages.parse.call_args.kwargs
        self.assertEqual(kwargs["model"], "test-model")
        self.assertIs(kwargs["output_format"], RiskAssessment)
        content = kwargs["messages"][0]["content"]
        self.assertIn("<diff>\n--- a\n+++ b\n", content)
        self.assertIn('"deployment_sequence"', content)
        self.assertIn('"change_type": "additive"', content)

    def test_llm_output_schema_has_no_deployment_order(self) -> None:
        fields = set(RiskAssessment.model_json_schema()["properties"])
        self.assertEqual(fields, {"risk_score", "approved", "justification"})

    def test_system_prompt_restricts_llm_to_justifying_given_sequence(self) -> None:
        prompt = SYSTEM_PROMPT.lower()
        self.assertIn("you do not choose or change the", prompt)
        self.assertIn("never propose\na different order", prompt)
        self.assertIn("contradicts the declared change_type", prompt)
        self.assertNotIn("deployment_order", prompt)

    def test_refusal_and_missing_output_raise(self) -> None:
        for stop_reason in ["refusal", "end_turn"]:
            with self.subTest(stop_reason=stop_reason):
                client = _fake_client(stop_reason, None)
                with self.assertRaises(RiskReasoningError):
                    RiskReasoner(model="m", client=client).assess(_request())


class CoordinatorNodeTests(unittest.TestCase):
    def test_trigger_node_validates_payload_and_targets_pr_service(self) -> None:
        result = coordinator_nodes.extract_trigger_node(
            {"trigger_event": PR.model_dump(), "workspace_id": ""}
        )
        self.assertEqual(result["target_services"], [TARGET])
        self.assertEqual(result["pull_request"], PR)
        self.assertEqual(result["discovery_issues"], [])

    def test_trigger_node_turns_unmapped_files_into_an_issue(self) -> None:
        event = PR.model_copy(update={"repository": "shop"}).model_dump()
        result = coordinator_nodes.extract_trigger_node(
            {
                "trigger_event": event,
                "workspace_id": "",
                "unmapped_files": ["db/migrations/0042.sql", "Makefile"],
            }
        )
        issues = result["discovery_issues"]
        assert isinstance(issues, list)
        self.assertEqual([issue.code for issue in issues], [ReasonCode.UNMAPPED_FILES])
        self.assertIn("2 changed file(s) in shop", issues[0].detail)
        self.assertIn("db/migrations/0042.sql, Makefile", issues[0].detail)

    def test_telemetry_from_knowledge_graph_when_no_workspace(self) -> None:
        state: Any = {
            "workspace_id": "",
            "matched_edges": [
                _edge(f"{URN}:store-front", TARGET, DiscoveryMethod.RUNTIME_TELEMETRY),
                _edge(TARGET, f"{URN}:rabbitmq", DiscoveryMethod.STATIC_CODE),
            ],
        }
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor:
            result = coordinator_nodes.validate_runtime_telemetry_node(state)
        extractor.assert_not_called()
        self.assertEqual(
            result["telemetry_status"],
            TelemetryStatus(
                source="knowledge_graph",
                baseline_runtime_edges=[f"{URN}:store-front -> {TARGET}"],
            ),
        )

    def test_sequence_node_sorts_before_llm_for_declared_change(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": [f"{URN}:store-front"],
            "downstream_services": [f"{URN}:rabbitmq"],
            "matched_edges": [
                _edge(f"{URN}:store-front", TARGET, DiscoveryMethod.STATIC_CODE),
                _edge(TARGET, f"{URN}:rabbitmq", DiscoveryMethod.STATIC_CODE),
            ],
        }
        additive = coordinator_nodes.sequence_deployment_node(state)
        self.assertEqual(additive["deployment_sequence"], SEQUENCE)

        state["pull_request"] = PR.model_copy(
            update={"change_type": ContractChange.BREAKING}
        )
        breaking = coordinator_nodes.sequence_deployment_node(state)
        self.assertEqual(breaking["deployment_sequence"], list(reversed(SEQUENCE)))

    def test_breaking_plan_reaches_llm_request_and_verdict(self) -> None:
        state: Any = {
            "pull_request": PR.model_copy(
                update={"change_type": ContractChange.BREAKING}
            ),
            "target_services": [TARGET],
            "upstream_services": [f"{URN}:store-front"],
            "downstream_services": [f"{URN}:rabbitmq"],
            "matched_edges": [
                _edge(f"{URN}:store-front", TARGET, DiscoveryMethod.STATIC_CODE),
                _edge(TARGET, f"{URN}:rabbitmq", DiscoveryMethod.STATIC_CODE),
            ],
            "telemetry_status": TelemetryStatus(source="knowledge_graph"),
        }
        state.update(coordinator_nodes.sequence_deployment_node(state))
        verdict = RiskAssessment(risk_score=0.8, approved=False, justification="why")
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            reasoner_cls.return_value.assess.return_value = verdict
            state.update(coordinator_nodes.assess_risk_and_sequence_node(state))

        request = reasoner_cls.return_value.assess.call_args.args[0]
        self.assertEqual(request.services_to_deploy, [TARGET])
        self.assertEqual(request.stale_consumers, [f"{URN}:store-front"])

        result = coordinator_nodes.compile_evidence_node(state)
        assessment = result["release_assessment"]
        self.assertEqual(assessment.deployment_sequence, list(reversed(SEQUENCE)))
        self.assertEqual(assessment.services_to_deploy, [TARGET])
        self.assertEqual(assessment.stale_consumers, [f"{URN}:store-front"])
        self.assertIn(f"**Services to Deploy:** {TARGET}", result["evidence_report"])

    def test_assess_node_passes_fixed_sequence_and_ignores_llm_order(self) -> None:
        plan = DeploymentPlan(change_type=ContractChange.ADDITIVE, sequence=SEQUENCE)
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": [f"{URN}:store-front"],
            "downstream_services": [f"{URN}:rabbitmq"],
            "matched_edges": [],
            "telemetry_status": TelemetryStatus(source="knowledge_graph"),
            "deployment_plan": plan,
            "deployment_sequence": SEQUENCE,
        }
        verdict = RiskAssessment(risk_score=0.4, approved=True, justification="why")
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            reasoner_cls.return_value.assess.return_value = verdict
            result = coordinator_nodes.assess_risk_and_sequence_node(state)

        request = reasoner_cls.return_value.assess.call_args.args[0]
        self.assertEqual(request.deployment_sequence, SEQUENCE)
        self.assertIs(request.change_type, ContractChange.ADDITIVE)
        self.assertEqual(result["advisory_assessment"], verdict)
        self.assertEqual(result["verdict"], decide_release([], plan, verdict))

    def test_assess_node_short_circuits_without_calling_llm(self) -> None:
        issues = [
            EvidenceIssue(
                code=ReasonCode.BASELINE_UNAVAILABLE,
                detail="No complete discovery baseline found for default/default; "
                "run discovery first.",
            ),
            _gap("Live telemetry FAILED: query timed out"),
        ]
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": [f"{URN}:store-front"],
            "downstream_services": [f"{URN}:rabbitmq"],
            "matched_edges": [],
            "telemetry_status": TelemetryStatus(source="live"),
            "discovery_issues": issues,
        }
        state.update(coordinator_nodes.sequence_deployment_node(state))
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            result = coordinator_nodes.assess_risk_and_sequence_node(state)

        reasoner_cls.assert_not_called()
        self.assertIsNone(result["advisory_assessment"])
        verdict = result["verdict"]
        self.assertIsNone(verdict.risk_score)
        self.assertFalse(verdict.approved)
        self.assertFalse(verdict.actionable)
        self.assertEqual(
            verdict.reason_codes,
            [
                ReasonCode.BASELINE_UNAVAILABLE,
                ReasonCode.DISCOVERY_GAP,
                ReasonCode.COMPATIBILITY_UNVERIFIED,
            ],
        )
        self.assertEqual(state["deployment_sequence"], SEQUENCE)
        self.assertIn("advisory model was not consulted", verdict.justification)
        self.assertIn(
            f"(1) [BASELINE_UNAVAILABLE] {issues[0].detail}", verdict.justification
        )
        self.assertIn(f"(2) [DISCOVERY_GAP] {issues[1].detail}", verdict.justification)

    def test_dependency_cycle_rejects_without_calling_llm(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": [f"{URN}:store-front"],
            "downstream_services": [f"{URN}:store-front"],
            "matched_edges": [
                _edge(f"{URN}:store-front", TARGET, DiscoveryMethod.STATIC_CODE),
                _edge(TARGET, f"{URN}:store-front", DiscoveryMethod.STATIC_CODE),
            ],
        }
        state.update(coordinator_nodes.sequence_deployment_node(state))
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            state.update(coordinator_nodes.assess_risk_and_sequence_node(state))

        reasoner_cls.assert_not_called()
        assessment = coordinator_nodes.compile_evidence_node(state)["release_assessment"]
        self.assertFalse(assessment.approved)
        self.assertFalse(assessment.actionable)
        self.assertFalse(assessment.safe_order_established)
        self.assertIsNone(assessment.risk_score)
        self.assertIn(ReasonCode.DEPENDENCY_CYCLE, assessment.reason_codes)
        self.assertIn("dependency cycle", assessment.justification)
        self.assertCountEqual(
            assessment.deployment_sequence, [TARGET, f"{URN}:store-front"]
        )

    def test_short_circuit_verdict_is_a_valid_release_assessment(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "discovery_issues": [_gap("RUNTIME_TELEMETRY PARTIAL: ns-b: 403")],
        }
        state.update(coordinator_nodes.sequence_deployment_node(state))
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            state.update(coordinator_nodes.assess_risk_and_sequence_node(state))
        reasoner_cls.assert_not_called()

        assessment = coordinator_nodes.compile_evidence_node(state)["release_assessment"]

        self.assertIsInstance(assessment, ReleaseAssessment)
        self.assertFalse(assessment.approved)
        self.assertIsNone(assessment.risk_score)
        self.assertEqual(assessment.deployment_sequence, [TARGET])
        self.assertEqual(assessment.evidence.pr_id, "PR-1")

    def test_live_telemetry_failure_becomes_discovery_issue(self) -> None:
        state: Any = {
            "workspace_id": "ws-1",
            "target_services": [TARGET],
            "discovery_issues": [_gap("existing issue")],
        }
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls:
            _live_extractor(extractor_cls, DiscoveryResult.failed("query timed out"))
            result = coordinator_nodes.validate_runtime_telemetry_node(state)

        self.assertEqual(
            result["discovery_issues"],
            [_gap("existing issue"), _gap("Live telemetry FAILED: query timed out")],
        )

    def test_mock_telemetry_is_demo_evidence_not_live(self) -> None:
        state: Any = {"workspace_id": "ws-1", "target_services": [TARGET]}
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls:
            extractor = _live_extractor(extractor_cls, DiscoveryResult.success([]))
            extractor.is_mock = True
            extractor.get_runtime_metrics.return_value = {
                "order-service": {"error_rate": 0.0}
            }
            result = coordinator_nodes.validate_runtime_telemetry_node(state)

        status = result["telemetry_status"]
        assert isinstance(status, TelemetryStatus)
        self.assertEqual(status.source, "mock")
        issues = result["discovery_issues"]
        assert isinstance(issues, list)
        self.assertEqual([issue.code for issue in issues], [ReasonCode.DEMO_EVIDENCE])

    def test_mock_fixture_edges_from_live_query_are_demo_evidence(self) -> None:
        mock_edge = DependencyEdge(
            source_service_id=f"{URN}:store-front",
            target_service_id=TARGET,
            discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
            confidence_score=0.0,
            last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            provenance=MOCK_FIXTURE_PROVENANCE,
        )
        state: Any = {"workspace_id": "ws-1", "target_services": [TARGET]}
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls:
            _live_extractor(extractor_cls, DiscoveryResult.success([mock_edge]))
            result = coordinator_nodes.validate_runtime_telemetry_node(state)

        status = result["telemetry_status"]
        assert isinstance(status, TelemetryStatus)
        self.assertEqual(status.source, "mock")

    def test_live_metrics_are_filtered_by_fquri_service_name(self) -> None:
        state: Any = {
            "workspace_id": "ws-1",
            "target_services": [TARGET],
            "downstream_services": [f"{URN}:rabbitmq"],
        }
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls:
            extractor = _live_extractor(extractor_cls, DiscoveryResult.success([]))
            extractor.get_runtime_metrics.return_value = {
                "order-service": {"error_rate": 0.1},
                "rabbitmq": {"error_rate": 0.0},
                "unrelated": {"error_rate": 0.9},
            }
            result = coordinator_nodes.validate_runtime_telemetry_node(state)

        status = result["telemetry_status"]
        assert isinstance(status, TelemetryStatus)
        self.assertEqual(status.source, "live")
        self.assertEqual(set(status.runtime_metrics), {TARGET, f"{URN}:rabbitmq"})

    def test_compile_node_emits_typed_release_assessment(self) -> None:
        sequence = [f"{URN}:store-front", TARGET]
        plan = DeploymentPlan(
            change_type=ContractChange.BREAKING,
            sequence=sequence,
            services_to_deploy=[TARGET],
            stale_consumers=[f"{URN}:store-front"],
        )
        advisory = RiskAssessment(
            risk_score=0.8,
            approved=False,
            justification="store-front must send customerId first.",
        )
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": [f"{URN}:store-front"],
            "downstream_services": [],
            "deployment_plan": plan,
            "advisory_assessment": advisory,
            "verdict": decide_release([], plan, advisory),
            "baseline_snapshot_id": "snap-1",
        }

        result = coordinator_nodes.compile_evidence_node(state)

        assessment = result["release_assessment"]
        self.assertIsInstance(assessment, ReleaseAssessment)
        self.assertEqual(
            assessment,
            ReleaseAssessment(
                approved=False,
                actionable=False,
                reason_codes=[
                    ReasonCode.COMPATIBILITY_UNVERIFIED,
                    ReasonCode.MODEL_REJECTED,
                ],
                risk_score=0.8,
                safe_order_established=False,
                deployment_sequence=sequence,
                services_to_deploy=[TARGET],
                stale_consumers=[f"{URN}:store-front"],
                justification=(
                    "Not approved (COMPATIBILITY_UNVERIFIED, MODEL_REJECTED). "
                    "The advisory model would reject this release: store-front "
                    "must send customerId first."
                ),
                advisory_assessment=advisory,
                evidence=AssessmentEvidence(
                    pr_id="PR-1",
                    baseline_snapshot_id="snap-1",
                    # No hop distances in this state: only the changed service.
                    test_scope=suggested_test_scope([TARGET], {}, {}),
                ),
            ),
        )
        report = result["evidence_report"]
        assert isinstance(report, str)
        self.assertIn("**Approved:** No", report)
        self.assertIn("**Actionable:** No", report)
        self.assertIn("**Risk Score:** 0.80", report)
        self.assertIn("**Advisory Model:** reject (risk 0.80)", report)

    def test_compile_node_flags_demo_mode_baseline(self) -> None:
        plan = DeploymentPlan(change_type=ContractChange.ADDITIVE, sequence=[TARGET])
        issues = [EvidenceIssue(code=ReasonCode.DEMO_EVIDENCE, detail="mock data")]
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "deployment_plan": plan,
            "discovery_issues": issues,
            "advisory_assessment": None,
            "verdict": decide_release(issues, plan, None),
            "baseline_snapshot_id": "20261112T000000000000Z-abcd1234",
            "baseline_demo_mode": True,
        }

        result = coordinator_nodes.compile_evidence_node(state)

        report = result["evidence_report"]
        assert isinstance(report, str)
        self.assertIn("20261112T000000000000Z-abcd1234 (DEMO MODE", report)
        self.assertIn("**Risk Score:** Not assessed", report)
        assessment = result["release_assessment"]
        self.assertTrue(assessment.evidence.demo_mode)
        self.assertFalse(assessment.approved)
        self.assertIn(ReasonCode.DEMO_EVIDENCE, assessment.reason_codes)

if __name__ == "__main__":
    unittest.main()
