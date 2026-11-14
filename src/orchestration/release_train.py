"""Release trains: several pull requests assessed as one release.

A train is built from existing per-PR assessments; nothing is re-assessed
and no coordinator logic is repeated. What a train adds:

- one release subgraph: the union of the members' targets, upstream and
  downstream services (a service changed by any member is a target);
- one deployment order over that union, from ``sequence_deployment`` and
  the dependency pairs each member's order honoured. Each pair is oriented
  by the change that touches its provider: when the callee is changed by a
  BREAKING member its consumer deploys first, otherwise the provider does.
  Conflicting orientations show up as a cycle;
- one verdict, worst wins: the train is approved only when every member is
  approved and the train adds no issue of its own, and actionable only when
  every member is actionable and no train issue limits it.

Train issues are what only the combination shows: members pinned to
different baseline snapshots, members recorded before ordering facts were
kept, and a cycle in the combined order. They are not ``ReasonCode``s: each
member's reason codes stay its own and are listed per member and combined.
"""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from src.coordinator.sequencing import (
    DependencyPair,
    ReleaseOrderFacts,
    sequence_deployment,
)
from src.core.models import ContractChange, ReasonCode, ReleaseAssessment


class TrainIssueKind(str, Enum):
    """Why a train needs more than its members do.

    BASELINE_MISMATCH: members were assessed against different baseline
        snapshots, so their radii aren't comparable.
    ORDER_FACTS_MISSING: a member was recorded before ordering facts were
        kept; the combined order leaves its edges out.
    TRAIN_CYCLE: no order of the combined subgraph satisfies every edge.
    """

    BASELINE_MISMATCH = "BASELINE_MISMATCH"
    ORDER_FACTS_MISSING = "ORDER_FACTS_MISSING"
    TRAIN_CYCLE = "TRAIN_CYCLE"


class TrainIssue(BaseModel):
    """One issue only the combination of the members shows."""

    kind: TrainIssueKind
    detail: str = Field(min_length=1)
    subjects: list[str] = Field(default_factory=list)


class TrainMemberInput(BaseModel):
    """One completed PR assessment to include in a train."""

    job_id: str = Field(min_length=1)
    pr_id: str = Field(min_length=1)
    headline: str = ""
    assessment: ReleaseAssessment
    order: ReleaseOrderFacts | None = None


class TrainMember(BaseModel):
    """One member's own verdict, as the train shows it."""

    job_id: str
    pr_id: str
    repository: str
    headline: str
    approved: bool
    actionable: bool
    reason_codes: list[ReasonCode]
    services_to_deploy: list[str]
    change_type: ContractChange | None
    baseline_snapshot_id: str | None


TrainStatus = Literal["approved", "decision_needed", "blocked"]


class ReleaseTrain(BaseModel):
    """Several PR assessments combined into one release.

    ``status`` is the combined verdict (worst member wins; train issues can
    only make it worse). ``deployment_sequence`` covers the whole combined
    subgraph; ``services_to_deploy`` is the part the members change, in that
    order. ``stale_consumers`` are unchanged consumers a BREAKING member
    needs updated first that no member of the train changes;
    ``consumers_changed_in_train`` are such consumers another member does
    change (check that member makes them compatible). ``risk_score`` is the
    highest member score, None when any member wasn't scored.
    """

    train_id: str
    title: str
    tenant: str
    project_id: str
    created_at: datetime
    status: TrainStatus
    headline: str
    approved: bool
    actionable: bool
    safe_order_established: bool
    risk_score: float | None
    reason_codes: list[ReasonCode]
    members: list[TrainMember] = Field(min_length=2)
    issues: list[TrainIssue] = Field(default_factory=list)
    services_to_deploy: list[str]
    upstream_services: list[str]
    downstream_services: list[str]
    deployment_sequence: list[str]
    cyclic_services: list[str] = Field(default_factory=list)
    stale_consumers: list[str] = Field(default_factory=list)
    consumers_changed_in_train: list[str] = Field(default_factory=list)


def _union(lists: list[list[str]]) -> list[str]:
    """Items of ``lists`` in first-seen order, without duplicates."""
    seen: dict[str, None] = {}
    for items in lists:
        for item in items:
            seen.setdefault(item, None)
    return list(seen)


def _oriented_pairs(orders: list[ReleaseOrderFacts]) -> list[DependencyPair]:
    """Every member's pairs, oriented for an ADDITIVE sort.

    An ADDITIVE sort deploys a pair's callee before its caller. A pair whose
    callee a BREAKING member changes is reversed, so its caller (the
    consumer) deploys first, as that member's own order required.
    """
    broken = {
        target
        for order in orders
        if order.change_type is ContractChange.BREAKING
        for target in order.targets
    }
    pairs: dict[tuple[str, str], DependencyPair] = {}
    for order in orders:
        for pair in order.edges:
            caller, callee = pair.source_service_id, pair.target_service_id
            key = (callee, caller) if callee in broken else (caller, callee)
            pairs[key] = DependencyPair(source_service_id=key[0], target_service_id=key[1])
    return [pairs[key] for key in sorted(pairs)]


def _code_rank(code: ReasonCode) -> int:
    return list(ReasonCode).index(code)


