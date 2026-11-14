"""Tests for external-AI controls (T3c): per-project switch and redaction."""

import json
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from src.assistant.chat import Assistant, AssistantDisabledError, ChatContext
from src.assistant.tools import AssistantTools
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.policy import decide_release
from src.coordinator.sequencing import DeploymentPlan
from src.core.models import (
    AssessmentEvidence,
    ContractChange,
    EvidenceIssue,
    ReasonCode,
    ReleaseAssessment,
    RiskAssessment,
)
from src.core.redaction import merge_counts, redact
from src.orchestration.briefing import build_briefing
from src.tools.baseline_store import BaselineScope
from src.tools.policy_store import LoadedPolicy, PolicyMissingError, parse_policy
from src.tools.risk_reasoner import RiskReasoner

URN = "urn:release-coord:acme:prod:aks-1:shop"
PLAN = DeploymentPlan(change_type=ContractChange.ADDITIVE, sequence=[f"{URN}:orders"])


def _policy(extra: str = "") -> Any:
    return parse_policy(
        "policy_version: 1\ntenant: acme\nproject_id: shop\nenvironment: prod\ncluster: aks-1\n"
        "required_sources: [STATIC_CODE]\nruntime_evidence: baseline\n" + extra,
        BaselineScope(tenant="acme", project_id="shop"),
    )


class RedactionTests(unittest.TestCase):
    def test_known_secret_shapes_and_emails(self) -> None:
        cases = {
            "Server=sql;User ID=app;Password=S3cr3t!pass;Encrypt=true": ("connection_secret", "S3cr3t!pass"),
            "amqp://guest:hunter22@rabbitmq:5672/": ("url_credentials", "hunter22"),
            'const apiKey = "abcd1234efgh5678";': ("assigned_secret", "abcd1234efgh5678"),
            "KEY=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA": ("token", "sk-ant-api03"),
            "SharedAccessKeyName=Root;SharedAccessKey=abc123XYZ=": ("connection_secret", "abc123XYZ="),
            "// ask jane.doe@contoso.com": ("email", "jane.doe@contoso.com"),
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345": ("token", "abcdefghijklmnop"),
            "?sv=2022&sig=AbCdEfGhIjKlMnOpQrSt%2B%3D": ("token", "AbCdEfGh"),
            "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----": ("private_key", "MIIB"),
        }
        for text, (kind, secret) in cases.items():
            with self.subTest(text=text):
                result = redact(text)
                self.assertEqual(result.counts, {kind: 1})
                self.assertNotIn(secret, result.text)
                self.assertIn(f"[redacted:{kind}]", result.text)

    def test_ordinary_code_is_left_alone(self) -> None:
        for text in [
            "var timeout = 30; // tokens per second: 5",
            "services.AddHttpClient(\"orders\", c => c.BaseAddress = new Uri(\"http://orders:8080\"));",
            "password_min_length = 12",
        ]:
            with self.subTest(text=text):
                self.assertEqual(redact(text).text, text)

    def test_redacting_twice_changes_nothing(self) -> None:
        once = redact("Password=hunter22pass;")
        twice = redact(once.text)
        self.assertEqual((twice.text, twice.counts), (once.text, {}))
        self.assertEqual(merge_counts({"email": 1}, {"email": 2, "token": 1}), {"email": 3, "token": 1})


class VerdictTests(unittest.TestCase):
    def test_disabled_model_with_complete_evidence_is_the_persons_call(self) -> None:
        verdict = decide_release([], PLAN, None, advisory_disabled=True)
        baseline = decide_release([], PLAN, RiskAssessment(risk_score=0.1, approved=True, justification="ok"))
        # The switch adds ADVISORY_DISABLED, which itself doesn't limit actionability.
        self.assertEqual(verdict.reason_codes, [*baseline.reason_codes, ReasonCode.ADVISORY_DISABLED])
        self.assertFalse(ReasonCode.ADVISORY_DISABLED.limits_actionability)
        self.assertEqual(verdict.actionable, baseline.actionable)
        self.assertFalse(verdict.approved)
        self.assertIn("external_ai: false", verdict.justification)

    def test_evidence_issues_still_come_first(self) -> None:
        issue = EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail="gap")
        verdict = decide_release([issue], PLAN, None, advisory_disabled=True)
        self.assertIn(ReasonCode.DISCOVERY_GAP, verdict.reason_codes)
        self.assertNotIn(ReasonCode.ADVISORY_DISABLED, verdict.reason_codes)
        self.assertFalse(verdict.actionable)

    def test_missing_advisory_without_the_switch_is_still_an_error(self) -> None:
        with self.assertRaises(ValueError):
            decide_release([], PLAN, None)

    def test_headline(self) -> None:
        assessment = ReleaseAssessment(
            approved=False,
            actionable=True,
            reason_codes=[ReasonCode.ADVISORY_DISABLED],
            deployment_sequence=PLAN.sequence,
            justification="x",
            evidence=AssessmentEvidence(pr_id="PR-1"),
        )
        self.assertTrue(build_briefing(assessment).headline.startswith("Your call: the evidence is complete"))


