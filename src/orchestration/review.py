"""The project dependency review: what a coordinator reviews, and the
confirmed dependency map their answers produce.

``answers_in_force`` decides which stored answers still count: the newest
per subject, not stale against the current commits or the files a pull
request changes, and (for a disagreed dependency) not contradicted by
evidence the answer never saw. ``confirmed_map`` applies those answers to
a baseline graph: disagreed edges removed, declared edges added with
``HUMAN_DECLARED`` evidence, linked identities merged. ``split_accepted``
moves the issues an answered question covers out of the evidence to fix.
``build_review`` lays out services, dependencies and questions for the
review page.

Pure: answers, commits, changes and issues are passed in. Computing the
issues (coordinator) and reading commits (source control) happen in the
layers that own them.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import Enum
from typing import Final

from pydantic import BaseModel, Field

from src.core.edge_resolution import K8S_ENV_PROVENANCE
from src.core.models import (
    IDENTITY_LINK_METADATA_KEY,
    IDENTITY_SUGGESTIONS_METADATA_KEY,
    IDENTITY_UNLINKED_METADATA_KEY,
    LABEL_SEPARATOR,
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    DependencyEdge,
    DiscoveryMethod,
    EvidenceIssue,
    IssueSubjectKind,
    KnowledgeGraph,
    ReasonCode,
    ServiceNode,
    merge_evidence_files,
)
from src.core.review import (
    HUMAN_DECLARED_PROVENANCE,
    REPOSITORY_ROOT,
    DeclaredDependency,
    RepositoryChanges,
    ReviewAnchor,
    ReviewAnswer,
    ReviewSubject,
    ReviewSubjectKind,
    ReviewVerdict,
    StaleReason,
    answer_freshness,
    current_answers,
    edge_evidence,
    edge_key,
)

# Issue codes a coordinator can answer: a gap with a specific cause.
QUESTION_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.DISCOVERY_GAP,
        ReasonCode.STATIC_COVERAGE_INCOMPLETE,
        ReasonCode.IDENTITY_UNLINKED,
    }
)
# A declared edge is a person's word, not measured: it carries no
# automated confidence.
_DECLARED_CONFIDENCE: Final[float] = 0.0


class StaleAnswer(BaseModel):
    """An answer that no longer counts, and why."""

    answer: ReviewAnswer
    reasons: list[StaleReason]


class ReviewInForce(BaseModel):
    """Which answers count against the current evidence.

    ``answers`` are in force; ``stale`` were given on evidence that has
    changed (their subject is open again, "previously answered by").
    """

    answers: dict[ReviewSubject, ReviewAnswer] = Field(default_factory=dict)
    stale: dict[ReviewSubject, StaleAnswer] = Field(default_factory=dict)


def is_question(issue: EvidenceIssue) -> bool:
    """Whether a coordinator can answer ``issue``: an answerable code with
    a specific cause (not the code's catch-all default key)."""
    return issue.code in QUESTION_CODES and issue.root_key != issue.code.value


def question_subject(issue: EvidenceIssue) -> ReviewSubject:
    kind = (
        ReviewSubjectKind.IDENTITY
        if issue.code is ReasonCode.IDENTITY_UNLINKED
        else ReviewSubjectKind.QUESTION
    )
    return ReviewSubject(kind=kind, key=issue.root_key)


def _edges_by_key(graph: KnowledgeGraph) -> dict[str, DependencyEdge]:
    return {edge_key(e.source_service_id, e.target_service_id): e for e in graph.edges}


def _new_evidence(answer: ReviewAnswer, edges: Mapping[str, DependencyEdge]) -> StaleReason | None:
    if answer.verdict is not ReviewVerdict.DISAGREE:
        return None
    edge = edges.get(answer.subject.key)
    if edge is None:
        return None
    unseen = sorted(set(edge_evidence(edge)) - set(answer.evidence_seen))
    if not unseen:
        return None
    return StaleReason(
        repository="",
        detail=f"new evidence since it was removed: {', '.join(unseen)}",
    )


def answers_in_force(
    graph: KnowledgeGraph,
    answers: Iterable[ReviewAnswer],
    current_commits: Mapping[str, str],
    changes: Sequence[RepositoryChanges],
    pending_changes: Mapping[str, Sequence[str]] | None = None,
    checked_at: datetime | None = None,
) -> ReviewInForce:
    """Decide which of ``answers`` count against ``graph``'s evidence.

    Args:
        graph: the graph the answers are applied to (baseline, or the
            pull request head's).
        answers: every stored answer for the project.
        current_commits / changes: as ``core.review.answer_freshness``.
        pending_changes: repository -> files a pull request changes; an
            answer anchored to one of them doesn't count for that release.
        checked_at: recorded on the freshness checks.
    """
    edges = _edges_by_key(graph)
    pending = pending_changes or {}
    in_force = ReviewInForce()
    for subject, answer in sorted(
        current_answers(answers).items(), key=lambda item: (item[0].kind.value, item[0].key)
    ):
        reasons = list(answer_freshness(answer, current_commits, changes, checked_at).stale)
        for anchor in answer.anchors:
            touched = anchor.touched_by(pending.get(anchor.repository, ()))
            if touched:
                reasons.append(
                    StaleReason(
                        repository=anchor.repository,
                        detail=f"this pull request changes {len(touched)} anchored file(s)",
                        changed_paths=tuple(touched),
                    )
                )
        reopened = _new_evidence(answer, edges)
        if reopened is not None:
            reasons.append(reopened)
        if reasons:
            in_force.stale[subject] = StaleAnswer(answer=answer, reasons=reasons)
        else:
            in_force.answers[subject] = answer
    return in_force


# --- Anchors ------------------------------------------------------------------


def _root_path(node: ServiceNode) -> str:
    root = node.metadata.get(SERVICE_ROOT_METADATA_KEY, "").strip("/")
    return f"{root}/" if root else REPOSITORY_ROOT


def _commit(graph: KnowledgeGraph, repository: str) -> str | None:
    revision = graph.scan.revision_for(repository) if graph.scan else None
    return revision.commit_sha if revision else None


def _anchors(graph: KnowledgeGraph, paths: Mapping[str, set[str]]) -> tuple[ReviewAnchor, ...] | None:
    anchors: list[ReviewAnchor] = []
    for repository, files in sorted(paths.items()):
        commit = _commit(graph, repository)
        if commit is None or not files:
            return None
        anchors.append(ReviewAnchor(repository=repository, commit_sha=commit, paths=tuple(files)))
    return tuple(anchors) or None


def edge_anchors(graph: KnowledgeGraph, edge: DependencyEdge) -> tuple[ReviewAnchor, ...] | None:
    """What an answer about ``edge`` rests on: the files citing it in the
    caller's repository (and the static defaults it resolved through), or
    the caller's service root when no file is cited. None when the caller
    has no scanned repository: the answer could never go stale, so it
    can't be given."""
    nodes = {node.service_id: node for node in graph.services}
    source = nodes.get(edge.source_service_id)
    repository = source.metadata.get(REPOSITORY_METADATA_KEY) if source else None
    if source is None or not repository:
        return None
    files = set(edge.evidence_files or ())
    files.update(
        use.file_path.lstrip("/")
        for use in graph.static_defaults_used or ()
        if use.service_id == edge.source_service_id and use.target == edge.target_service_id
    )
    return _anchors(graph, {repository: files or {_root_path(source)}})


def issue_anchors(graph: KnowledgeGraph, issues: Iterable[EvidenceIssue]) -> tuple[ReviewAnchor, ...] | None:
    """What an answer to a question rests on: its unread files, the roots
    of the services it concerns, and the roots of every service calling an
    unresolved entity it concerns. None if any part isn't in a scanned
    repository (identity questions need none: ``ReviewVerdict.LINK``)."""
    nodes = {node.service_id: node for node in graph.services}
    paths: dict[str, set[str]] = {}

    def add_service(service_id: str) -> bool:
        node = nodes.get(service_id)
        repository = node.metadata.get(REPOSITORY_METADATA_KEY) if node else None
        if node is None or not repository:
            return False
        paths.setdefault(repository, set()).add(_root_path(node))
        return True

    for issue in issues:
        for subject in issue.subjects:
            if subject.kind is IssueSubjectKind.FILE:
                repository, _, path = subject.id.partition(":")
                if not path:
                    return None
                paths.setdefault(repository, set()).add(path.lstrip("/"))
            elif subject.kind is IssueSubjectKind.SERVICE:
                if not add_service(subject.id):
                    return None
            else:
                callers = [e.source_service_id for e in graph.edges if e.target_service_id == subject.id]
                if not callers or not all(add_service(caller) for caller in callers):
                    return None
    return _anchors(graph, paths)


# --- Confirmed map ------------------------------------------------------------


class AppliedAnswer(BaseModel):
    """An answer in force and what it changed in the map."""

    answer: ReviewAnswer
    edges: list[str] = Field(default_factory=list)


class ConfirmedMap(BaseModel):
    """A graph with the review applied.

    ``removed`` / ``added`` are the dependency changes, ``accepted`` the
    answered questions by root key, ``linked`` the identity merges
    (``renamed`` maps a merged source-code service ID to the runtime ID it
    became). ``unapplied`` are answers in force that couldn't be applied
    (an endpoint no longer in the graph), each with why.
    """

    graph: KnowledgeGraph
    removed: list[AppliedAnswer] = Field(default_factory=list)
    added: list[AppliedAnswer] = Field(default_factory=list)
    accepted: dict[str, ReviewAnswer] = Field(default_factory=dict)
    linked: list[AppliedAnswer] = Field(default_factory=list)
    renamed: dict[str, str] = Field(default_factory=dict)
    unapplied: list[str] = Field(default_factory=list)

    def rename(self, service_id: str) -> str:
        return self.renamed.get(service_id, service_id)


def _declared_edge(dependency: DeclaredDependency, answered_at: datetime) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=dependency.source_id,
        target_service_id=dependency.target_id,
        discovery_methods=[DiscoveryMethod.HUMAN_DECLARED],
        confidence_score=_DECLARED_CONFIDENCE,
        last_observed_at=answered_at,
        provenance=HUMAN_DECLARED_PROVENANCE,
        protocol=dependency.protocol,
        evidence_files=[],
    )


