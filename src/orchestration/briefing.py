"""The release briefing: an assessment arranged for the person deciding.

A pure function of a ``ReleaseAssessment``. It never changes the verdict
(``approved``, ``actionable`` and the reason codes are copied, never
recomputed) and never hides an issue: every issue lands in exactly one
group (checked by ``ReleaseBriefing``).

Issues are grouped by cause (``EvidenceIssue.root_key``: one variable unset
in nine services is one question) and ranked:

- DECISION: a person must decide or accept something (contract
  compatibility, a new or external dependency, an unset variable, a
  telemetry blind spot, a dependency cycle, the risk review).
- FIX_EVIDENCE: a gap a declaration or configuration fix removes (a stale
  baseline, a name that doesn't link, files the scanner couldn't read).
- INFO: context that doesn't block (what the policy accepted, objects
  outside the supported Kubernetes shape).

The headline stays fail-closed: it says "Go" only when the assessment is
approved. Exemptions are offered as drafts for a reviewed policy change,
never applied.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Final, Self

from pydantic import BaseModel, Field, model_validator

from src.core.models import (
    EvidenceIssue,
    IssueSubjectKind,
    ReasonCode,
    ReleaseAssessment,
    UnresolvedEntityKind,
)
from src.core.reason_explanations import REASON_EXPLANATIONS

_DECISION_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.COMPATIBILITY_UNVERIFIED,
        ReasonCode.DEPENDENCY_CYCLE,
        ReasonCode.MODEL_REJECTED,
        ReasonCode.BASELINE_DRIFT,
        ReasonCode.RUNTIME_UNOBSERVABLE,
    }
)
# Unresolved entities a person must judge (is this dependency intended?),
# as opposed to ones a declaration resolves.
_DECISION_ENTITIES: Final[frozenset[UnresolvedEntityKind]] = frozenset(
    {
        UnresolvedEntityKind.IP_ADDRESS,
        UnresolvedEntityKind.EXTERNAL_DNS,
        UnresolvedEntityKind.PRIVATE_DNS,
        UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET,
    }
)
_LISTED: Final[int] = 5
# A short title per code, for groups without a more specific cause.
_TITLES: Final[dict[ReasonCode, str]] = {
    ReasonCode.BASELINE_UNAVAILABLE: "No usable discovery baseline",
    ReasonCode.BASELINE_STALE: "The discovery baseline is too old",
    ReasonCode.BASELINE_SCOPE_MISMATCH: "The baseline describes another environment or cluster",
    ReasonCode.BASELINE_DRIFT: "Live traffic shows a dependency the baseline lacks",
    ReasonCode.BASELINE_REVISION_MISMATCH: "The baseline doesn't match the pull request's base",
    ReasonCode.COMPATIBILITY_UNVERIFIED: "Contract compatibility is not verified",
    ReasonCode.DEMO_EVIDENCE: "The evidence includes demo data",
    ReasonCode.DEPENDENCY_CYCLE: "No safe deployment order (dependency cycle)",
    ReasonCode.DISCOVERY_GAP: "Dependency evidence is incomplete",
    ReasonCode.IDENTITY_UNLINKED: "Source code not linked to a running service",
    ReasonCode.MODEL_REJECTED: "The risk review rejected the release",
    ReasonCode.POLICY_INVALID: "The project's release policy is invalid",
    ReasonCode.POLICY_MISSING: "The project has no release policy",
    ReasonCode.REQUIRED_SOURCE_MISSING: "Required evidence is missing or too weak",
    ReasonCode.REVISION_UNVERIFIED: "The pull request's commits couldn't be verified",
    ReasonCode.RUNTIME_UNOBSERVABLE: "Telemetry can't see some services' protocols",
    ReasonCode.STATIC_COVERAGE_INCOMPLETE: "Source not fully read or understood",
    ReasonCode.UNMAPPED_FILES: "Changed files belong to no service",
    ReasonCode.REVIEW_UNAVAILABLE: "The dependency review couldn't be read",
}


class BriefingRank(str, Enum):
    DECISION = "decision"
    FIX_EVIDENCE = "fix_evidence"
    INFO = "info"


_RANK_ORDER: Final[dict[BriefingRank, int]] = {
    BriefingRank.DECISION: 0,
    BriefingRank.FIX_EVIDENCE: 1,
    BriefingRank.INFO: 2,
}


class BriefingGroup(BaseModel):
    """Issues with one cause, and what a person does about them."""

    root_key: str = Field(min_length=1)
    rank: BriefingRank
    title: str = Field(min_length=1)
    why_it_matters: str = ""
    what_to_check: list[str] = Field(default_factory=list)
    how_to_close: list[str] = Field(default_factory=list)
    policy_draft: str = Field(
        default="", description="YAML for a reviewed policy change; never applied."
    )
    services: list[str] = Field(default_factory=list)
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    issues: list[EvidenceIssue] = Field(default_factory=list)


class DeploymentStep(BaseModel):
    order: int = Field(ge=1)
    service: str = Field(min_length=1)
    changed: bool
    note: str = ""


class ReleaseBriefing(BaseModel):
    """One assessment as a person reads it; derived, never decisive."""

    pr_id: str = Field(min_length=1)
    head_sha: str = ""
    headline: str = Field(min_length=1)
    approved: bool
    actionable: bool
    decisions_needed: int = Field(ge=0)
    evidence_fixes: int = Field(ge=0)
    groups: list[BriefingGroup]
    deployment: list[DeploymentStep]
    rollback_order: list[str]
    raw_issue_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _faithful(self) -> Self:
        grouped = sum(len(group.issues) for group in self.groups)
        if grouped != self.raw_issue_count:
            raise ValueError("every issue must land in exactly one group")
        if self.headline.startswith("Go") and not self.approved:
            raise ValueError("the headline may say Go only for an approved assessment")
        return self


def short_name(service_id: str) -> str:
    """``urn:...:shop:orders`` -> ``orders``; anything else unchanged."""
    return service_id.rsplit(":", 1)[-1] if service_id.startswith("urn:") else service_id


def rank_of(issue: EvidenceIssue) -> BriefingRank:
    if issue.code in _DECISION_CODES:
        return BriefingRank.DECISION
    if issue.code is ReasonCode.DISCOVERY_GAP and issue.entity_kind in _DECISION_ENTITIES:
        return BriefingRank.DECISION
    return BriefingRank.FIX_EVIDENCE


def _affected(issue: EvidenceIssue) -> list[str]:
    """Services an issue concerns: its service subjects, else the caller
    nearest the unresolved node on its path."""
    named = [short_name(s.id) for s in issue.subjects if s.kind is IssueSubjectKind.SERVICE]
    if named:
        return named
    if len(issue.via) >= 2:
        return [short_name(issue.via[-2])]
    return []


def _files(issues: Iterable[EvidenceIssue]) -> list[str]:
    found = {s.id for i in issues for s in i.subjects if s.kind is IssueSubjectKind.FILE}
    return sorted(found)


def _entities(issues: Iterable[EvidenceIssue]) -> list[str]:
    found = {s.id for i in issues for s in i.subjects if s.kind is IssueSubjectKind.ENTITY}
    return sorted(found)


def _listed(items: list[str]) -> str:
    shown = ", ".join(items[:_LISTED])
    return shown + (f" (+{len(items) - _LISTED} more)" if len(items) > _LISTED else "")


def _dependency_draft(entities: list[str]) -> str:
    lines = ["exemptions:", "  dependencies:"]
    for entity in entities:
        lines += [f"    - entity_id: {entity}", "      reason: <why this is intended - reviewer>"]
    return "\n".join(lines)


def _unscanned_draft(files: list[str]) -> str:
    lines = ["exemptions:", "  unscanned_files:"]
    for file in files:
        path = file.split(":", 1)[-1]
        lines += [f"    - pattern: {path}", "      reason: <what a reviewer checked - reviewer>"]
    return "\n".join(lines)


def _describe_group(group: BriefingGroup) -> None:
    """Fill a group's title, checks and closing steps from its cause."""
    key, issues = group.root_key, group.issues
    code = issues[0].code
    meaning, fix = REASON_EXPLANATIONS.get(code, ("See docs/release-model.md.", ""))
    group.why_it_matters = meaning
    services = group.services
    who = _listed(services) if services else "the release"
    if key.startswith("env:"):
        variable = key.removeprefix("env:").upper()
        group.title = f"{variable} has no readable value for {len(services) or 1} service(s)"
        group.what_to_check = [
            f"Does {who} need {variable} in this environment? If it calls nothing through it "
            "here, it can be accepted; otherwise set it in the manifests."
        ]
        group.how_to_close = ["Set the variable, or accept it with a reviewed policy exemption."]
        group.policy_draft = _dependency_draft(_entities(issues))
    elif key.startswith("host:"):
        host = key.removeprefix("host:")
        group.title = f"{who} reaches {host}, which matches no registered service"
        group.what_to_check = [
            f"Is {host} an intended dependency (external service, infrastructure, dev-only "
            "address)? Who owns it?"
        ]
        group.how_to_close = [
            "Allow it as egress in the project policy, register or declare the service, or "
            "fix the configuration that points there."
        ]
        group.policy_draft = _dependency_draft(_entities(issues))
    elif key.startswith("unscanned:"):
        files = _files(issues)
        group.title = f"{len(files)} file(s) not read or understood, in {len(services) or 1} service(s)"
        group.what_to_check = [f"Read {_listed([f.split(':', 1)[-1] for f in files])}: do they call another service?"]
        group.how_to_close = [fix] if fix else []
        group.policy_draft = _unscanned_draft(files)
    elif key.startswith("identity:"):
        group.title = f"{key.removeprefix('identity:')}: source code not linked to a running service"
        group.what_to_check = [issue.detail for issue in issues]
        group.how_to_close = [fix] if fix else []
    else:
        group.title = _TITLES.get(code, code.value)
        group.what_to_check = [issue.detail for issue in issues[:_LISTED]]
        group.how_to_close = [fix] if fix else []


