"""Tests for output origin labels and the intended-use statement (T3b)."""

import unittest
from pathlib import Path

from src.core.models import AssessmentEvidence, ReleaseAssessment, RiskAssessment
from src.orchestration.briefing import Origin, build_briefing, render_markdown
from tests.test_briefing import pr_b_assessment

TARGET = "urn:release-coord:acme:prod:aks-1:shop:orders"
ROOT = Path(__file__).resolve().parents[1]


def _approved() -> ReleaseAssessment:
    return ReleaseAssessment(
        approved=True,
        actionable=True,
        deployment_sequence=[TARGET],
        justification="ok",
        evidence=AssessmentEvidence(pr_id="PR-1"),
        advisory_assessment=RiskAssessment(risk_score=0.2, approved=True, justification="Adds an optional field."),
    )


class OriginTests(unittest.TestCase):
    def test_every_group_has_an_origin_and_issues_are_computed(self) -> None:
        briefing = build_briefing(pr_b_assessment())
        self.assertTrue(briefing.groups)
        for group in briefing.groups:
            if group.issues:
                self.assertIs(group.origin, Origin.COMPUTED)
        accepted = [g for g in briefing.groups if g.root_key == "info:accepted"]
        self.assertTrue(all(g.origin is Origin.HUMAN for g in accepted))

    def test_model_opinion_is_shown_apart_and_labelled(self) -> None:
        briefing = build_briefing(_approved())
        assert briefing.advisory is not None
        self.assertIs(briefing.advisory.origin, Origin.AI_GENERATED)
        self.assertEqual(briefing.advisory.justification, "Adds an optional field.")
        text = render_markdown(briefing)
        self.assertIn("### Advisory risk review [AI-generated]", text)
        self.assertIn("not part of the signed decision", text)

    def test_no_model_no_advisory(self) -> None:
        briefing = build_briefing(pr_b_assessment())
        self.assertIsNone(briefing.advisory)
        self.assertIn("[Computed]", render_markdown(briefing))


class StatementTests(unittest.TestCase):
    def test_statement_makes_no_compliance_claim(self) -> None:
        text = (ROOT / "docs" / "intended-use.md").read_text(encoding="utf-8")
        for required in ["Not validated software", "Not an approver", "Not a complete impact analysis", "AI-generated"]:
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