def _union(first: DependencyEdge, second: DependencyEdge) -> DependencyEdge:
    methods = list(dict.fromkeys([*first.discovery_methods, *second.discovery_methods]))
    labels = list(dict.fromkeys([*first.provenance.split(LABEL_SEPARATOR), *second.provenance.split(LABEL_SEPARATOR)]))
    return first.model_copy(
        update={
            "discovery_methods": methods,
            "provenance": LABEL_SEPARATOR.join(labels),
            "confidence_score": max(first.confidence_score, second.confidence_score),
            "protocol": first.protocol or second.protocol,
            "evidence_files": merge_evidence_files(first.evidence_files, second.evidence_files),
            "direction_uncertain": first.direction_uncertain and second.direction_uncertain,
        }
    )


def _merge_identity(
    services: list[ServiceNode], edges: list[DependencyEdge], source_id: str, runtime_id: str, describe: str
) -> tuple[list[ServiceNode], list[DependencyEdge]]:
    """Fold the source-code service into the runtime one: its edges move to
    the runtime ID and its repository location is kept on the runtime node."""
    source = next(s for s in services if s.service_id == source_id)
    kept: list[ServiceNode] = []
    for node in services:
        if node.service_id == source_id:
            continue
        if node.service_id == runtime_id:
            metadata = {
                **{k: v for k, v in source.metadata.items() if k not in (IDENTITY_UNLINKED_METADATA_KEY, IDENTITY_SUGGESTIONS_METADATA_KEY)},
                **node.metadata,
                IDENTITY_LINK_METADATA_KEY: describe,
            }
            node = node.model_copy(update={"metadata": metadata})
        kept.append(node)
    merged: dict[tuple[str, str], DependencyEdge] = {}
    for edge in edges:
        pair = tuple(runtime_id if end == source_id else end for end in (edge.source_service_id, edge.target_service_id))
        if pair[0] == pair[1]:
            continue
        moved = edge.model_copy(update={"source_service_id": pair[0], "target_service_id": pair[1]})
        held = merged.get((pair[0], pair[1]))
        merged[(pair[0], pair[1])] = moved if held is None else _union(held, moved)
    return kept, list(merged.values())