def _groups(issues: list[EvidenceIssue]) -> list[BriefingGroup]:
    by_key: dict[str, BriefingGroup] = {}
    for issue in issues:
        group = by_key.get(issue.root_key)
        if group is None:
            group = BriefingGroup(root_key=issue.root_key, rank=rank_of(issue), title=issue.code.value)
            by_key[issue.root_key] = group
        group.issues.append(issue)
        if issue.code not in group.reason_codes:
            group.reason_codes.append(issue.code)
        if rank_of(issue) is BriefingRank.DECISION:
            group.rank = BriefingRank.DECISION
        for service in _affected(issue):
            if service not in group.services:
                group.services.append(service)
    for group in by_key.values():
        _describe_group(group)
    return sorted(
        by_key.values(),
        key=lambda g: (_RANK_ORDER[g.rank], -max(len(g.services), 1), g.root_key),
    )


def _context_groups(assessment: ReleaseAssessment) -> list[BriefingGroup]:
    """Non-blocking context: what the policy accepted, out-of-shape objects."""
    evidence = assessment.evidence
    accepted = [
        f"{e.rule}: {e.reason}"
        for e in [*evidence.exempted_dependencies, *evidence.exempted_files, *evidence.exempted_unscanned_files]
    ]
    coverage = evidence.live_coverage
    if coverage is not None:
        accepted += [f"{short_name(s)} allowed to be quiet" for s in coverage.silent_services]
        accepted += [
            f"{short_name(s)}: telemetry can't see {', '.join(p)} (accepted)"
            for s, p in coverage.accepted_unobservable.items()
        ]
    groups: list[BriefingGroup] = []
    if accepted:
        groups.append(
            BriefingGroup(
                root_key="info:accepted",
                rank=BriefingRank.INFO,
                title=f"{len(accepted)} item(s) accepted by the project policy",
                why_it_matters="Reviewed exceptions; each still carries its reason.",
                what_to_check=accepted,
            )
        )
    groups.extend(_review_groups(assessment))
    if evidence.shape_findings:
        groups.append(
            BriefingGroup(
                root_key="info:shape",
                rank=BriefingRank.INFO,
                title=f"{len(evidence.shape_findings)} Kubernetes object(s) outside the supported shape",
                why_it_matters="They may hide calls; see each finding.",
                what_to_check=[
                    f"{f.kind.value}: {f.namespace}/{f.object}: {f.detail}"
                    for f in evidence.shape_findings[:_LISTED]
                ],
            )
        )
    return groups


