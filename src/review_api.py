"""Project dependency review API.

``GET  /api/v1/projects/{tenant}/{project_id}/review`` lays out the newest
complete baseline for review: services, dependencies with their evidence,
and the questions the platform could not answer, each with where the
review stands.
``POST /api/v1/projects/{tenant}/{project_id}/review/answers`` records one
answer. The client says only what a person decided (subject, verdict,
reason, what is declared or linked); the server sets the time, the
baseline it was given on, the anchors it rests on and the evidence seen,
so an answer can't claim evidence it wasn't given on.
``GET  /api/v1/projects/{tenant}/{project_id}/review/export`` lists the
corrections as feedback for scanner work.

The reviewer is the signed-in user when the platform's authentication
supplies one (``X-MS-CLIENT-PRINCIPAL-NAME``, set by Azure App Service /
Container Apps authentication, which strips it from client requests);
otherwise the name the client sends (local pilot only).
"""

import sqlite3
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Path, status
from pydantic import BaseModel, Field, ValidationError

from src.coordinator import nodes as coordinator_nodes
from src.core.models import DependencyEdge, DiscoveryMethod
from src.core.review import (
    DeclaredDependency,
    ReviewAnchor,
    ReviewAnswer,
    ReviewSubject,
    ReviewSubjectKind,
    ReviewVerdict,
    edge_evidence,
)
from src.orchestration.review import (
    ProjectReview,
    ReviewInForce,
    build_review,
    edge_anchors,
)
from src.tools.baseline_store import PATH_SEGMENT_PATTERN, BaselineScope, StoredBaseline
from src.tools.policy_store import PolicyInvalidError, PolicyMissingError, ProjectPolicy
from src.tools.review_store import DuplicateAnswerError, ReviewStoreCorruptError

router = APIRouter(prefix="/api/v1/projects/{tenant}/{project_id}/review", tags=["review"])

PRINCIPAL_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"

Tenant = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]
ProjectId = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]


class AnswerRequest(BaseModel):
    """What a coordinator decided about one subject of the review."""

    subject: ReviewSubject
    verdict: ReviewVerdict
    reviewer: str = Field(default="", max_length=200)
    reason: str | None = Field(default=None, max_length=2000)
    declared: list[DeclaredDependency] = Field(default_factory=list)
    linked_runtime_name: str | None = Field(default=None, max_length=200)


class Correction(BaseModel):
    """One correction, as scanner feedback."""

    kind: str = Field(description="false_positive | missed | false_alarm | identity")
    subject: str
    reviewer: str
    answered_at: datetime
    reason: str | None = None
    declared: list[DeclaredDependency] = Field(default_factory=list)


class ReviewExport(BaseModel):
    snapshot_id: str
    corrections: list[Correction]


def _context(tenant: str, project_id: str) -> tuple[BaselineScope, StoredBaseline, ProjectPolicy | None]:
    scope = BaselineScope(tenant=tenant, project_id=project_id)
    baseline = coordinator_nodes.BASELINE_STORE.latest(scope)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No complete discovery baseline for {tenant}/{project_id}; run onboarding first.",
        )
    try:
        policy: ProjectPolicy | None = coordinator_nodes.POLICY_STORE.load(scope).policy
    except (PolicyMissingError, PolicyInvalidError):
        policy = None
    return scope, baseline, policy


def _in_force(scope: BaselineScope, baseline: StoredBaseline, policy: ProjectPolicy | None) -> ReviewInForce:
    try:
        return coordinator_nodes.review_in_force(scope, baseline, policy, baseline.graph) or ReviewInForce()
    except (ReviewStoreCorruptError, sqlite3.Error, OSError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The dependency review can't be read: {error}",
        ) from error


def _review(tenant: str, project_id: str) -> tuple[BaselineScope, StoredBaseline, ProjectReview]:
    scope, baseline, policy = _context(tenant, project_id)
    review = build_review(
        baseline.graph,
        baseline.snapshot_id,
        coordinator_nodes.project_questions(policy, baseline),
        _in_force(scope, baseline, policy),
    )
    return scope, baseline, review