def confirmed_map(graph: KnowledgeGraph, in_force: ReviewInForce) -> ConfirmedMap:
    """Apply the answers in force to ``graph``.

    Order: identities are merged first (so the other answers name the
    merged services), then disagreed edges removed, then declared edges
    added (merged into an existing edge between the same services).
    """
    result = ConfirmedMap(graph=graph)
    services = list(graph.services)
    edges = list(graph.edges)
    answers = list(in_force.answers.values())

    for answer in (a for a in answers if a.verdict is ReviewVerdict.LINK):
        source_name = answer.subject.key.partition(":")[2]
        sources = [
            s for s in services
            if s.name.lower() == source_name and s.metadata.get(IDENTITY_UNLINKED_METADATA_KEY) == "true"
        ]
        runtime = [
            s for s in services
            if s.identity.service_name == answer.linked_runtime_name and s.service_id not in {x.service_id for x in sources}
        ]
        if len(sources) != 1 or len(runtime) != 1:
            result.unapplied.append(
                f"link {source_name} = {answer.linked_runtime_name}: expected one unlinked "
                f"source service and one runtime service, found {len(sources)} and {len(runtime)}"
            )
            continue
        describe = f"HUMAN_DECLARED: {sources[0].name} = {answer.linked_runtime_name} ({answer.reviewer})"
        services, edges = _merge_identity(services, edges, sources[0].service_id, runtime[0].service_id, describe)
        result.renamed[sources[0].service_id] = runtime[0].service_id
        result.linked.append(AppliedAnswer(answer=answer, edges=[]))
        result.accepted[answer.subject.key] = answer

    def renamed_key(key: str) -> str:
        source, _, target = key.removeprefix("edge:").partition("->")
        return edge_key(result.rename(source), result.rename(target))

    for answer in (a for a in answers if a.verdict is ReviewVerdict.DISAGREE):
        key = renamed_key(answer.subject.key)
        remaining = [e for e in edges if edge_key(e.source_service_id, e.target_service_id) != key]
        if len(remaining) == len(edges):
            continue  # Discovery no longer finds it: nothing to remove.
        edges = remaining
        result.removed.append(AppliedAnswer(answer=answer, edges=[key]))

    nodes = {s.service_id for s in services} | {e.entity_id for e in graph.unresolved_entities}
    for answer in (a for a in answers if a.verdict in (ReviewVerdict.ADD, ReviewVerdict.CALLS)):
        applied: list[str] = []
        for dependency in answer.declared:
            declared = dependency.model_copy(
                update={"source_id": result.rename(dependency.source_id), "target_id": result.rename(dependency.target_id)}
            )
            missing = [end for end in (declared.source_id, declared.target_id) if end not in nodes]
            if missing:
                result.unapplied.append(f"{declared.key}: {', '.join(missing)} not in the baseline")
                continue
            edge = _declared_edge(declared, answer.answered_at)
            position = next(
                (i for i, e in enumerate(edges) if (e.source_service_id, e.target_service_id) == (declared.source_id, declared.target_id)),
                None,
            )
            if position is None:
                edges.append(edge)
            else:
                edges[position] = _union(edges[position], edge)
            applied.append(declared.key)
        if applied:
            result.added.append(AppliedAnswer(answer=answer, edges=applied))
        # A question is closed only by the dependencies behind it, all of them.
        if answer.verdict is ReviewVerdict.CALLS and len(applied) == len(answer.declared):
            result.accepted[answer.subject.key] = answer

    for answer in (a for a in answers if a.verdict is ReviewVerdict.NOTHING_HIDDEN):
        result.accepted[answer.subject.key] = answer

    result.graph = graph.model_copy(update={"services": services, "edges": edges})
    KnowledgeGraph.model_validate(result.graph.model_dump())  # Joins still hold.
    return result


