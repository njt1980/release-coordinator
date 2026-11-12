"""Tests for the deterministic release policy and the verdict contract."""

import unittest

from pydantic import ValidationError

from src.coordinator.policy import decide_release, needs_model
from src.coordinator.sequencing import DeploymentPlan
from src.core.models import (
    AssessmentEvidence,
    ContractChange,
    EvidenceIssue,
    ReasonCode,
    ReleaseAssessment,
    RiskAssessment,
)

TARGET = "urn:release-coord:acme:unknown:unknown:shop:order-service"
PLAN = DeploymentPlan(change_type=ContractChange.ADDITIVE, sequence=[TARGET])
VERIFIED = PLAN.model_copy(update={"compatibility_verified": True})
CYCLIC = PLAN.model_copy(update={"cyclic_services": [TARGET]})
APPROVE = RiskAssessment(risk_score=0.2, approved=True, justification="fine")
REJECT = RiskAssessment(risk_score=0.9, approved=False, justification="risky")
DEMO = EvidenceIssue(code=ReasonCode.DEMO_EVIDENCE, detail="mock fixture")


class DecideReleaseTests(unittest.TestCase):
    def test_model_is_consulted_only_with_complete_evidence_and_acyclic_plan(
        self,
    ) -> None:
        self.assertTrue(needs_model([], PLAN))
        self.assertFalse(needs_model([DEMO], PLAN))
        self.assertFalse(needs_model([], CYCLIC))

    def test_approval_requires_verified_order_and_model_approval(self) -> None:
        verdict = decide_release([], VERIFIED, APPROVE)

        self.assertTrue(verdict.approved)
        self.assertTrue(verdict.actionable)
        self.assertEqual(verdict.reason_codes, [])
        self.assertEqual(verdict.risk_score, 0.2)

    def test_model_approval_cannot_override_unverified_compatibility(self) -> None:
        verdict = decide_release([], PLAN, APPROVE)

        self.assertFalse(verdict.approved)
        self.assertFalse(verdict.actionable)
        self.assertEqual(verdict.reason_codes, [ReasonCode.COMPATIBILITY_UNVERIFIED])
        self.assertIn("would approve", verdict.justification)

    def test_model_rejection_over_complete_evidence_is_actionable(self) -> None:
        verdict = decide_release([], VERIFIED, REJECT)

        self.assertFalse(verdict.approved)
        self.assertTrue(verdict.actionable)
        self.assertEqual(verdict.reason_codes, [ReasonCode.MODEL_REJECTED])
        self.assertEqual(verdict.risk_score, 0.9)

    def test_demo_evidence_is_never_approved_or_scored(self) -> None:
        verdict = decide_release([DEMO], VERIFIED, None)

        self.assertFalse(verdict.approved)
        self.assertFalse(verdict.actionable)
        self.assertIsNone(verdict.risk_score)
        self.assertEqual(verdict.reason_codes, [ReasonCode.DEMO_EVIDENCE])
        self.assertIn("[DEMO_EVIDENCE] mock fixture", verdict.justification)

    def test_drift_and_scope_mismatch_block_without_the_model(self) -> None:
        for code in (ReasonCode.BASELINE_DRIFT, ReasonCode.BASELINE_SCOPE_MISMATCH):
            with self.subTest(code=code):
                issue = EvidenceIssue(code=code, detail="detail")
                self.assertTrue(code.limits_actionability)
                self.assertFalse(needs_model([issue], VERIFIED))
                verdict = decide_release([issue], VERIFIED, None)
                self.assertFalse(verdict.approved)
                self.assertFalse(verdict.actionable)
                self.assertIsNone(verdict.risk_score)
                self.assertEqual(verdict.reason_codes, [code])

    def test_reason_codes_are_unique_and_ordered(self) -> None:
        issues = [
            EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail="a"),
            DEMO,
            EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail="b"),
        ]
        verdict = decide_release(issues, CYCLIC, None)

        self.assertEqual(
            verdict.reason_codes,
            [
                ReasonCode.DISCOVERY_GAP,
                ReasonCode.DEMO_EVIDENCE,
                ReasonCode.DEPENDENCY_CYCLE,
                ReasonCode.COMPATIBILITY_UNVERIFIED,
            ],
        )

    def test_cycle_without_issues_explains_the_cycle(self) -> None:
        verdict = decide_release([], CYCLIC, None)

        self.assertIsNone(verdict.risk_score)
        self.assertIn("dependency cycle", verdict.justification)

    def test_missing_required_advisory_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            decide_release([], PLAN, None)


class ReleaseAssessmentContractTests(unittest.TestCase):
    def _assessment(self, **overrides: object) -> ReleaseAssessment:
        fields: dict[str, object] = {
            "approved": False,
            "actionable": False,
            "reason_codes": [ReasonCode.DEMO_EVIDENCE],
            "deployment_sequence": [TARGET],
            "justification": "j",
            "evidence": AssessmentEvidence(pr_id="PR-1"),
        }
        fields.update(overrides)
        return ReleaseAssessment.model_validate(fields)

    def test_consistent_verdicts_validate(self) -> None:
        self._assessment()
        self._assessment(
            approved=True,
            actionable=True,
            reason_codes=[],
            risk_score=0.1,
            advisory_assessment=APPROVE,
        )
        self._assessment(actionable=True, reason_codes=[ReasonCode.MODEL_REJECTED])

    def test_inconsistent_verdicts_are_rejected(self) -> None:
        for overrides in [
            # A blocking reason cannot be actionable.
            {"actionable": True},
            # No limiting reason means the verdict must be actionable.
            {"reason_codes": [ReasonCode.MODEL_REJECTED]},
            # Approval cannot coexist with reason codes.
            {"approved": True, "advisory_assessment": APPROVE},
            # Approval needs the model's opinion.
            {"approved": True, "actionable": True, "reason_codes": []},
        ]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self._assessment(**overrides)


if __name__ == "__main__":
    unittest.main()
