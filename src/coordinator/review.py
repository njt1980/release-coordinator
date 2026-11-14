"""The project dependency review inside a release assessment.

``load_review_node`` (in ``nodes``) reads the project's answers, asks
source control what changed since each answer's commit, and decides which
answers are in force (``orchestration.review.answers_in_force``). The
radius node then walks the confirmed map and turns answered questions into
context. This module holds the pieces that join those steps to the
assessment's own types.
"""

from collections.abc import Iterable, Mapping, Sequence

from src.core.models import (
    AssessmentReview,
    DependencyChange,
    DependencyEdge,
    DiscoveryMethod,
    EvidenceIssue,
    ReasonCode,
    ReviewNote,
)
from src.core.review import (
    RepositoryChanges,
    ReviewAnswer,
    ReviewSubjectKind,
    ReviewVerdict,
    edge_key,
)
from src.orchestration.review import AcceptedIssue, ConfirmedMap, ReviewInForce
from src.tools.devops_extractor import CommitComparison


def review_unavailable(detail: str) -> EvidenceIssue:
    return EvidenceIssue(
        code=ReasonCode.REVIEW_UNAVAILABLE,
        detail=f"The project's dependency review couldn't be read: {detail}",
    )


def needed_comparisons(
    answers: Iterable[ReviewAnswer], current_commits: Mapping[str, str]
) -> list[tuple[str, str, str]]:
    """``(repository, anchor commit, current commit)`` for every anchor not
    at the current commit of a repository the baseline scanned."""
    needed = {
        (anchor.repository, anchor.commit_sha, current)
        for answer in answers
        for anchor in answer.anchors
        if (current := current_commits.get(anchor.repository)) is not None
        and current != anchor.commit_sha
    }
    return sorted(needed)


def to_changes(comparison: CommitComparison) -> RepositoryChanges:
    return RepositoryChanges(
        repository=comparison.repository,
        from_commit=comparison.base,
        to_commit=comparison.target,
        from_is_ancestor=comparison.base_is_ancestor,
        changed_paths=tuple(comparison.changed_paths),
    )


def pending_changes(
    repository: str, changed_files: Sequence[str], current_commits: Mapping[str, str]
) -> dict[str, list[str]]:
    """The pull request's changed files, keyed by the repository name the
    baseline scanned (source control names are case-insensitive)."""
    if not repository:
        return {}
    scanned = next((name for name in current_commits if name.lower() == repository.lower()), repository)
    return {scanned: list(changed_files)}


def _note(answer: ReviewAnswer, detail: str) -> ReviewNote:
    return ReviewNote(
        answer_id=answer.answer_id,
        subject=answer.subject.key,
        verdict=answer.verdict.value,
        reviewer=answer.reviewer,
        answered_at=answer.answered_at,
        reason=answer.reason,
        detail=detail,
    )


def _change(key: str) -> DependencyChange:
    source, _, target = key.removeprefix("edge:").partition("->")
    return DependencyChange(source_id=source, target_id=target)


def assessment_review(
    confirmed: ConfirmedMap,
    in_force: ReviewInForce,
    accepted: Sequence[AcceptedIssue],
    radius_edges: Sequence[DependencyEdge],
) -> AssessmentReview:
    """Record how the review shaped this assessment."""
    applied: list[ReviewNote] = []
    for item in confirmed.removed:
        applied.append(_note(item.answer, f"removed {', '.join(item.edges)}"))
    for item in confirmed.added:
        applied.append(_note(item.answer, f"declared {', '.join(item.edges)}"))
    for item in confirmed.linked:
        applied.append(_note(item.answer, f"linked to {item.answer.linked_runtime_name}"))
    closed = {issue.issue.root_key for issue in accepted}
    for key, answer in sorted(confirmed.accepted.items()):
        if key in closed and answer.verdict is not ReviewVerdict.LINK:
            applied.append(_note(answer, "closed the question"))
    needs = [
        _note(stale.answer, "; ".join(reason.detail for reason in stale.reasons))
        for stale in in_force.stale.values()
    ]
    agreed = {
        subject.key
        for subject, answer in in_force.answers.items()
        if subject.kind is ReviewSubjectKind.DEPENDENCY and answer.verdict is ReviewVerdict.AGREE
    }
    confirmed_edges: list[DependencyChange] = []
    declared: list[DependencyChange] = []
    unreviewed = 0
    for edge in radius_edges:
        key = edge_key(edge.source_service_id, edge.target_service_id)
        if DiscoveryMethod.HUMAN_DECLARED in edge.discovery_methods:
            declared.append(_change(key))
        elif key in agreed:
            confirmed_edges.append(_change(key))
        else:
            unreviewed += 1
    return AssessmentReview(
        applied=applied,
        accepted_issues=[issue.issue for issue in accepted],
        needs_rereview=needs,
        confirmed=confirmed_edges,
        declared=declared,
        removed=[_change(edge) for item in confirmed.removed for edge in item.edges],
        unreviewed=unreviewed,
        unapplied=list(confirmed.unapplied),
    )