class AcceptedIssue(BaseModel):
    """An issue a coordinator's answer covers: context, not evidence to fix."""

    issue: EvidenceIssue
    answer: ReviewAnswer


def split_accepted(
    issues: Iterable[EvidenceIssue], accepted: Mapping[str, ReviewAnswer]
) -> tuple[list[EvidenceIssue], list[AcceptedIssue]]:
    """Separate the issues an answered question covers from the rest."""
    remaining: list[EvidenceIssue] = []
    covered: list[AcceptedIssue] = []
    for issue in issues:
        answer = accepted.get(issue.root_key) if is_question(issue) else None
        if answer is None:
            remaining.append(issue)
        else:
            covered.append(AcceptedIssue(issue=issue, answer=answer))
    return remaining, covered


# --- Review page --------------------------------------------------------------


class ReviewStatus(str, Enum):
    """Where one dependency or question stands in the review."""

    OPEN = "open"
    CONFIRMED = "confirmed"
    REMOVED = "removed"
    ADDED = "added"
    ANSWERED = "answered"
    LINKED = "linked"
    NEEDS_REREVIEW = "needs_rereview"


_STATUS_BY_VERDICT: Final[dict[ReviewVerdict, ReviewStatus]] = {
    ReviewVerdict.AGREE: ReviewStatus.CONFIRMED,
    ReviewVerdict.DISAGREE: ReviewStatus.REMOVED,
    ReviewVerdict.ADD: ReviewStatus.ADDED,
    ReviewVerdict.NOTHING_HIDDEN: ReviewStatus.ANSWERED,
    ReviewVerdict.CALLS: ReviewStatus.ANSWERED,
    ReviewVerdict.LINK: ReviewStatus.LINKED,
}


