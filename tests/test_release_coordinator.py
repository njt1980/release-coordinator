"""Tests for the LLM-backed risk reasoning stage of the release coordinator."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from src.coordinator import nodes as coordinator_nodes
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryResult,
    PullRequestPayload,
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
    diff="--- a\n+++ b\n",
)


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
        target_services=[TARGET],
        upstream_services=[f"{URN}:store-front"],
        downstream_services=[f"{URN}:rabbitmq"],
        baseline_deployment_order=[f"{URN}:rabbitmq", TARGET, f"{URN}:store-front"],
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
    def test_returns_validated_assessment_and_sends_diff(self) -> None:
        verdict = RiskAssessment(
            risk_score=0.75,
            approved=True,
            deployment_order=[f"{URN}:rabbitmq", TARGET, f"{URN}:store-front"],
            justification="store-front posts orders without customerId.",
        )
        client = _fake_client("end_turn", verdict)

        result = RiskReasoner(model="test-model", client=client).assess(_request())

        self.assertEqual(result, verdict)
        kwargs = client.beta.messages.parse.call_args.kwargs
        self.assertEqual(kwargs["model"], "test-model")
        self.assertIs(kwargs["output_format"], RiskAssessment)
        self.assertIn("<diff>\n--- a\n+++ b\n", kwargs["messages"][0]["content"])

    def test_rejects_unknown_or_missing_services(self) -> None:
        for order in [[f"{URN}:ghost", TARGET], [f"{URN}:store-front"]]:
            with self.subTest(order=order):
                client = _fake_client(
                    "end_turn",
                    RiskAssessment(
                        risk_score=0.1, approved=True, deployment_order=order, justification="x"
                    ),
                )
                with self.assertRaises(RiskReasoningError):
                    RiskReasoner(model="m", client=client).assess(_request())

    def test_accepts_consumers_first_order_for_breaking_change(self) -> None:
        """PR-2048 shape: customerId becomes required and the queue message
        gains schemaVersion 2, so callers and the queue consumer must deploy
        before the target. The validator must not force the additive baseline."""
        consumers = [
            f"{URN}:makeline-service",
            f"{URN}:store-admin",
            f"{URN}:store-front",
        ]
        request = RiskAssessmentRequest(
            pull_request=PR,
            target_services=[TARGET],
            upstream_services=consumers,
            downstream_services=[],
            baseline_deployment_order=[TARGET, *consumers],
            matched_edges=[],
            telemetry=TelemetryStatus(source="knowledge_graph"),
        )
        verdict = RiskAssessment(
            risk_score=0.8,
            approved=False,
            deployment_order=[*consumers, TARGET],
            justification="Callers must send customerId before it is required.",
        )
        client = _fake_client("end_turn", verdict)

        result = RiskReasoner(model="m", client=client).assess(request)

        self.assertEqual(result.deployment_order, [*consumers, TARGET])

    def test_system_prompt_orders_breaking_changes_consumers_first(self) -> None:
        prompt = SYSTEM_PROMPT.lower()
        self.assertIn("tightening or breaking", prompt)
        self.assertIn("goes before the target", prompt)
        self.assertNotIn("deviate from it only", prompt)

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
                runtime_observed_edges=[f"{URN}:store-front -> {TARGET}"],
            ),
        )

    def test_assess_node_passes_radius_and_baseline_to_reasoner(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": [f"{URN}:store-front"],
            "downstream_services": [f"{URN}:rabbitmq"],
            "matched_edges": [],
            "telemetry_status": TelemetryStatus(source="knowledge_graph"),
        }
        verdict = RiskAssessment(
            risk_score=0.4,
            approved=True,
            deployment_order=[TARGET, f"{URN}:store-front"],
            justification="why",
        )
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            reasoner_cls.return_value.assess.return_value = verdict
            result = coordinator_nodes.assess_risk_and_sequence_node(state)

        request = reasoner_cls.return_value.assess.call_args.args[0]
        self.assertEqual(
            request.baseline_deployment_order,
            [f"{URN}:rabbitmq", TARGET, f"{URN}:store-front"],
        )
        self.assertEqual(
            result,
            {
                "risk_score": 0.4,
                "approved": True,
                "deployment_sequence": [TARGET, f"{URN}:store-front"],
                "justification": "why",
            },
        )

    def test_assess_node_short_circuits_without_calling_llm(self) -> None:
        issues = [
            "No complete discovery baseline found for default/default; "
            "run discovery first.",
            "Live telemetry FAILED: query timed out",
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
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            result = coordinator_nodes.assess_risk_and_sequence_node(state)

        reasoner_cls.assert_not_called()
        self.assertEqual(result["risk_score"], 1.0)
        self.assertIs(result["approved"], False)
        self.assertEqual(
            result["deployment_sequence"],
            [f"{URN}:rabbitmq", TARGET, f"{URN}:store-front"],
        )
        justification = result["justification"]
        assert isinstance(justification, str)
        self.assertIn("without LLM risk assessment", justification)
        self.assertIn(f"(1) {issues[0]}", justification)
        self.assertIn(f"(2) {issues[1]}", justification)

    def test_short_circuit_verdict_is_a_valid_release_assessment(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "discovery_issues": ["RUNTIME_TELEMETRY PARTIAL: ns-b: 403"],
        }
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            state.update(coordinator_nodes.assess_risk_and_sequence_node(state))
        reasoner_cls.assert_not_called()

        assessment = coordinator_nodes.compile_evidence_node(state)["release_assessment"]

        self.assertIsInstance(assessment, ReleaseAssessment)
        self.assertFalse(assessment.approved)
        self.assertEqual(assessment.risk_score, 1.0)
        self.assertEqual(assessment.deployment_sequence, [TARGET])

    def test_live_telemetry_failure_becomes_discovery_issue(self) -> None:
        state: Any = {
            "workspace_id": "ws-1",
            "target_services": [TARGET],
            "discovery_issues": ["existing issue"],
        }
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls:
            extractor = extractor_cls.return_value
            extractor.get_active_dependencies.return_value = DiscoveryResult.failed(
                "query timed out"
            )
            extractor.get_runtime_metrics.return_value = {}
            result = coordinator_nodes.validate_runtime_telemetry_node(state)

        self.assertEqual(
            result["discovery_issues"],
            ["existing issue", "Live telemetry FAILED: query timed out"],
        )

    def test_baseline_order_deploys_deepest_providers_first(self) -> None:
        # Traversal order is nearest first: TARGET -> queue -> storage.
        order = coordinator_nodes.baseline_deployment_order(
            [TARGET],
            upstream=[f"{URN}:store-front", f"{URN}:web"],
            downstream=[f"{URN}:queue", f"{URN}:storage"],
        )
        self.assertEqual(
            order,
            [f"{URN}:storage", f"{URN}:queue", TARGET, f"{URN}:store-front", f"{URN}:web"],
        )

    def test_live_metrics_are_filtered_by_fquri_service_name(self) -> None:
        state: Any = {
            "workspace_id": "ws-1",
            "target_services": [TARGET],
            "downstream_services": [f"{URN}:rabbitmq"],
        }
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls:
            extractor = extractor_cls.return_value
            extractor.get_active_dependencies.return_value = DiscoveryResult.success([])
            extractor.get_runtime_metrics.return_value = {
                "order-service": {"error_rate": 0.1},
                "rabbitmq": {"error_rate": 0.0},
                "unrelated": {"error_rate": 0.9},
            }
            result = coordinator_nodes.validate_runtime_telemetry_node(state)

        status = result["telemetry_status"]
        assert isinstance(status, TelemetryStatus)
        self.assertEqual(set(status.runtime_metrics), {"order-service", "rabbitmq"})

    def test_compile_node_emits_typed_release_assessment(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": [f"{URN}:store-front"],
            "downstream_services": [],
            "risk_score": 0.8,
            "approved": False,
            "deployment_sequence": [f"{URN}:store-front", TARGET],
            "justification": "store-front must send customerId first.",
        }

        result = coordinator_nodes.compile_evidence_node(state)

        assessment = result["release_assessment"]
        self.assertIsInstance(assessment, ReleaseAssessment)
        self.assertEqual(
            assessment.model_dump(),
            {
                "risk_score": 0.8,
                "approved": False,
                "deployment_sequence": [f"{URN}:store-front", TARGET],
                "justification": "store-front must send customerId first.",
            },
        )
        self.assertIn("**Approved:** No", result["evidence_report"])

    def test_compile_node_flags_demo_mode_baseline(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "risk_score": 0.2,
            "approved": True,
            "deployment_sequence": [TARGET],
            "justification": "ok",
            "baseline_snapshot_id": "20261112T000000000000Z-abcd1234",
            "baseline_demo_mode": True,
        }

        report = coordinator_nodes.compile_evidence_node(state)["evidence_report"]

        assert isinstance(report, str)
        self.assertIn("20261112T000000000000Z-abcd1234 (DEMO MODE", report)


if __name__ == "__main__":
    unittest.main()