def _review_groups(assessment: ReleaseAssessment) -> list[BriefingGroup]:
    """What the project's dependency review contributed, and what the
    deployment order rests on."""
    review = assessment.evidence.review
    if review is None:
        return []
    rests_on = (
        f"The order rests on {len(review.confirmed)} confirmed, {len(review.declared)} "
        f"declared and {review.unreviewed} unreviewed dependencies in the radius, "
        f"{len(review.accepted_issues)} accepted gap(s), and the declared change type."
    )
    answered = [
        f"{note.reviewer} on {note.answered_at:%Y-%m-%d}: {note.detail}"
        + (f" ({note.reason})" if note.reason else "")
        for note in review.applied
    ]
    groups = [
        BriefingGroup(
            root_key="info:review",
            rank=BriefingRank.INFO,
            title="Dependency review",
            why_it_matters=rests_on,
            what_to_check=[*answered[:_LISTED], *review.unapplied[:_LISTED]],
        )
    ]
    if review.needs_rereview:
        groups.append(
            BriefingGroup(
                root_key="info:review-stale",
                rank=BriefingRank.INFO,
                title=f"{len(review.needs_rereview)} review answer(s) need re-review",
                why_it_matters="Their evidence changed, so they no longer count; what they covered is back above.",
                what_to_check=[
                    f"{note.subject} ({note.verdict} by {note.reviewer}): {note.detail}"
                    for note in review.needs_rereview[:_LISTED]
                ],
            )
        )
    return groups


