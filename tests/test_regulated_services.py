"""Tests for regulated services in the policy and the briefing (T3d)."""

import unittest
from typing import Any

from src.coordinator.project_policy import regulated_impact
from src.core.models import (
    AssessmentEvidence,
    DependencyChange,
    ReasonCode,
    RegulatedImpact,
    ReleaseAssessment,
    RiskAssessment,
)
from src.orchestration.briefing import BriefingRank, build_briefing
from src.tools.baseline_store import BaselineScope
from src.tools.policy_store import PolicyInvalidError, parse_policy

URN = "urn:release-coord:acme:prod:aks-1"
IMAGING, VIEWER, BILLING = (f"{URN}:clinical:{n}" for n in ("imaging", "viewer", "billing"))


def _policy(extra: str) -> Any:
    return parse_policy(
        "policy_version: 1\ntenant: acme\nproject_id: shop\nenvironment: prod\ncluster: aks-1\n"
        "required_sources: [STATIC_CODE]\nruntime_evidence: baseline\n" + extra,
        BaselineScope(tenant="acme", project_id="shop"),
    )


POLICY = _policy(
    "regulated_services:\n"
    "  - service: Clinical/Imaging\n"
    "    classification: IEC 62304 class B\n"
)


class PolicyTests(unittest.TestCase):
    def test_services_are_namespaced_and_unique(self) -> None:
        self.assertEqual(POLICY.regulated_service("clinical/imaging").classification, "IEC 62304 class B")
        for bad in [
            "regulated_services:\n  - service: imaging\n",
            "regulated_services:\n  - service: a/b\n  - service: A/B\n",
        ]:
            with self.subTest(bad=bad), self.assertRaises(PolicyInvalidError):
                _policy(bad)


class ImpactTests(unittest.TestCase):
    def test_changed_service_and_dependency_changes_touch_it(self) -> None:
        impacts = regulated_impact(
            POLICY,
            [IMAGING, BILLING],
            [DependencyChange(source_id=VIEWER, target_id=IMAGING)],
            [DependencyChange(source_id=IMAGING, target_id=BILLING)],
        )
        self.assertEqual(
            impacts,
            [
                RegulatedImpact(
                    service=IMAGING,
                    classification="IEC 62304 class B",
                    reasons=[
                        "this pull request changes it",
                        "this pull request adds a dependency on it from clinical/viewer",
                        "this pull request removes its dependency on clinical/billing",
                    ],
                )
            ],
        )

    def test_untouched_or_unregulated_is_no_impact(self) -> None:
        self.assertEqual(regulated_impact(POLICY, [BILLING], [], []), [])
        self.assertEqual(regulated_impact(None, [IMAGING], [], []), [])


def _assessment(impacts: list[RegulatedImpact], approved: bool) -> ReleaseAssessment:
    return ReleaseAssessment(
        approved=approved,
        actionable=approved,
        reason_codes=[] if approved else [ReasonCode.DISCOVERY_GAP],
        deployment_sequence=[IMAGING],
        services_to_deploy=[IMAGING],
        justification="ok",
        evidence=AssessmentEvidence(pr_id="PR-1", regulated_impact=impacts),
        advisory_assessment=RiskAssessment(risk_score=0.1, approved=True, justification="ok") if approved else None,
    )


class BriefingTests(unittest.TestCase):
    IMPACT = RegulatedImpact(service=IMAGING, classification="IEC 62304 class B", reasons=["this pull request changes it"])

    def test_regulated_impact_is_the_first_decision(self) -> None:
        briefing = build_briefing(_assessment([self.IMPACT], approved=True))
        group = briefing.groups[0]
        self.assertEqual((group.root_key, group.rank), ("decision:regulatory", BriefingRank.DECISION))
        self.assertEqual(group.what_to_check, ["imaging (IEC 62304 class B): this pull request changes it"])
        self.assertEqual(briefing.decisions_needed, 1)
        self.assertIn("decide whether regulatory review is needed", briefing.headline)

    def test_no_impact_no_group(self) -> None:
        briefing = build_briefing(_assessment([], approved=True))
        self.assertNotIn("decision:regulatory", [g.root_key for g in briefing.groups])
        self.assertNotIn("regulatory", briefing.headline)


if __name__ == "__main__":
    unittest.main()
