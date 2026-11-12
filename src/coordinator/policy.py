"""Deterministic release policy: turn evidence into the final verdict.

The advisory model only scores and justifies a release. Whether the release
is approved, and whether the result is actionable at all, is decided here
from the evidence issues, the deployment plan and the model's opinion, so a
model answer can never approve a release the evidence does not support.
"""

from pydantic import BaseModel, Field

from src.coordinator.sequencing import DeploymentPlan
from src.core.models import EvidenceIssue, ReasonCode, RiskAssessment


class ReleaseVerdict(BaseModel):
    """The policy decision for one release.

    ``reason_codes`` are unique, in the order they were found. ``risk_score``
    is None when the model was not consulted.
    """

    approved: bool
    actionable: bool
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    risk_score: float | None = Field(default=None, ge=0.0, le=1.0)
    justification: str = Field(min_length=1)


def _unique(codes: list[ReasonCode]) -> list[ReasonCode]:
    return list(dict.fromkeys(codes))


def plan_reason_codes(plan: DeploymentPlan) -> list[ReasonCode]:
    """Return why ``plan`` does not establish a safe rolling order."""
    codes: list[ReasonCode] = []
    if not plan.is_acyclic:
        codes.append(ReasonCode.DEPENDENCY_CYCLE)
    if not plan.compatibility_verified:
        codes.append(ReasonCode.COMPATIBILITY_UNVERIFIED)
    return codes


def needs_model(issues: list[EvidenceIssue], plan: DeploymentPlan) -> bool:
    """Whether the advisory model should be consulted.

    It is not consulted when the evidence is incomplete or the subgraph has
    a dependency cycle: either way no verdict could be acted on, and a
    model opinion would only add a misleading score.
    """
    return not issues and plan.is_acyclic


def _evidence_rejection(issues: list[EvidenceIssue]) -> str:
    listed = " ".join(
        f"({index}) [{issue.code.value}] {issue.detail}"
        for index, issue in enumerate(issues, 1)
    )
    return (
        "Not assessed: the evidence is insufficient, so the advisory model "
        f"was not consulted and no risk score is given. Issues: {listed} "
        "Resolve these and re-run discovery before re-assessing; the "
        "deployment sequence shown is computed from incomplete evidence."
    )


def _cycle_rejection(plan: DeploymentPlan) -> str:
    return (
        "Not assessed: the release subgraph has a dependency cycle, so no "
        "deployment order satisfies every dependency for a "
        f"{plan.change_type.value} change. Services in or blocked by the "
        f"cycle: {', '.join(plan.cyclic_services)}. The deployment sequence "
        "shown breaks the cycle arbitrarily and needs human review."
    )


def _assessed_justification(
    codes: list[ReasonCode], advisory: RiskAssessment
) -> str:
    opinion = "approve" if advisory.approved else "reject"
    if not codes:
        return f"Approved. Advisory assessment: {advisory.justification}"
    reasons = ", ".join(code.value for code in codes)
    return (
        f"Not approved ({reasons}). The advisory model would {opinion} this "
        f"release: {advisory.justification}"
    )


def decide_release(
    issues: list[EvidenceIssue],
    plan: DeploymentPlan,
    advisory: RiskAssessment | None,
) -> ReleaseVerdict:
    """Return the final policy verdict.

    The release is approved only when there are no evidence issues, the plan
    establishes a safe order, and the advisory model approved. It is
    actionable only when no reason code limits actionability; a model
    rejection over complete evidence is still actionable.

    Raises:
        ValueError: if ``advisory`` is missing although ``needs_model`` says
            the model had to be consulted.
    """
    if advisory is None and needs_model(issues, plan):
        raise ValueError("an advisory assessment is required for this release")
    codes =[issue.code for issue in issues] + plan_reason_codes(plan)
    if advisory is not None and not advisory.approved:
        codes.append(ReasonCode.MODEL_REJECTED)
    codes = _unique(codes)
    if issues:
        justification = _evidence_rejection(issues)
    elif advisory is None:
        justification = _cycle_rejection(plan)
    else:
        justification = _assessed_justification(codes, advisory)
    return ReleaseVerdict(
        approved=advisory is not None and advisory.approved and not codes,
        actionable=not any(code.limits_actionability for code in codes),
        reason_codes=codes,
        risk_score=advisory.risk_score if advisory else None,
        justification=justification,
    )