def headline(assessment: ReleaseAssessment, decisions: int, fixes: int) -> str:
    """The fail-closed go/no-go line."""
    if assessment.approved:
        return "Go (advisory): every check passed and the risk review approved."
    if assessment.actionable:
        codes = ", ".join(code.value for code in assessment.reason_codes) or "risk review"
        return f"No-go: risk found ({codes})."
    return (
        f"No-go: evidence incomplete. {decisions} decision(s) and {fixes} evidence fix(es) "
        "before this release can be judged."
    )


def _deployment(assessment: ReleaseAssessment) -> list[DeploymentStep]:
    changed = set(assessment.services_to_deploy)
    stale = set(assessment.stale_consumers)
    return [
        DeploymentStep(
            order=position,
            service=short_name(service),
            changed=service in changed,
            note="unchanged consumer must already run a compatible version" if service in stale else "",
        )
        for position, service in enumerate(assessment.deployment_sequence, start=1)
    ]


def build_briefing(assessment: ReleaseAssessment) -> ReleaseBriefing:
    """Arrange ``assessment`` for the person deciding (see module doc)."""
    groups = _groups(list(assessment.issues))
    decisions = sum(1 for g in groups if g.rank is BriefingRank.DECISION)
    fixes = sum(1 for g in groups if g.rank is BriefingRank.FIX_EVIDENCE)
    return ReleaseBriefing(
        pr_id=assessment.evidence.pr_id,
        head_sha=assessment.evidence.head_sha,
        headline=headline(assessment, decisions, fixes),
        approved=assessment.approved,
        actionable=assessment.actionable,
        decisions_needed=decisions,
        evidence_fixes=fixes,
        groups=[*groups, *_context_groups(assessment)],
        deployment=_deployment(assessment),
        rollback_order=[short_name(s) for s in reversed(assessment.services_to_deploy)],
        raw_issue_count=len(assessment.issues),
    )


def render_markdown(briefing: ReleaseBriefing) -> str:
    """The briefing as Markdown (a report, or later a PR comment)."""
    lines = [f"## Release briefing: {briefing.pr_id}", "", f"**{briefing.headline}**", ""]
    lines.append(
        f"{briefing.raw_issue_count} raw issue(s) grouped into "
        f"{sum(1 for g in briefing.groups if g.issues)} cause(s)."
    )
    titles = {
        BriefingRank.DECISION: "Decisions needed",
        BriefingRank.FIX_EVIDENCE: "Evidence to fix",
        BriefingRank.INFO: "Context",
    }
    number = 0
    for rank in BriefingRank:
        groups = [g for g in briefing.groups if g.rank is rank]
        if not groups:
            continue
        lines += ["", f"### {titles[rank]}"]
        for group in groups:
            number += 1
            codes = ", ".join(c.value for c in group.reason_codes)
            lines.append(f"{number}. **{group.title}**" + (f" ({codes})" if codes else ""))
            if group.why_it_matters:
                lines.append(f"   {group.why_it_matters}")
            lines += [f"   - Check: {item}" for item in group.what_to_check]
            lines += [f"   - Close: {item}" for item in group.how_to_close]
    if briefing.deployment:
        order = " → ".join(
            f"{s.service}{'*' if s.changed else ''}" for s in briefing.deployment
        )
        lines += ["", "### Deployment order", order, "(* changed by this pull request)"]
        if briefing.rollback_order:
            lines.append(f"Rollback: {' → '.join(briefing.rollback_order)}")
    return "\n".join(lines) + "\n"