def _headline(status: TrainStatus, members: int, issues: int) -> str:
    if status == "approved":
        return f"Go: all {members} pull requests are approved as one release."
    if status == "decision_needed":
        return f"Your call: {members} pull requests, evidence complete; a person decides."
    extra = f" and {issues} train issue{'s' if issues != 1 else ''}" if issues else ""
    return f"No-go: the evidence for this {members}-PR release is incomplete{extra}."


def build_release_train(
    train_id: str,
    title: str,
    tenant: str,
    project_id: str,
    created_at: datetime,
    members: list[TrainMemberInput],
) -> ReleaseTrain:
    """Combine completed PR assessments of one project into a release train.

    Raises:
        ValueError: fewer than two members, or a job listed twice.
    """
    if len(members) < 2:
        raise ValueError("a release train needs at least two assessments")
    if len({m.job_id for m in members}) != len(members):
        raise ValueError("each assessment can be in a train only once")

    issues: list[TrainIssue] = []
    snapshots = sorted(
        {m.assessment.evidence.baseline_snapshot_id or "none" for m in members}
    )
    if len(snapshots) > 1:
        issues.append(
            TrainIssue(
                kind=TrainIssueKind.BASELINE_MISMATCH,
                detail=(
                    "The pull requests were assessed against different baseline "
                    f"snapshots ({', '.join(snapshots)}); re-assess them against "
                    "one baseline before combining them."
                ),
                subjects=snapshots,
            )
        )
    missing = [m.pr_id for m in members if m.order is None]
    if missing:
        issues.append(
            TrainIssue(
                kind=TrainIssueKind.ORDER_FACTS_MISSING,
                detail=(
                    f"{', '.join(missing)} {'was' if len(missing) == 1 else 'were'} "
                    "assessed before ordering facts were kept, so the combined order "
                    "leaves out their dependency edges; re-assess them."
                ),
                subjects=missing,
            )
        )

    orders = [m.order for m in members if m.order is not None]
    targets = _union(
        [m.order.targets if m.order else m.assessment.services_to_deploy for m in members]
    )
    if not targets:
        targets = _union([m.assessment.deployment_sequence for m in members])
    target_set = set(targets)
    upstream = [s for s in _union([o.upstream for o in orders]) if s not in target_set]
    downstream = [s for s in _union([o.downstream for o in orders]) if s not in target_set]
    # Members without facts still contribute their whole subgraph.
    known = {*targets, *upstream, *downstream}
    upstream += [
        s
        for s in _union([m.assessment.deployment_sequence for m in members if m.order is None])
        if s not in known
    ]
    plan = sequence_deployment(
        targets=targets,
        upstream=upstream,
        downstream=downstream,
        edges=_oriented_pairs(orders),
        change_type=ContractChange.ADDITIVE,
    )
    if plan.cyclic_services:
        issues.append(
            TrainIssue(
                kind=TrainIssueKind.TRAIN_CYCLE,
                detail=(
                    "No deployment order of the combined release satisfies every "
                    f"dependency; the order shown for {', '.join(plan.cyclic_services)} "
                    "breaks the cycle arbitrarily."
                ),
                subjects=plan.cyclic_services,
            )
        )

    stale_all = _union([m.assessment.stale_consumers for m in members])
    stale = [s for s in stale_all if s not in target_set]
    changed = [s for s in stale_all if s in target_set]

    scores = [m.assessment.risk_score for m in members]
    risk = None if any(score is None for score in scores) else max(
        score for score in scores if score is not None
    )
    codes = sorted({c for m in members for c in m.assessment.reason_codes}, key=_code_rank)
    actionable = all(m.assessment.actionable for m in members) and not issues
    approved = actionable and all(m.assessment.approved for m in members)
    status: TrainStatus = (
        "approved" if approved else "decision_needed" if actionable else "blocked"
    )
    return ReleaseTrain(
        train_id=train_id,
        title=title or f"Release of {', '.join(m.pr_id for m in members)}",
        tenant=tenant,
        project_id=project_id,
        created_at=created_at,
        status=status,
        headline=_headline(status, len(members), len(issues)),
        approved=approved,
        actionable=actionable,
        safe_order_established=(
            not issues and all(m.assessment.safe_order_established for m in members)
        ),
        risk_score=risk,
        reason_codes=codes,
        members=[
            TrainMember(
                job_id=m.job_id,
                pr_id=m.pr_id,
                repository=m.assessment.evidence.repository,
                headline=m.headline,
                approved=m.assessment.approved,
                actionable=m.assessment.actionable,
                reason_codes=list(m.assessment.reason_codes),
                services_to_deploy=list(m.assessment.services_to_deploy),
                change_type=m.order.change_type if m.order else None,
                baseline_snapshot_id=m.assessment.evidence.baseline_snapshot_id,
            )
            for m in members
        ],
        issues=issues,
        services_to_deploy=plan.services_to_deploy,
        upstream_services=upstream,
        downstream_services=downstream,
        deployment_sequence=plan.sequence,
        cyclic_services=plan.cyclic_services,
        stale_consumers=stale,
        consumers_changed_in_train=changed,
    )