class RiskNodeTests(unittest.TestCase):
    def _state(self, policy: Any) -> Any:
        from src.coordinator.state import PullRequestPayload  # noqa: PLC0415

        return {
            "pull_request": PullRequestPayload(
                pr_id="PR-1", title="t", target_service=f"{URN}:orders", change_type=ContractChange.ADDITIVE,
                diff="+ Password=hunter22pass;\n+ // owner jane@contoso.com",
            ),
            "target_services": [f"{URN}:orders"],
            "deployment_plan": PLAN,
            "deployment_sequence": PLAN.sequence,
            "project_policy": LoadedPolicy(policy=policy, sha256="0" * 64),
        }

    def test_policy_switch_keeps_the_model_out(self) -> None:
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            result = coordinator_nodes.assess_risk_and_sequence_node(self._state(_policy("external_ai: false\n")))
        reasoner_cls.assert_not_called()
        self.assertIsNone(result["advisory_assessment"])
        self.assertIn(ReasonCode.ADVISORY_DISABLED, result["verdict"].reason_codes)  # type: ignore[attr-defined]

    def test_reasoner_sends_only_redacted_text(self) -> None:
        client = MagicMock()
        client.beta.messages.parse.return_value = MagicMock(
            stop_reason="end_turn", parsed_output=RiskAssessment(risk_score=0.1, approved=True, justification="ok")
        )
        reasoner = RiskReasoner(model="m", client=client)
        with patch.object(coordinator_nodes, "RiskReasoner", return_value=reasoner):
            result = coordinator_nodes.assess_risk_and_sequence_node(self._state(_policy()))
        sent = client.beta.messages.parse.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("hunter22pass", sent)
        self.assertNotIn("jane@contoso.com", sent)
        self.assertEqual(result["advisory_redactions"], {"connection_secret": 1, "email": 1})


class AssistantTests(unittest.TestCase):
    def setUp(self) -> None:
        policies = MagicMock()

        def load(scope: BaselineScope) -> Any:
            if scope.project_id == "none":
                raise PolicyMissingError("none")
            extra = "external_ai: false\n" if scope.project_id == "shop" else ""
            policy = parse_policy(
                f"policy_version: 1\ntenant: acme\nproject_id: {scope.project_id}\nenvironment: prod\n"
                "cluster: aks-1\nrequired_sources: [STATIC_CODE]\nruntime_evidence: baseline\n" + extra,
                scope,
            )
            return LoadedPolicy(policy=policy, sha256="0" * 64)

        policies.load.side_effect = load
        runs = MagicMock()
        runs.summaries.return_value = [
            MagicMock(tenant="acme", project_id="shop", model_dump=lambda **_: {"run": "shop"}),
            MagicMock(tenant="acme", project_id="other", model_dump=lambda **_: {"run": "other"}),
        ]
        self.tools = AssistantTools(baselines=MagicMock(), policies=policies, runs=runs)

    def test_switched_off_projects_are_never_sent(self) -> None:
        self.assertFalse(self.tools.external_ai_allowed("acme", "shop"))
        self.assertTrue(self.tools.external_ai_allowed("acme", "none"))
        text, is_error = self.tools.run("get_policy", {"tenant": "acme", "project_id": "shop"})
        self.assertTrue(is_error)
        self.assertIn("External AI is turned off for project acme/shop", text)
        listed, _ = self.tools.run("list_runs", {})
        self.assertEqual(json.loads(listed), [{"run": "other"}])

    def test_tool_results_are_redacted(self) -> None:
        with patch.object(AssistantTools, "policy", return_value="owner: jane@contoso.com"):
            text, is_error = self.tools.run("get_policy", {"tenant": "acme", "project_id": "other"})
        self.assertFalse(is_error)
        self.assertEqual(text, "owner: [redacted:email]")

    def test_assistant_refuses_on_a_switched_off_project(self) -> None:
        client = MagicMock()
        assistant = Assistant(tools=self.tools, client=client, model="m")
        with self.assertRaises(AssistantDisabledError):
            assistant.ask("why?", ChatContext(tenant="acme", project_id="shop"))
        client.beta.messages.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