class ReviewedItem(BaseModel):
    """Review state shared by dependencies and questions.

    ``answer`` is the answer in force; ``previous`` a stale answer and
    ``stale`` why it no longer counts. ``anchors`` are what a new answer
    would rest on (None: it can't be answered, see ``edge_anchors``).
    """

    subject: ReviewSubject
    status: ReviewStatus
    answer: ReviewAnswer | None = None
    previous: ReviewAnswer | None = None
    stale: list[StaleReason] = Field(default_factory=list)
    anchors: tuple[ReviewAnchor, ...] | None = None


class DependencyRow(ReviewedItem):
    """One dependency: discovery's evidence for it, or a coordinator's word."""

    source_id: str
    target_id: str
    discovery_methods: list[DiscoveryMethod]
    provenance: str
    protocol: str | None = None
    evidence_files: list[str] | None = None
    evidence: tuple[str, ...] = ()
    weak: str | None = Field(default=None, description="Why the evidence is weak, if it is.")


class QuestionRow(ReviewedItem):
    """One cause the platform could not see past, with every issue it covers."""

    code: ReasonCode
    details: list[str]
    services: list[str]


class ServiceRow(BaseModel):
    service_id: str
    name: str
    source: str
    repository: str | None = None
    root: str | None = None


class ProjectReview(BaseModel):
    """Everything the review page shows for one baseline snapshot."""

    snapshot_id: str
    services: list[ServiceRow]
    dependencies: list[DependencyRow]
    questions: list[QuestionRow]
    unapplied: list[str] = Field(default_factory=list)


def _weakness(graph: KnowledgeGraph, edge: DependencyEdge) -> str | None:
    methods = set(edge.discovery_methods)
    if methods == {DiscoveryMethod.HUMAN_DECLARED}:
        return "declared by a person only"
    if edge.provenance == K8S_ENV_PROVENANCE:
        return "only a Kubernetes environment variable names it (it may be unused)"
    if methods == {DiscoveryMethod.STATIC_CODE} and any(
        use.service_id == edge.source_service_id and use.target == edge.target_service_id
        for use in graph.static_defaults_used or ()
    ):
        return "only a default value in source names it"
    return None


class _State(BaseModel):
    status: ReviewStatus
    answer: ReviewAnswer | None = None
    previous: ReviewAnswer | None = None
    stale: list[StaleReason] = Field(default_factory=list)


def _state(subject: ReviewSubject, in_force: ReviewInForce) -> _State:
    answer = in_force.answers.get(subject)
    if answer is not None:
        return _State(status=_STATUS_BY_VERDICT[answer.verdict], answer=answer)
    stale = in_force.stale.get(subject)
    if stale is not None:
        return _State(status=ReviewStatus.NEEDS_REREVIEW, previous=stale.answer, stale=stale.reasons)
    return _State(status=ReviewStatus.OPEN)


