"""The release briefing (step 2b): grouped, ranked, faithful, fail-closed."""

import unittest

from pydantic import ValidationError

from src.core.issue_keys import files_key, unresolved_key
from src.core.models import (
    AssessmentEvidence,
    EvidenceIssue,
    Exemption,
    IssueSubject,
    IssueSubjectKind,
    ReasonCode,
    ReleaseAssessment,
    RiskAssessment,
    UnresolvedEntityKind,
)
from src.orchestration.briefing import (
    BriefingRank,
    ReleaseBriefing,
    build_briefing,
    render_markdown,
)

URN = "urn:release-coord:acme:lab:aks:eshop"
SERVICES = ["basket-api", "catalog-api", "ordering-api", "order-processor", "payment-processor",
            "webapp", "webhooks-api", "identity-api", "webhooksclient"]
TARGET = f"{URN}:basket-api"


def _env_gap(service: str, variable: str) -> EvidenceIssue:
    entity = f"unresolved:env:{service}/{variable.lower()}"
    return EvidenceIssue(
        code=ReasonCode.DISCOVERY_GAP,
        detail=f"Unresolved dependency: {entity}",
        subjects=[IssueSubject(kind=IssueSubjectKind.ENTITY, id=entity)],
        entity_kind=UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET,
        root_key=unresolved_key(f"env:{service}/{variable}", entity),
        via=[TARGET, f"{URN}:{service}", entity],
    )


def _host_gap(service: str, host: str, kind: UnresolvedEntityKind) -> EvidenceIssue:
    entity = f"unresolved:{host}"
    return EvidenceIssue(
        code=ReasonCode.DISCOVERY_GAP,
        detail=f"Unresolved dependency: {entity}",
        subjects=[IssueSubject(kind=IssueSubjectKind.ENTITY, id=entity)],
        entity_kind=kind,
        root_key=unresolved_key(host, entity),
        via=[TARGET, f"{URN}:{service}", entity],
    )


def _library_gap(service: str) -> EvidenceIssue:
    paths = ["src/EventBusRabbitMQ/Extensions.cs", "src/eShop.ServiceDefaults/Config.cs"]
    return EvidenceIssue(
        code=ReasonCode.STATIC_COVERAGE_INCOMPLETE,
        detail=f"{service} has 2 files not understood",
        subjects=[IssueSubject(kind=IssueSubjectKind.SERVICE, id=service)]
        + [IssueSubject(kind=IssueSubjectKind.FILE, id=f"eShop:{p}") for p in paths],
        root_key=files_key("eShop", paths),
    )


def pr_b_assessment() -> ReleaseAssessment:
    """eShop PR B (a change to the shared EventBus library), as gap lines."""
    issues = [_env_gap(s, "OTEL_EXPORTER_OTLP_ENDPOINT") for s in SERVICES]
    issues += [_env_gap("catalog-api", "ConnectionStrings__embedding"), _env_gap("webapp", "ConnectionStrings__chat")]
    issues += [
        _host_gap("identity-api", "10.0.2.2:5243", UnresolvedEntityKind.IP_ADDRESS),
        _host_gap("identity-api", "http://eshopxamarin", UnresolvedEntityKind.UNREGISTERED_SERVICE),
    ]
    issues += [_library_gap(s) for s in SERVICES[:7]]
    issues.append(EvidenceIssue(code=ReasonCode.COMPATIBILITY_UNVERIFIED, detail="No contract evidence."))
    return ReleaseAssessment(
        approved=False,
        actionable=False,
        reason_codes=[ReasonCode.DISCOVERY_GAP, ReasonCode.STATIC_COVERAGE_INCOMPLETE, ReasonCode.COMPATIBILITY_UNVERIFIED],
        deployment_sequence=[f"{URN}:{s}" for s in SERVICES[:7]],
        services_to_deploy=[f"{URN}:{s}" for s in SERVICES[:7]],
        justification="Evidence incomplete.",
        evidence=AssessmentEvidence(
            pr_id="PR-B",
            head_sha="b" * 40,
            exempted_dependencies=[
                Exemption(subject="unresolved:registry.npmjs.org", rule="egress host registry.npmjs.org", reason="npm")
            ],
        ),
        issues=issues,
    )


class BriefingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assessment = pr_b_assessment()
        self.briefing = build_briefing(self.assessment)

    def test_twenty_two_lines_become_a_handful_of_causes(self) -> None:
        blocking = [g for g in self.briefing.groups if g.issues]
        self.assertEqual(self.briefing.raw_issue_count, 21)
        self.assertEqual(len(blocking), 7)
        self.assertEqual(sum(len(g.issues) for g in blocking), 21)

    def test_ranked_decisions_first(self) -> None:
        ranks = [g.rank for g in self.briefing.groups]
        self.assertEqual(ranks, sorted(ranks, key=[BriefingRank.DECISION, BriefingRank.FIX_EVIDENCE, BriefingRank.INFO].index))
        decisions = {g.root_key for g in self.briefing.groups if g.rank is BriefingRank.DECISION}
        self.assertEqual(
            decisions,
            {"env:otel_exporter_otlp_endpoint", "env:connectionstrings__embedding",
             "env:connectionstrings__chat", "host:10.0.2.2", "COMPATIBILITY_UNVERIFIED"},
        )
        self.assertEqual(self.briefing.decisions_needed, 5)
        self.assertEqual(self.briefing.evidence_fixes, 2)

    def test_one_variable_unset_in_nine_services_is_one_question(self) -> None:
        (otel,) = [g for g in self.briefing.groups if g.root_key == "env:otel_exporter_otlp_endpoint"]
        self.assertEqual(len(otel.issues), 9)
        self.assertEqual(len(otel.services), 9)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", otel.title)
        self.assertIn("exemptions:\n  dependencies:", otel.policy_draft)
        self.assertEqual(otel.policy_draft.count("entity_id:"), 9)

    def test_a_shared_library_is_one_cause(self) -> None:
        (library,) = [g for g in self.briefing.groups if g.root_key.startswith("unscanned:")]
        self.assertEqual(len(library.services), 7)
        self.assertIn("unscanned_files", library.policy_draft)
        self.assertIn("src/EventBusRabbitMQ/Extensions.cs", library.policy_draft)

    def test_headline_is_fail_closed(self) -> None:
        self.assertTrue(self.briefing.headline.startswith("No-go: evidence incomplete. 5 decision(s)"))
        with self.assertRaises(ValidationError):
            ReleaseBriefing.model_validate({**self.briefing.model_dump(), "headline": "Go anyway"})

    def test_every_issue_is_kept(self) -> None:
        data = self.briefing.model_dump()
        data["groups"][0]["issues"] = []
        with self.assertRaises(ValidationError):
            ReleaseBriefing.model_validate(data)

    def test_accepted_items_are_context(self) -> None:
        (info,) = [g for g in self.briefing.groups if g.rank is BriefingRank.INFO]
        self.assertEqual(info.issues, [])
        self.assertIn("egress host registry.npmjs.org: npm", info.what_to_check)

    def test_approved_and_risk_found_headlines(self) -> None:
        approved = ReleaseAssessment(
            approved=True, actionable=True, deployment_sequence=[TARGET], justification="ok",
            evidence=AssessmentEvidence(pr_id="PR-1"),
            advisory_assessment=RiskAssessment(risk_score=0.1, approved=True, justification="ok"),
        )
        self.assertTrue(build_briefing(approved).headline.startswith("Go (advisory)"))
        rejected = approved.model_copy(update={"approved": False, "reason_codes": [ReasonCode.MODEL_REJECTED]})
        self.assertEqual(build_briefing(rejected).headline, "No-go: risk found (MODEL_REJECTED).")

    def test_markdown(self) -> None:
        text = render_markdown(self.briefing)
        self.assertIn("### Decisions needed", text)
        self.assertIn("### Evidence to fix", text)
        self.assertIn("21 raw issue(s) grouped into 7 cause(s)", text)
        self.assertIn("Rollback: webhooks-api → webapp", text)


class CoverageOfCodesTests(unittest.TestCase):
    def test_every_reason_code_is_explained_and_titled(self) -> None:
        from src.core.reason_explanations import REASON_EXPLANATIONS
        from src.orchestration.briefing import _TITLES

        for code in ReasonCode:
            with self.subTest(code=code):
                self.assertIn(code, REASON_EXPLANATIONS)
                self.assertIn(code, _TITLES)


if __name__ == "__main__":
    unittest.main()