@router.get("", response_model=ProjectReview)
def get_review(tenant: Tenant, project_id: ProjectId) -> ProjectReview:
    """The newest baseline laid out for review."""
    return _review(tenant, project_id)[2]


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _anchors_for(
    request: AnswerRequest, baseline: StoredBaseline, review: ProjectReview
) -> tuple[tuple[ReviewAnchor, ...], tuple[str, ...]]:
    """Anchors and evidence seen for ``request``, from the current review.

    Raises:
        HTTPException: 422 when the subject isn't in the review or the
            answer can't be anchored.
    """
    subject = request.subject
    if request.verdict is ReviewVerdict.LINK:
        if not any(q.subject == subject for q in review.questions):
            raise _unprocessable(f"{subject.key} is not an open identity question")
        return (), ()
    if request.verdict is ReviewVerdict.ADD:
        if len(request.declared) != 1:
            raise _unprocessable("add declares exactly one dependency")
        declared = request.declared[0]
        known = {s.service_id for s in review.services}
        if declared.source_id not in known or declared.target_id not in known:
            raise _unprocessable("both ends of a declared dependency must be services in the baseline")
        probe = DependencyEdge(
            source_service_id=declared.source_id,
            target_service_id=declared.target_id,
            discovery_methods=[DiscoveryMethod.HUMAN_DECLARED],
            confidence_score=0.0,
            last_observed_at=datetime.now(timezone.utc),
        )
        anchors = edge_anchors(baseline.graph, probe)
        if anchors is None:
            raise _unprocessable(f"{declared.source_id} has no scanned repository to anchor to")
        return anchors, ()
    if subject.kind is ReviewSubjectKind.DEPENDENCY:
        row = next((d for d in review.dependencies if d.subject == subject), None)
        if row is None:
            raise _unprocessable(f"{subject.key} is not a dependency in the current baseline")
        if row.anchors is None:
            raise _unprocessable(f"{subject.key} has no scanned repository to anchor to")
        edge = next(
            (e for e in baseline.graph.edges if (e.source_service_id, e.target_service_id) == (row.source_id, row.target_id)),
            None,
        )
        return row.anchors, edge_evidence(edge) if edge else row.evidence
    question = next((q for q in review.questions if q.subject == subject), None)
    if question is None:
        raise _unprocessable(f"{subject.key} is not a question in the current baseline")
    if question.anchors is None:
        raise _unprocessable(f"{subject.key} rests on nothing in a scanned repository")
    known = {s.service_id for s in review.services}
    unknown = sorted({end for d in request.declared for end in (d.source_id, d.target_id)} - known)
    if unknown:
        raise _unprocessable(f"not services in the baseline: {', '.join(unknown)}")
    return question.anchors, ()


@router.post("/answers", response_model=ProjectReview, status_code=status.HTTP_201_CREATED)
def post_answer(
    tenant: Tenant,
    project_id: ProjectId,
    request: AnswerRequest,
    principal: Annotated[str | None, Header(alias=PRINCIPAL_HEADER)] = None,
) -> ProjectReview:
    """Record one answer and return the updated review."""
    scope, baseline, review = _review(tenant, project_id)
    reviewer = (principal or request.reviewer).strip()
    if not reviewer:
        raise _unprocessable("a reviewer name is required")
    anchors, seen = _anchors_for(request, baseline, review)
    try:
        answer = ReviewAnswer(
            answer_id=uuid4().hex,
            subject=request.subject,
            verdict=request.verdict,
            reviewer=reviewer,
            answered_at=datetime.now(timezone.utc),
            snapshot_id=baseline.snapshot_id,
            reason=request.reason,
            declared=tuple(request.declared),
            linked_runtime_name=request.linked_runtime_name,
            anchors=anchors,
            evidence_seen=seen if request.verdict is ReviewVerdict.DISAGREE else (),
        )
    except ValidationError as error:
        raise _unprocessable(str(error)) from error
    try:
        coordinator_nodes.REVIEW_STORE.append(scope, answer)
    except DuplicateAnswerError as error:  # pragma: no cover - uuid4 collision
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _review(tenant, project_id)[2]


_KINDS = {
    ReviewVerdict.DISAGREE: "false_positive",
    ReviewVerdict.ADD: "missed",
    ReviewVerdict.CALLS: "missed",
    ReviewVerdict.NOTHING_HIDDEN: "false_alarm",
    ReviewVerdict.LINK: "identity",
}


@router.get("/export", response_model=ReviewExport)
def export_corrections(tenant: Tenant, project_id: ProjectId) -> ReviewExport:
    """Corrections in force: disagreed edges are scanner false positives,
    declared ones misses, "nothing hidden" answers false alarms."""
    scope, baseline, policy = _context(tenant, project_id)
    in_force = _in_force(scope, baseline, policy)
    corrections = [
        Correction(
            kind=_KINDS[answer.verdict],
            subject=answer.subject.key,
            reviewer=answer.reviewer,
            answered_at=answer.answered_at,
            reason=answer.reason,
            declared=list(answer.declared),
        )
        for answer in in_force.answers.values()
        if answer.verdict in _KINDS
    ]
    return ReviewExport(snapshot_id=baseline.snapshot_id, corrections=corrections)
