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

TARGET = "default/order-service"
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
        upstream_services=["default/store-front"],
        downstream_services=["default/rabbitmq"],
        baseline_deployment_order=["default/rabbitmq", TARGET, "default/store-front"],
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
            deployment_order=["default/rabbitmq", TARGET, "default/store-front"],
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
        for order in [["default/ghost", TARGET], ["default/store-front"]]:
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
            "default/makeline-service",
            "default/store-admin",
            "default/store-front",
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
                _edge("default/store-front", TARGET, DiscoveryMethod.RUNTIME_TELEMETRY),
                _edge(TARGET, "default/rabbitmq", DiscoveryMethod.STATIC_CODE),
            ],
        }
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor:
            result = coordinator_nodes.validate_runtime_telemetry_node(state)
        extractor.assert_not_called()
        self.assertEqual(
            result["telemetry_status"],
            TelemetryStatus(
                source="knowledge_graph",
                runtime_observed_edges=[f"default/store-front -> {TARGET}"],
            ),
        )

    def test_assess_node_passes_radius_and_baseline_to_reasoner(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": ["default/store-front"],
            "downstream_services": ["default/rabbitmq"],
            "matched_edges": [],
            "telemetry_status": TelemetryStatus(source="knowledge_graph"),
        }
        verdict = RiskAssessment(
            risk_score=0.4,
            approved=True,
            deployment_order=[TARGET, "default/store-front"],
            justification="why",
        )
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            reasoner_cls.return_value.assess.return_value = verdict
            result = coordinator_nodes.assess_risk_and_sequence_node(state)

        request = reasoner_cls.return_value.assess.call_args.args[0]
        self.assertEqual(
            request.baseline_deployment_order,
            ["default/rabbitmq", TARGET, "default/store-front"],
        )
        self.assertEqual(
            result,
            {
                "risk_score": 0.4,
                "approved": True,
                "deployment_sequence": [TARGET, "default/store-front"],
                "justification": "why",
            },
        )

    def test_compile_node_emits_typed_release_assessment(self) -> None:
        state: Any = {
            "pull_request": PR,
            "target_services": [TARGET],
            "upstream_services": ["default/store-front"],
            "downstream_services": [],
            "risk_score": 0.8,
            "approved": False,
            "deployment_sequence": ["default/store-front", TARGET],
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
                "deployment_sequence": ["default/store-front", TARGET],
                "justification": "store-front must send customerId first.",
            },
        )
        self.assertIn("**Approved:** No", result["evidence_report"])


if __name__ == "__main__":
    unittest.main()