def build_review(
    graph: KnowledgeGraph,
    snapshot_id: str,
    issues: Iterable[EvidenceIssue],
    in_force: ReviewInForce,
) -> ProjectReview:
    """Lay out ``graph`` (the baseline, without the review applied) and the
    project-wide ``issues`` for review. Dependencies are between registered
    services (an edge to an unresolved entity is listed as its question).
    Declared dependencies in force are listed as ADDED rows; removed ones
    keep their row as REMOVED."""
    services = [
        ServiceRow(
            service_id=node.service_id,
            name=node.name,
            source=node.metadata.get("source", "unknown"),
            repository=node.metadata.get(REPOSITORY_METADATA_KEY),
            root=node.metadata.get(SERVICE_ROOT_METADATA_KEY),
        )
        for node in graph.services
    ]
    dependencies: list[DependencyRow] = []
    listed: set[str] = set()
    registered = {node.service_id for node in graph.services}
    for edge in graph.edges:
        if edge.target_service_id not in registered or edge.source_service_id not in registered:
            continue  # An unresolved end is a question, not a dependency to agree with.
        subject = ReviewSubject(kind=ReviewSubjectKind.DEPENDENCY, key=edge_key(edge.source_service_id, edge.target_service_id))
        listed.add(subject.key)
        dependencies.append(
            DependencyRow(
                subject=subject,
                source_id=edge.source_service_id,
                target_id=edge.target_service_id,
                discovery_methods=edge.discovery_methods,
                provenance=edge.provenance,
                protocol=edge.protocol,
                evidence_files=edge.evidence_files,
                evidence=edge_evidence(edge),
                weak=_weakness(graph, edge),
                anchors=edge_anchors(graph, edge),
                **dict(_state(subject, in_force)),
            )
        )
    declared_answers = [
        *(a for a in in_force.answers.values() if a.verdict in (ReviewVerdict.ADD, ReviewVerdict.CALLS)),
        *(s.answer for s in in_force.stale.values() if s.answer.verdict is ReviewVerdict.ADD),
    ]
    for answer in declared_answers:
        for dependency in answer.declared:
            if dependency.key in listed:
                continue
            listed.add(dependency.key)
            subject = ReviewSubject(kind=ReviewSubjectKind.DEPENDENCY, key=dependency.key)
            state = _state(subject, in_force)
            if state.status is ReviewStatus.OPEN:
                state = _State(status=ReviewStatus.ADDED, answer=answer)
            dependencies.append(
                DependencyRow(
                    subject=subject,
                    source_id=dependency.source_id,
                    target_id=dependency.target_id,
                    discovery_methods=[DiscoveryMethod.HUMAN_DECLARED],
                    provenance=HUMAN_DECLARED_PROVENANCE,
                    protocol=dependency.protocol,
                    weak="declared by a person only",
                    anchors=answer.anchors or None,
                    **dict(state),
                )
            )

    grouped: dict[str, list[EvidenceIssue]] = {}
    for issue in issues:
        if is_question(issue):
            grouped.setdefault(issue.root_key, []).append(issue)
    questions: list[QuestionRow] = []
    for key, group in sorted(grouped.items()):
        subject = question_subject(group[0])
        questions.append(
            QuestionRow(
                subject=subject,
                code=group[0].code,
                details=list(dict.fromkeys(issue.detail for issue in group)),
                services=sorted(
                    {s.id for issue in group for s in issue.subjects if s.kind is IssueSubjectKind.SERVICE}
                    | {hop for issue in group for hop in issue.via[:-1]}
                ),
                anchors=None if subject.kind is ReviewSubjectKind.IDENTITY else issue_anchors(graph, group),
                **dict(_state(subject, in_force)),
            )
        )
    return ProjectReview(
        snapshot_id=snapshot_id,
        services=services,
        dependencies=dependencies,
        questions=questions,
        unapplied=confirmed_map(graph, in_force).unapplied,
    )
