"""Node functions for the release orchestration graph."""

import logging
import sqlite3
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Final, Literal

from pydantic import BaseModel

from src.coordinator.coverage import radius_shape_findings, static_coverage_issues
from src.coordinator.policy import decide_release, needs_model
from src.coordinator.project_policy import (
    live_coverage,
    regulated_impact,
    unobservable_protocols,
    with_unscoped_sources,
    live_runtime_state,
    baseline_freshness_issue,
    baseline_scope_issue,
    baseline_runtime_state,
    dependency_exemption,
    partition_exempt_files,
    required_source_issues,
    runtime_evidence_issue,
)
from src.coordinator.revision import (
    RevisionLag,
    changed_services,
    head_topology,
    missing_head_services,
    pull_request_issues,
    target_roots,
)
from src.coordinator.runtime_drift import (
    baseline_identity_context,
    drift_issue,
    resolve_live_edges,
    unbaselined_edges,
    workspace_mismatch_issue,
)
from src.coordinator.review import (
    assessment_review,
    needed_comparisons,
    pending_changes,
    review_unavailable,
    to_changes,
)
from src.coordinator.sequencing import DeploymentPlan, sequence_deployment
from src.coordinator.state import ReleaseGraphState
from src.core import config
from src.core.issue_keys import identity_key, unresolved_key
from src.core.models import (
    IDENTITY_SUGGESTIONS_METADATA_KEY,
    IDENTITY_UNLINKED_METADATA_KEY,
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    AssessmentEvidence,
    DependencyEdge,
    DiscoveryIncompleteError,
    DiscoveryMethod,
    EvidenceIssue,
    Exemption,
    IssueSubject,
    IssueSubjectKind,
    KnowledgeGraph,
    ObservedEdge,
    PullRequestPayload,
    ReasonCode,
    ReleaseAssessment,
    RuntimeEvidenceState,
    ScannedRevision,
    ServiceIdentity,
    TelemetryStatus,
    UnresolvedEntityKind,
    UnresolvedReach,
)
from src.core.service_ids import IdentityIndex, parse_env_var_reference
from src.tools.baseline_store import BaselineScope, StoredBaseline, default_store
from src.tools.policy_store import (
    LoadedPolicy,
    PolicyInvalidError,
    PolicyMissingError,
    ProjectPolicy,
    default_policy_store,
)
from src.core.review import RepositoryChanges
from src.orchestration.review import (
    ReviewInForce,
    answers_in_force,
    confirmed_map,
    split_accepted,
)
from src.tools.devops_extractor import CommitComparison, DevOpsExtractor
from src.tools.review_store import ReviewStoreCorruptError, default_review_store
from src.tools.risk_reasoner import RiskAssessmentRequest, RiskReasoner
from src.coordinator.observability import observability_gaps
from src.tools.telemetry_extractor import TelemetryExtractor

logger = logging.getLogger(__name__)

BASELINE_STORE = default_store()
POLICY_STORE = default_policy_store()
REVIEW_STORE = default_review_store()

UNRESOLVED_DEPENDENCY_ISSUE: Final[str] = "Unresolved dependency in critical path"


def _service_name(service_id: str) -> str:
    """Return the bare service name of a FQURI (or the ID itself otherwise)."""
    try:
        return ServiceIdentity.parse(service_id).service_name
    except ValueError:
        return service_id


def utc_now() -> datetime:
    """Return the current UTC time (patched in tests)."""
    return datetime.now(timezone.utc)


def _gap(detail: str) -> EvidenceIssue:
    return EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail=detail)


def _scope(state: ReleaseGraphState) -> BaselineScope:
    return BaselineScope(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        project_id=state.get("project_id") or config.DEFAULT_PROJECT_ID,
    )


def _policy(state: ReleaseGraphState) -> ProjectPolicy | None:
    loaded = state.get("project_policy")
    return loaded.policy if loaded else None


def load_policy_node(state: ReleaseGraphState) -> dict[str, object]:
    """Load the project's release policy.

    A missing or invalid policy adds a POLICY_MISSING or POLICY_INVALID
    issue and leaves ``project_policy`` None: the run continues so the
    report is complete, but without exemptions and without approval.
    """
    scope = _scope(state)
    issues = list(state.get("discovery_issues", []))
    try:
        return {"project_policy": POLICY_STORE.load(scope), "discovery_issues": issues}
    except PolicyMissingError as error:
        code, detail = ReasonCode.POLICY_MISSING, f"{error}; add one to enable approvals."
    except PolicyInvalidError as error:
        code, detail = ReasonCode.POLICY_INVALID, f"Release policy is invalid: {error}"
    logger.warning("Policy for %s/%s unusable: %s", scope.tenant, scope.project_id, detail)
    return {
        "project_policy": None,
        "discovery_issues": [*issues, EvidenceIssue(code=code, detail=detail)],
    }


def unmapped_files_issue(
    repository: str, unmapped_files: list[str]
) -> EvidenceIssue | None:
    """Return the issue for changed files no registered service owns, if any."""
    if not unmapped_files:
        return None
    where = f" in {repository}" if repository else ""
    return EvidenceIssue(
        code=ReasonCode.UNMAPPED_FILES,
        detail=(
            f"{len(unmapped_files)} changed file(s){where} are not owned by any "
            f"registered service: {', '.join(unmapped_files)}"
        ),
    )


def extract_trigger_node(state: ReleaseGraphState) -> dict[str, object]:
    """Validate the pull-request payload and resolve the target services.

    Changed files no registered service owns (``unmapped_files``) become an
    UNMAPPED_FILES issue: their impact was never assessed. Files the project
    policy exempts are moved to ``exempted_files`` instead.
    """
    pull_request = PullRequestPayload.model_validate(state["trigger_event"])
    targets = list(state.get("target_services") or [pull_request.target_service])
    issues = list(state.get("discovery_issues", []))
    remaining, exempted = partition_exempt_files(
        _policy(state), state.get("unmapped_files", [])
    )
    unmapped = unmapped_files_issue(pull_request.repository, remaining)
    if unmapped is not None:
        issues.append(unmapped)
    return {
        "pull_request": pull_request,
        "target_services": targets,
        "unmapped_files": remaining,
        "exempted_files": exempted,
        "discovery_issues": issues,
    }


Direction = Literal["upstream", "downstream"]
_Adjacency = Mapping[str, list[tuple[str, int]]]


class BlastRadius(BaseModel):
    """Transitive blast radius of a set of target services.

    ``upstream`` and ``downstream`` hold registered services only, in
    breadth-first order (nearest first). ``matched_edges`` are every edge
    walked in either direction. ``unresolved`` holds one entry per
    unresolved node reached.
    """

    upstream: list[str]
    downstream: list[str]
    matched_edges: list[DependencyEdge]
    unresolved: list[UnresolvedReach]


def _adjacency(
    edges: list[DependencyEdge], direction: Direction
) -> dict[str, list[tuple[str, int]]]:
    """Map each node to ``(neighbour, edge index)`` pairs in one direction.

    ``downstream`` follows edges caller -> callee; ``upstream`` follows them
    backwards, from callee to caller.
    """
    adjacency: dict[str, list[tuple[str, int]]] = {}
    for position, edge in enumerate(edges):
        source, target = edge.source_service_id, edge.target_service_id
        if direction == "upstream":
            source, target = target, source
        adjacency.setdefault(source, []).append((target, position))
    return adjacency


def _breadth_first(
    starts: Iterable[str], adjacency: _Adjacency
) -> tuple[list[str], list[int], dict[str, list[str]]]:
    """Walk ``adjacency`` breadth-first from ``starts``.

    A visited set (seeded with the start nodes) means each node is expanded
    once, so cyclic dependencies terminate and starts are never reported
    as their own neighbours.

    Returns:
        The nodes reached (excluding starts) in visiting order, the indexes
        of every edge walked, and the path from a start to each reached node.
    """
    paths = {start: [start] for start in starts}
    visited = set(paths)
    queue = deque(paths)
    reached: list[str] = []
    walked: list[int] = []
    while queue:
        node = queue.popleft()
        for neighbour, position in adjacency.get(node, []):
            walked.append(position)
            if neighbour in visited:
                continue
            visited.add(neighbour)
            paths[neighbour] = [*paths[node], neighbour]
            reached.append(neighbour)
            queue.append(neighbour)
    return reached, walked, paths


def _unresolved_issue(
    node: str, kind: str, direction: Direction, path: list[str]
) -> str:
    """Describe an unresolved node reached by the traversal.

    The path is rendered in call direction (caller -> callee).
    """
    hops = path if direction == "downstream" else list(reversed(path))
    return (
        f"{UNRESOLVED_DEPENDENCY_ISSUE}: {node} ({kind}) is {direction} of the "
        f"release via {' -> '.join(hops)}"
    )


def traverse_blast_radius(graph: KnowledgeGraph, targets: list[str]) -> BlastRadius:
    """Return the full transitive blast radius of ``targets`` in ``graph``.

    An edge ``A -> B`` means A depends on (calls) B. Every service with a
    path to a target is upstream; every node a target has a path to is
    downstream. A node that is not a registered service (an
    ``UnresolvedEntity`` or any endpoint missing from the inventory) is left
    out of both lists and reported as an issue instead.
    """
    registered = {service.service_id for service in graph.services}
    entities = {e.entity_id: e for e in graph.unresolved_entities}
    walked: set[int] = set()
    radius: dict[Direction, list[str]] = {"upstream": [], "downstream": []}
    unresolved: list[UnresolvedReach] = []
    reported: set[str] = set()
    for direction in ("downstream", "upstream"):
        reached, edges, paths = _breadth_first(
            targets, _adjacency(graph.edges, direction)
        )
        walked.update(edges)
        for node in reached:
            if node in registered:
                radius[direction].append(node)
            elif node not in reported:
                reported.add(node)
                entity = entities.get(node)
                kind = entity.kind.value if entity else "not in service inventory"
                detail = _unresolved_issue(node, kind, direction, paths[node])
                hops = paths[node] if direction == "downstream" else list(reversed(paths[node]))
                unresolved.append(
                    UnresolvedReach(
                        node=node,
                        detail=detail,
                        reference=entity.reference if entity else "",
                        entity_kind=entity.kind if entity else None,
                        via=hops,
                    )
                )
    return BlastRadius(
        upstream=radius["upstream"],
        downstream=radius["downstream"],
        matched_edges=[graph.edges[position] for position in sorted(walked)],
        unresolved=unresolved,
    )


def unresolved_gaps(
    policy: ProjectPolicy | None, unresolved: list[UnresolvedReach]
) -> tuple[list[EvidenceIssue], list[Exemption]]:
    """Turn unresolved nodes into DISCOVERY_GAP issues unless the policy
    exempts them; exempted nodes are returned separately."""
    issues: list[EvidenceIssue] = []
    exempted: list[Exemption] = []
    for reach in unresolved:
        exemption = dependency_exemption(policy, reach.node)
        if exemption is None:
            issues.append(
                EvidenceIssue(
                    code=ReasonCode.DISCOVERY_GAP,
                    detail=reach.detail,
                    subjects=[IssueSubject(kind=IssueSubjectKind.ENTITY, id=reach.node)],
                    entity_kind=reach.entity_kind,
                    root_key=unresolved_key(reach.reference, reach.node),
                    via=reach.via,
                )
            )
        else:
            exempted.append(exemption)
    return issues, exempted


def identity_issues(graph: KnowledgeGraph, services: Iterable[str]) -> list[EvidenceIssue]:
    """IDENTITY_UNLINKED for each service in ``services`` that exists in
    source code but matches no running service (see ``_link_by_name`` in
    discovery): its runtime calls and callers are unknown. Near names are
    offered as questions, never applied."""
    nodes = {node.service_id: node for node in graph.services}
    issues: list[EvidenceIssue] = []
    for service_id in dict.fromkeys(services):
        node = nodes.get(service_id)
        if node is None or node.metadata.get(IDENTITY_UNLINKED_METADATA_KEY) != "true":
            continue
        location = "/".join(
            part
            for part in (node.metadata.get(REPOSITORY_METADATA_KEY), node.metadata.get(SERVICE_ROOT_METADATA_KEY))
            if part
        )
        suggested = [s for s in node.metadata.get(IDENTITY_SUGGESTIONS_METADATA_KEY, "").split(",") if s]
        hint = (
            f" Is it deployed as {' or '.join(suggested)}?" if suggested else ""
        )
        issues.append(
            EvidenceIssue(
                code=ReasonCode.IDENTITY_UNLINKED,
                subjects=[IssueSubject(kind=IssueSubjectKind.SERVICE, id=service_id)],
                root_key=identity_key(node.name),
                detail=(
                    f"{node.name} (source {location or 'unknown'}) matches no running service, "
                    f"so its runtime calls and callers are unknown.{hint} Declare its deployed "
                    "name in release-coord.yaml, or deploy it."
                ),
            )
        )
    return issues


def explain_unset(reach: UnresolvedReach) -> UnresolvedReach:
    """Say why an unset environment variable is still a gap.

    Kubernetes not setting a variable doesn't say where the code connects:
    a default in the code or the container image (which discovery doesn't
    inspect) is used, and live traffic needn't exercise that path. Only a
    reviewed policy exemption accepts it.
    """
    return reach.model_copy(
        update={
            "detail": (
                f"{reach.detail}. Kubernetes doesn't set this variable, so a default "
                "in the code or the container image decides where it points, and "
                "discovery doesn't read those; exempt it in the project policy "
                "(exemptions.dependencies) once reviewed"
            )
        }
    )


def explain_pipeline(reach: UnresolvedReach, graph: KnowledgeGraph) -> UnresolvedReach:
    """Say when the service's pipelines may set an unresolved variable.

    Kubernetes didn't give the variable a readable value, but a build
    pipeline of the service's repository defines it (or imports variables
    we can't read), so the image may carry it. Pipeline values never
    resolve the reference; they are shown for the reviewer.
    """
    entity = next((e for e in graph.unresolved_entities if e.entity_id == reach.node), None)
    if entity is None or entity.kind is not UnresolvedEntityKind.ENVIRONMENT_VARIABLE:
        return reach
    parsed = parse_env_var_reference(entity.reference)
    if parsed is None:
        return reach
    service_name, variable = parsed
    repositories = {
        str(service.metadata[REPOSITORY_METADATA_KEY]).lower()
        for service in graph.services
        if service.name.lower() == service_name and REPOSITORY_METADATA_KEY in service.metadata
    }
    notes: list[str] = []
    for pipeline in graph.pipeline_variables or []:
        if pipeline.repository.lower() not in repositories:
            continue
        names = [*pipeline.variables, *pipeline.withheld_variables, *pipeline.opaque_variables]
        name = next((n for n in names if n.lower() == variable), variable)
        if pipeline.may_define(name):
            notes.append(pipeline.describe(name))
    if not notes:
        return reach
    return reach.model_copy(
        update={
            "detail": (
                f"{reach.detail}. Kubernetes gives it no readable value, but "
                + "; ".join(notes)
                + ": a build may bake it into the image, which discovery doesn't read"
            )
        }
    )


def policy_baseline_issues(
    policy: ProjectPolicy | None, baseline: StoredBaseline, now: datetime
) -> list[EvidenceIssue]:
    """Return the project policy's scope, freshness and required-source issues.

    Without a policy there are none here: the missing policy is already an
    issue of its own (see ``load_policy_node``).
    """
    if policy is None:
        return []
    scope = baseline_scope_issue(policy, baseline.snapshot_id, baseline.graph.scan)
    stale = baseline_freshness_issue(
        policy, baseline.snapshot_id, baseline.created_at, now
    )
    sources = required_source_issues(
        policy, baseline.snapshot_id, baseline.graph.discovery
    )
    return [*([scope] if scope else []), *([stale] if stale else []), *sources]


def resolve_targets(
    graph: KnowledgeGraph, targets: list[str]
) -> tuple[list[str], list[str]]:
    """Resolve target FQURIs against the baseline's service inventory.

    Unknown segments in a target act as wildcards; a target resolves only
    when exactly one registered service matches it.

    Returns:
        The targets, each replaced by the registered FQURI it resolved to
        (unresolved targets are kept as given), and one issue per target
        that did not resolve.
    """
    index = IdentityIndex(service.identity for service in graph.services)
    resolved: list[str] = []
    issues: list[str] = []
    for target in targets:
        try:
            match = index.resolve(ServiceIdentity.parse(target))
        except ValueError:
            match = None
        if match is None:
            issues.append(
                f"{UNRESOLVED_DEPENDENCY_ISSUE}: target {target} does not match "
                "exactly one registered service in the baseline"
            )
        service_id = match.uri if match else target
        if service_id not in resolved:
            resolved.append(service_id)
    return resolved, issues


def load_baseline(
    scope: BaselineScope, snapshot_id: str | None
) -> tuple[StoredBaseline | None, EvidenceIssue | None]:
    """Load the pinned snapshot, or the newest complete one when none is pinned.

    A pinned snapshot is never replaced by another one: if it cannot be
    read, the assessment has no baseline rather than a different one.

    Returns:
        The baseline, or None and a BASELINE_UNAVAILABLE issue saying why.
    """
    where = f"{scope.tenant}/{scope.project_id}"
    if snapshot_id:
        try:
            return BASELINE_STORE.read(scope, snapshot_id), None
        except (OSError, ValueError, DiscoveryIncompleteError) as error:
            logger.warning("Pinned baseline %s/%s unusable: %s", where, snapshot_id, error)
            return None, EvidenceIssue(
                code=ReasonCode.BASELINE_UNAVAILABLE,
                detail=f"Pinned baseline snapshot {where}/{snapshot_id} is unusable: {error}",
            )
    baseline = BASELINE_STORE.latest(scope)
    if baseline is None:
        logger.warning("No complete discovery baseline for %s.", where)
        return None, EvidenceIssue(
            code=ReasonCode.BASELINE_UNAVAILABLE,
            detail=f"No complete discovery baseline found for {where}; run discovery first.",
        )
    return baseline, None


def _revision_issue(detail: str) -> EvidenceIssue:
    return EvidenceIssue(code=ReasonCode.REVISION_UNVERIFIED, detail=detail)


def _compare(
    extractor: DevOpsExtractor, repository: str, base: str, target: str
) -> CommitComparison | None:
    result = extractor.compare_commits(repository, base, target)
    return result.items[0] if result.items else None


def baseline_lag(
    extractor: DevOpsExtractor, graph: KnowledgeGraph, repository: str, base: str, head: str
) -> tuple[RevisionLag | None, EvidenceIssue | None]:
    """Relate the baseline's scanned revision of ``repository`` to ``base``.

    Returns the services changed between them (see ``RevisionLag``); none
    when the head already contains the scanned revision (the branch merged
    the baseline's commit, so the head scan sees what the baseline saw). Or a
    BASELINE_REVISION_MISMATCH issue when they can't be related: the
    baseline didn't scan the repository, or the two have diverged.
    """
    scanned = next(
        (
            revision
            for revision in (graph.scan.scanned_revisions if graph.scan else [])
            if revision.repository.lower() == repository.lower()
        ),
        None,
    )
    mismatch = ReasonCode.BASELINE_REVISION_MISMATCH
    if scanned is None:
        return None, EvidenceIssue(
            code=mismatch,
            detail=f"The baseline records no scanned revision of {repository}.",
        )
    if scanned.commit_sha == base:
        return RevisionLag(), None
    forward = _compare(extractor, repository, scanned.commit_sha, base)
    if forward is not None and forward.base_is_ancestor:
        return RevisionLag(
            radius_services=sorted(changed_services(graph, repository, [forward]))
        ), None
    backward = _compare(extractor, repository, base, scanned.commit_sha)
    if backward is not None and backward.base_is_ancestor:
        contained = _compare(extractor, repository, scanned.commit_sha, head)
        if contained is not None and contained.base_is_ancestor:
            return RevisionLag(), None
        return RevisionLag(
            target_services=sorted(changed_services(graph, repository, [backward]))
        ), None
    return None, EvidenceIssue(
        code=mismatch,
        detail=(
            f"The baseline scanned {repository}@{scanned.commit_sha[:12]}, which "
            f"has diverged from the pull request's base {base[:12]} (or they "
            "couldn't be compared); re-run discovery."
        ),
    )


def verify_revision_node(state: ReleaseGraphState) -> dict[str, object]:
    """Verify the pull request's revisions and read its head's dependencies.

    Source control is the project policy's ``source_control``, never a
    caller-supplied URL. The base must be an ancestor of the head and the
    changed files exactly what the commits change; the baseline's scanned
    revision must relate to the base (see ``baseline_lag``); and the
    changed services are scanned at the head, so the radius is walked over
    the dependencies that will ship (``assessed_graph``). Any failure is a
    REVISION_UNVERIFIED or BASELINE_REVISION_MISMATCH issue, which makes
    the verdict non-actionable.

    Without a policy (already POLICY_MISSING/INVALID) or a loadable
    baseline (reported by the radius node) there is nothing to verify.
    """
    policy = _policy(state)
    baseline, _ = load_baseline(_scope(state), state.get("baseline_snapshot_id"))
    if policy is None or baseline is None:
        return {}
    pull_request: PullRequestPayload = state["pull_request"]
    issues = list(state.get("discovery_issues", []))
    source = policy.source_control
    if source is None:
        issues.append(
            _revision_issue(
                "The project policy names no source_control, so the pull "
                "request's revisions can't be verified or its head scanned."
            )
        )
        return {"discovery_issues": issues}
    repository = pull_request.repository
    extractor = DevOpsExtractor(
        org_url=source.organization_url, project=source.project, repositories=[repository]
    )
    compared = extractor.compare_commits(repository, pull_request.base_sha, pull_request.head_sha)
    if not compared.items:
        issues.append(_revision_issue("; ".join(compared.errors) or "Commits can't be compared."))
        return {"discovery_issues": issues}
    comparison = compared.items[0]
    issues.extend(pull_request_issues(comparison, pull_request.changed_files))

    graph = baseline.graph
    lag, lag_problem = baseline_lag(
        extractor, graph, repository, comparison.base, comparison.target
    )
    if lag_problem is not None:
        issues.append(lag_problem)
    if graph.scan and graph.scan.namespaces and graph.service_environments is None:
        issues.append(
            EvidenceIssue(
                code=ReasonCode.BASELINE_REVISION_MISMATCH,
                detail=(
                    f"Baseline {baseline.snapshot_id} predates recorded container "
                    "environments, so the head's environment variables can't be "
                    "resolved as discovery resolved them; re-run discovery."
                ),
            )
        )

    roots = target_roots(graph, repository, state.get("target_services", []))
    head = extractor.scan_repository(
        repository,
        commit=ScannedRevision(
            repository=repository,
            branch=pull_request.source_branch or "pull-request",
            commit_sha=comparison.target,
        ),
        only_roots=set(roots),
    )
    if not head.items:
        issues.append(
            _revision_issue("The head couldn't be scanned: " + "; ".join(head.errors))
        )
        return {"discovery_issues": issues, "revision_lag": lag}
    head_services = [scan for repo_scan in head.items for scan in repo_scan.services]
    head_coverage = [repo_scan.coverage() for repo_scan in head.items]
    for service in missing_head_services(roots, head_services):
        issues.append(
            _revision_issue(
                f"{service} is no longer a service boundary at the head (removed "
                "or moved); re-run discovery after merging, or declare it in "
                "release-coord.yaml."
            )
        )
    topology = head_topology(
        graph, baseline_identity_context(_scope(state).tenant, graph), roots, head_services
    )
    return {
        "discovery_issues": issues,
        "assessed_graph": topology.graph,
        "dependency_changes": topology,
        "revision_lag": lag,
        "head_coverage": head_coverage,
    }


def review_in_force(
    scope: BaselineScope,
    baseline: StoredBaseline,
    policy: ProjectPolicy | None,
    graph: KnowledgeGraph,
    pending: Mapping[str, list[str]] | None = None,
) -> ReviewInForce | None:
    """The project's dependency review answers that count against
    ``graph`` (None when the project has none).

    Each answer is checked against ``baseline``'s scanned commits: source
    control (the policy's ``source_control``) reports what changed since
    the commit it was given at; ``pending`` (repository -> files a pull
    request changes) count as changes too. An answer that can't be checked
    doesn't count (see ``answers_in_force``).

    Raises:
        ReviewStoreCorruptError, sqlite3.Error, OSError: the store can't be read.
    """
    answers = REVIEW_STORE.answers(scope)
    if not answers:
        return None
    scan = baseline.graph.scan
    current = {r.repository: r.commit_sha for r in scan.scanned_revisions} if scan else {}
    needed = needed_comparisons(answers, current)
    changes: list[RepositoryChanges] = []
    if needed and policy is not None and policy.source_control is not None:
        source = policy.source_control
        extractor = DevOpsExtractor(
            org_url=source.organization_url,
            project=source.project,
            repositories=sorted({repository for repository, _, _ in needed}),
        )
        for repository, anchored, scanned in needed:
            compared = extractor.compare_commits(repository, anchored, scanned)
            if compared.errors:
                logger.warning("Review anchors in %s unchecked: %s", repository, compared.errors)
            changes.extend(to_changes(comparison) for comparison in compared.items)
    return answers_in_force(graph, answers, current, changes, pending, utc_now())


def load_review_node(state: ReleaseGraphState) -> dict[str, object]:
    """Decide which of the project's dependency review answers count for
    this release (see ``review_in_force``); the pull request's changed
    files count as changes. A review store that can't be read is
    REVIEW_UNAVAILABLE: declared dependencies may be missing from the order.
    """
    scope = _scope(state)
    baseline, _ = load_baseline(scope, state.get("baseline_snapshot_id"))
    if baseline is None:
        return {"review_in_force": None}
    pull_request: PullRequestPayload = state["pull_request"]
    scan = baseline.graph.scan
    current = {r.repository: r.commit_sha for r in scan.scanned_revisions} if scan else {}
    try:
        in_force = review_in_force(
            scope,
            baseline,
            _policy(state),
            state.get("assessed_graph") or baseline.graph,
            pending_changes(pull_request.repository, pull_request.changed_files, current),
        )
    except (ReviewStoreCorruptError, sqlite3.Error, OSError) as error:
        logger.warning("Dependency review unreadable: %s", error)
        return {
            "review_in_force": None,
            "discovery_issues": [*state.get("discovery_issues", []), review_unavailable(str(error))],
        }
    return {"review_in_force": in_force}


def project_questions(
    policy: ProjectPolicy | None, baseline: StoredBaseline
) -> list[EvidenceIssue]:
    """Every gap an assessment of any service in ``baseline`` would raise
    that a coordinator can answer: unresolved dependencies the policy
    doesn't exempt, source-only identities, and unread source files."""
    graph = baseline.graph
    services = [service.service_id for service in graph.services]
    radius = traverse_blast_radius(graph, services)
    unset_kind = {
        entity.entity_id
        for entity in graph.unresolved_entities
        if entity.kind is UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET
    }
    gaps, _ = unresolved_gaps(
        policy,
        [
            explain_unset(reach) if reach.node in unset_kind else explain_pipeline(reach, graph)
            for reach in radius.unresolved
        ],
    )
    coverage, _ = static_coverage_issues(policy, graph, baseline.snapshot_id)
    return [*gaps, *identity_issues(graph, services), *coverage]


def calculate_static_radius_node(state: ReleaseGraphState) -> dict[str, object]:
    """Compute the full transitive blast radius of the target services.

    The snapshot pinned in ``baseline_snapshot_id`` is loaded, or the
    project's newest complete one when none is pinned (see
    ``load_baseline``). Targets are resolved to registered FQURIs, then the
    graph is walked breadth-first in both directions with no depth limit
    (see ``traverse_blast_radius``).

    Every reason the radius cannot be trusted is added to
    ``discovery_issues``, which rejects the release without calling the LLM:
    no usable baseline, a target that does not resolve, an unresolved
    dependency anywhere in the radius that the project policy does not
    exempt, a baseline relying on mock data, a baseline that did not scan
    the policy's environment and cluster, a baseline older than the policy
    allows, a required evidence source that did not succeed, commits
    between the baseline and the base that change the release (see
    ``RevisionLag``), source files the static scan couldn't read (see
    ``static_coverage_issues``), or a service in the radius known only from
    source code (see ``identity_issues``). The radius is walked over ``assessed_graph`` (the
    head's dependencies) when the head was scanned, the baseline otherwise, with
    the project's dependency review applied (``confirmed_map``); issues an
    answered question covers become context in ``assessment_review``.
    """
    scope = _scope(state)
    policy = _policy(state)
    issues = list(state.get("discovery_issues", []))
    baseline, problem = load_baseline(scope, state.get("baseline_snapshot_id"))
    if baseline is None:
        assert problem is not None
        return {
            "upstream_services": [],
            "downstream_services": [],
            "matched_edges": [],
            "discovery_issues": [*issues, problem],
            "baseline_runtime_state": RuntimeEvidenceState.NONE,
            "baseline_graph": None,
        }

    graph = state.get("assessed_graph") or baseline.graph
    in_force = state.get("review_in_force")
    confirmed = confirmed_map(graph, in_force) if in_force is not None else None
    requested = list(state.get("target_services", []))
    if confirmed is not None:
        graph = confirmed.graph
        requested = [confirmed.rename(target) for target in requested]
    targets, target_issues = resolve_targets(graph, requested)
    radius = traverse_blast_radius(graph, targets)
    lag = state.get("revision_lag")
    lag_issue = (
        lag.issue(targets, [*radius.upstream, *radius.downstream]) if lag else None
    )
    if lag_issue is not None:
        issues.append(lag_issue)
    unset_kind = {
        entity.entity_id
        for entity in graph.unresolved_entities
        if entity.kind is UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET
    }
    gaps, exempted = unresolved_gaps(
        policy,
        [
            explain_unset(reach) if reach.node in unset_kind else explain_pipeline(reach, graph)
            for reach in radius.unresolved
        ],
    )
    issues.extend(_gap(issue) for issue in target_issues)
    issues.extend(gaps)
    issues.extend(policy_baseline_issues(policy, baseline, utc_now()))
    issues.extend(identity_issues(baseline.graph, [*targets, *radius.upstream, *radius.downstream]))
    coverage_issues, unscanned_exempted = static_coverage_issues(
        policy, baseline.graph, baseline.snapshot_id, state.get("head_coverage") or []
    )
    issues.extend(coverage_issues)
    if baseline.demo_mode:
        issues.append(
            EvidenceIssue(
                code=ReasonCode.DEMO_EVIDENCE,
                detail=(
                    f"Baseline snapshot {baseline.snapshot_id} relies on mock "
                    "fixture data."
                ),
            )
        )
    review = None
    if confirmed is not None and in_force is not None:
        issues, accepted = split_accepted(issues, confirmed.accepted)
        review = assessment_review(confirmed, in_force, accepted, radius.matched_edges)
    return {
        "target_services": targets,
        "upstream_services": radius.upstream,
        "downstream_services": radius.downstream,
        "matched_edges": radius.matched_edges,
        "discovery_issues": issues,
        "assessment_review": review,
        "exempted_dependencies": exempted,
        "exempted_unscanned_files": unscanned_exempted,
        "shape_findings": radius_shape_findings(
            baseline.graph, [*targets, *radius.upstream, *radius.downstream]
        ),
        "baseline_snapshot_id": baseline.snapshot_id,
        "baseline_created_at": baseline.created_at,
        "baseline_demo_mode": baseline.demo_mode,
        "baseline_runtime_state": baseline_runtime_state(baseline.graph.discovery),
        "baseline_graph": baseline.graph,
    }


def validate_runtime_telemetry_node(state: ReleaseGraphState) -> dict[str, object]:
    """Gather runtime evidence for the services in the release radius.

    With a blank ``workspace_id`` only the baseline is used: its runtime
    edges in the radius are reported as ``baseline_runtime_edges`` and the
    evidence is the baseline's state. Otherwise the telemetry workspace is
    queried live, restricted to the project policy's telemetry cluster and
    lookback (``runtime_lookback_days``). A FAILED or PARTIAL live query is
    a DISCOVERY_GAP, and a query answered from the mock fixture
    DEMO_EVIDENCE.

    Real live edges are resolved against the baseline. Their coverage of
    the radius classifies the evidence (``live_runtime_state``): no rows is
    LIVE_EMPTY, an old newest row LIVE_STALE, an unobserved radius service
    LIVE_UNCOVERED, an unobserved service talking only protocols the
    telemetry can't see LIVE_UNOBSERVABLE (``unobservable_protocols``),
    otherwise LIVE. When that is weaker than the policy requires, a
    REQUIRED_SOURCE_MISSING (or RUNTIME_UNOBSERVABLE) issue explains why. The resolved
    edges touching the radius are ``runtime_observed_edges``; a workspace
    other than the baseline's is a DISCOVERY_GAP, and radius edges the
    baseline lacks are BASELINE_DRIFT. Metrics are keyed by service FQURI.
    A role-profile probe adds the radius services' observability gaps
    (``observability_gaps``; context only, never an issue).
    """
    baseline_edges = [
        f"{edge.source_service_id} -> {edge.target_service_id}"
        for edge in state.get("matched_edges", [])
        if DiscoveryMethod.RUNTIME_TELEMETRY in edge.discovery_methods
    ]
    workspace_id = (state.get("workspace_id") or "").strip()
    if not workspace_id:
        status = TelemetryStatus(
            source="knowledge_graph",
            runtime_evidence=state.get(
                "baseline_runtime_state", RuntimeEvidenceState.NONE
            ),
            baseline_runtime_edges=baseline_edges,
        )
        return _apply_runtime_policy(
            state, status, list(state.get("discovery_issues", []))
        )

    radius = _radius(state)
    policy = _policy(state)
    extractor = TelemetryExtractor(
        workspace_id=workspace_id,
        additional_workspaces=policy.telemetry_additional_workspaces if policy else (),
    )
    result = extractor.get_active_dependencies(
        days_back=(
            policy.runtime_lookback_days if policy else config.DEFAULT_RUNTIME_LOOKBACK_DAYS
        ),
        scope=policy.telemetry_scope if policy else None,
    )
    issues = list(state.get("discovery_issues", []))
    if result.status.is_incomplete:
        issues.append(
            _gap(f"Live telemetry {result.status.value}: {'; '.join(result.errors)}")
        )
    is_mock = extractor.is_mock or any(edge.is_mock_fixture for edge in result.items)
    if is_mock:
        issues.append(
            EvidenceIssue(
                code=ReasonCode.DEMO_EVIDENCE,
                detail="Live telemetry was answered from the mock fixture.",
            )
        )
    metrics = radius_metrics(radius, extractor.get_runtime_metrics())
    graph = state.get("baseline_graph")
    observed: list[ObservedEdge] = []
    drift: list[ObservedEdge] = []
    if not is_mock and graph is not None:
        observed = resolve_live_edges(
            graph, baseline_identity_context(_scope(state).tenant, graph), result.items
        )
        drift_issues, drift = live_drift_issues(state, workspace_id, observed)
        issues.extend(drift_issues)
    now = utc_now()
    blind = unobservable_protocols(graph.edges, radius) if graph is not None else {}
    coverage = live_coverage(policy, observed, radius, now, blind).model_copy(
        update={"sampled_sources": extractor.sampled_sources}
    )
    if coverage.uncovered_services and not is_mock and policy is not None:
        coverage = with_unscoped_sources(
            coverage,
            extractor.get_unscoped_sources(
                policy.runtime_lookback_days, policy.telemetry_scope
            ),
        )
    if is_mock:
        runtime_state, why = RuntimeEvidenceState.MOCK, ""
    elif result.status.is_incomplete:
        runtime_state, why = RuntimeEvidenceState.LIVE_INCOMPLETE, ""
    elif graph is None:
        runtime_state, why = (
            RuntimeEvidenceState.LIVE_UNCOVERED,
            "There is no baseline to resolve live telemetry against.",
        )
    else:
        runtime_state, why = live_runtime_state(policy, coverage, now)
    gaps = None
    if not is_mock and graph is not None:
        profiles = extractor.get_role_profiles(
            policy.runtime_lookback_days if policy else config.DEFAULT_RUNTIME_LOOKBACK_DAYS,
            policy.telemetry_scope if policy else None,
        )
        if profiles is not None:
            gaps = observability_gaps(
                graph,
                baseline_identity_context(_scope(state).tenant, graph),
                profiles,
                result.items,
                now,
                radius,
            )
    status = TelemetryStatus(
        source="mock" if is_mock else "live",
        runtime_evidence=runtime_state,
        runtime_observed_edges=[
            f"{edge.source_id} -> {edge.target_id}"
            for edge in observed
            if edge.source_id in radius or edge.target_id in radius
        ],
        baseline_runtime_edges=baseline_edges,
        runtime_metrics=metrics,
        unbaselined_edges=drift,
        coverage=None if is_mock else coverage,
        dropped_records=extractor.dropped_records,
        observability_gaps=gaps,
    )
    return _apply_runtime_policy(state, status, issues, why)


def _radius(state: ReleaseGraphState) -> set[str]:
    return {
        *state.get("target_services", []),
        *state.get("upstream_services", []),
        *state.get("downstream_services", []),
    }


def radius_metrics(
    radius: Iterable[str], metrics: Mapping[str, Mapping[str, int | float]]
) -> dict[str, dict[str, float]]:
    """Key per-source telemetry metrics by the radius FQURI they belong to.

    A ``<namespace>/<name>`` key matches that exact service; a bare name
    matches when exactly one radius service has it. Other keys are dropped.
    """
    identities = []
    for service_id in radius:
        try:
            identities.append(ServiceIdentity.parse(service_id))
        except ValueError:
            continue
    keyed: dict[str, dict[str, float]] = {}
    for key, values in metrics.items():
        namespace, separator, name = key.partition("/")
        matches = [
            identity
            for identity in identities
            if (
                (identity.namespace, identity.service_name) == (namespace, name)
                if separator
                else identity.service_name == key
            )
        ]
        if len(matches) == 1:
            keyed[matches[0].uri] = {metric: float(value) for metric, value in values.items()}
    return keyed


def live_drift_issues(
    state: ReleaseGraphState, workspace_id: str, observed: list[ObservedEdge]
) -> tuple[list[EvidenceIssue], list[ObservedEdge]]:
    """Compare resolved real live edges with the baseline.

    Returns the issues and the unbaselined edges. Nothing is compared when
    there is no baseline (already BASELINE_UNAVAILABLE) or the live
    workspace is not the baseline's (a DISCOVERY_GAP: the edges are not
    comparable). Edges to an unresolved entity the project policy exempts,
    by dependency exemption or egress rule, are not drift.
    """
    graph = state.get("baseline_graph")
    if graph is None:
        return [], []
    mismatch = workspace_mismatch_issue(graph, workspace_id)
    if mismatch is not None:
        return [mismatch], []
    policy = _policy(state)
    endpoints = {edge.source_id for edge in observed} | {edge.target_id for edge in observed}
    exempt = {node for node in endpoints if dependency_exemption(policy, node) is not None}
    drift = unbaselined_edges(graph, _radius(state), observed, exempt)
    issue = drift_issue(state.get("baseline_snapshot_id") or "unknown", drift)
    return ([issue] if issue else []), drift


def _apply_runtime_policy(
    state: ReleaseGraphState,
    status: TelemetryStatus,
    issues: list[EvidenceIssue],
    why: str = "",
) -> dict[str, object]:
    """Return the node update with the policy's runtime evidence issue added.

    Live evidence never settles an unset environment variable: it shows
    which services are active, not that the code path reading the variable
    ran, so those stay gaps unless the policy exempts them (see
    ``explain_unset``).
    """
    policy = _policy(state)
    if policy is not None:
        issue = runtime_evidence_issue(policy, status.runtime_evidence, why)
        if issue is not None:
            issues.append(issue)
    return {"telemetry_status": status, "discovery_issues": issues}


def sequence_deployment_node(state: ReleaseGraphState) -> dict[str, object]:
    """Compute the deterministic deployment plan before any LLM call.

    Topologically sorts the release subgraph (targets, upstream, downstream
    and the matched edges between them) for the PR's declared change type;
    see ``sequence_deployment``.
    """
    pull_request: PullRequestPayload = state["pull_request"]
    plan = sequence_deployment(
        targets=list(state.get("target_services", [])),
        upstream=list(state.get("upstream_services", [])),
        downstream=list(state.get("downstream_services", [])),
        edges=state.get("matched_edges", []),
        change_type=pull_request.change_type,
    )
    return {"deployment_plan": plan, "deployment_sequence": plan.sequence}


def assess_risk_and_sequence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Consult the advisory model if appropriate, then apply release policy.

    The deployment sequence comes from ``sequence_deployment_node`` and is
    never changed here. The Anthropic API is not called when there are
    evidence issues or the subgraph has a dependency cycle (``needs_model``).
    The final verdict is always decided in code by ``decide_release``.
    """
    plan: DeploymentPlan = state["deployment_plan"]
    issues = list(state.get("discovery_issues", []))
    advisory = None
    loaded = state.get("project_policy")
    disabled = loaded is not None and not loaded.policy.external_ai
    redactions: dict[str, int] = {}
    if needs_model(issues, plan) and not disabled:
        request = RiskAssessmentRequest(
            pull_request=state["pull_request"],
            change_type=plan.change_type,
            target_services=list(state.get("target_services", [])),
            upstream_services=list(state.get("upstream_services", [])),
            downstream_services=list(state.get("downstream_services", [])),
            deployment_sequence=plan.sequence,
            services_to_deploy=plan.services_to_deploy,
            stale_consumers=plan.stale_consumers,
            matched_edges=list(state.get("matched_edges", [])),
            telemetry=state.get("telemetry_status")
            or TelemetryStatus(source="knowledge_graph"),
        )
        reasoner = RiskReasoner()
        advisory = reasoner.assess(request)
        redactions = reasoner.last_redactions
    return {
        "advisory_assessment": advisory,
        "advisory_redactions": redactions,
        "verdict": decide_release(issues, plan, advisory, advisory_disabled=disabled),
    }


def assessment_evidence(state: ReleaseGraphState) -> AssessmentEvidence:
    """Return the exact inputs this run assessed."""
    pull_request: PullRequestPayload = state["pull_request"]
    telemetry = state.get("telemetry_status")
    loaded = state.get("project_policy")
    graph = state.get("baseline_graph")
    scan = graph.scan if graph else None
    changes = state.get("dependency_changes")
    return AssessmentEvidence(
        pr_id=pull_request.pr_id,
        repository=pull_request.repository,
        base_sha=pull_request.base_sha,
        head_sha=pull_request.head_sha,
        baseline_snapshot_id=state.get("baseline_snapshot_id") or None,
        changed_files=list(pull_request.changed_files),
        unmapped_files=list(state.get("unmapped_files", [])),
        exempted_files=list(state.get("exempted_files", [])),
        exempted_dependencies=list(state.get("exempted_dependencies", [])),
        baseline_created_at=state.get("baseline_created_at"),
        demo_mode=bool(state.get("baseline_demo_mode"))
        or (telemetry is not None and telemetry.source == "mock"),
        runtime_evidence=(
            telemetry.runtime_evidence if telemetry else RuntimeEvidenceState.NONE
        ),
        policy_version=loaded.policy.policy_version if loaded else None,
        policy_sha256=loaded.sha256 if loaded else None,
        baseline_environment=scan.environment if scan else None,
        baseline_cluster=scan.cluster if scan else None,
        scanned_revisions=list(scan.scanned_revisions) if scan else [],
        unbaselined_edges=list(telemetry.unbaselined_edges) if telemetry else [],
        live_coverage=telemetry.coverage if telemetry else None,
        observability_gaps=telemetry.observability_gaps if telemetry else None,
        head_scanned=changes is not None,
        dependencies_added=list(changes.added) if changes else [],
        dependencies_removed=list(changes.removed) if changes else [],
        exempted_unscanned_files=list(state.get("exempted_unscanned_files", [])),
        shape_findings=list(state.get("shape_findings", [])),
        review=state.get("assessment_review"),
        advisory_redactions=dict(state.get("advisory_redactions") or {}),
        regulated_impact=regulated_impact(
            loaded.policy if loaded else None,
            state.get("target_services", []),
            changes.added if changes else [],
            changes.removed if changes else [],
        ),
    )


def build_release_assessment(state: ReleaseGraphState) -> ReleaseAssessment:
    """Return the typed verdict the control plane parses from the run."""
    plan: DeploymentPlan = state["deployment_plan"]
    verdict = state["verdict"]
    return ReleaseAssessment(
        approved=verdict.approved,
        actionable=verdict.actionable,
        reason_codes=verdict.reason_codes,
        risk_score=verdict.risk_score,
        safe_order_established=plan.safe_order_established,
        deployment_sequence=plan.sequence,
        services_to_deploy=plan.services_to_deploy,
        stale_consumers=plan.stale_consumers,
        justification=verdict.justification,
        advisory_assessment=state.get("advisory_assessment"),
        evidence=assessment_evidence(state),
        issues=list(state.get("discovery_issues", [])),
    )


def _render_report(assessment: ReleaseAssessment, state: ReleaseGraphState) -> str:
    """Render an assessment and its radius as a Markdown report."""
    pull_request: PullRequestPayload = state["pull_request"]
    evidence = assessment.evidence

    def _list(values: Iterable[str]) -> str:
        return ", ".join(values) or "None identified"

    snapshot = evidence.baseline_snapshot_id or "None"
    if evidence.demo_mode:
        snapshot += " (DEMO MODE: relies on mock fixture data)"
    revision = (
        f"{evidence.repository or 'unknown repository'} "
        f"{evidence.base_sha or '?'}..{evidence.head_sha or '?'}"
    )
    risk = (
        f"{assessment.risk_score:.2f}"
        if assessment.risk_score is not None
        else "Not assessed (insufficient evidence)"
    )
    advisory = assessment.advisory_assessment
    advisory_text = (
        f"{'approve' if advisory.approved else 'reject'} "
        f"(risk {advisory.risk_score:.2f})"
        if advisory
        else "Not consulted"
    )
    issues = [f"[{issue.code.value}] {issue.detail}" for issue in assessment.issues]
    policy = (
        f"version {evidence.policy_version} (sha256 {evidence.policy_sha256[:12]})"
        if evidence.policy_version is not None and evidence.policy_sha256
        else "None loaded"
    )
    created = (
        evidence.baseline_created_at.isoformat()
        if evidence.baseline_created_at
        else "Unknown"
    )
    exemptions = [
        f"{item.subject} ({item.reason})"
        for item in [
            *evidence.exempted_files,
            *evidence.exempted_dependencies,
            *evidence.exempted_unscanned_files,
        ]
    ]
    shape = [
        f"{finding.namespace}/{finding.object} [{finding.kind.value}]: {finding.detail}"
        for finding in evidence.shape_findings
    ]
    target = (
        f"{evidence.baseline_environment}/{evidence.baseline_cluster}"
        if evidence.baseline_environment and evidence.baseline_cluster
        else "Not recorded"
    )
    revisions = [
        f"{revision.repository}@{revision.commit_sha} ({revision.branch})"
        + (
            f" [PR base {evidence.base_sha} differs]"
            if revision.repository.lower() == evidence.repository.lower()
            and evidence.base_sha
            and not revision.commit_sha.startswith(evidence.base_sha.lower())
            else ""
        )
        for revision in evidence.scanned_revisions
    ]
    head_text = (
        "Verified; changed services read at the head"
        if evidence.head_scanned
        else "Not verified (see evidence issues)"
    )
    added = [f"{c.source_id} -> {c.target_id}" for c in evidence.dependencies_added]
    removed = [f"{c.source_id} -> {c.target_id}" for c in evidence.dependencies_removed]
    drift = [f"{edge.source_id} -> {edge.target_id}" for edge in evidence.unbaselined_edges]
    coverage = evidence.live_coverage
    coverage_text = (
        "No live query"
        if coverage is None
        else (
            f"{coverage.edge_count} live edge(s), newest "
            f"{coverage.newest_observed_at.isoformat() if coverage.newest_observed_at else 'none'}; "
            f"unobserved radius services: {_list(coverage.uncovered_services)}; "
            f"allowlisted silent: {_list(coverage.silent_services)}"
        )
    )
    return (
        "## Release Impact Assessment\n\n"
        f"**Pull Request:** {pull_request.pr_id}: {pull_request.title}\n\n"
        f"**Revision:** {revision}\n\n"
        f"**Baseline Snapshot:** {snapshot}\n\n"
        f"**Baseline Created:** {created}\n\n"
        f"**Baseline Environment/Cluster:** {target}\n\n"
        f"**Scanned Revisions:** {_list(revisions)}\n\n"
        f"**Pull Request Head:** {head_text}\n\n"
        f"**Dependencies Added by the PR:** {_list(added)}\n\n"
        f"**Dependencies Removed by the PR:** {_list(removed)}\n\n"
        f"**Release Policy:** {policy}\n\n"
        f"**Runtime Evidence:** {evidence.runtime_evidence.value}\n\n"
        f"**Live Coverage:** {coverage_text}\n\n"
        f"**Live Edges Missing From Baseline:** {_list(drift)}\n\n"
        f"**Policy Exemptions:** {_list(exemptions)}\n\n"
        f"**Target Services:** {_list(state.get('target_services', []))}\n\n"
        f"**Unmapped Files:** {_list(evidence.unmapped_files)}\n\n"
        f"**Out-of-Shape Kubernetes Objects in the Radius:** {_list(shape)}\n\n"
        "**Blast Radius:**\n"
        "- **Upstream (depend on the target, directly or transitively):** "
        f"{_list(state.get('upstream_services', []))}\n"
        "- **Downstream (called by the target, directly or transitively):** "
        f"{_list(state.get('downstream_services', []))}\n\n"
        f"**Approved:** {'Yes' if assessment.approved else 'No'}\n\n"
        f"**Actionable:** {'Yes' if assessment.actionable else 'No'}\n\n"
        f"**Reason Codes:** {_list(code.value for code in assessment.reason_codes)}\n\n"
        f"**Risk Score:** {risk}\n\n"
        f"**Advisory Model:** {advisory_text}\n\n"
        f"**Evidence Issues:** {_list(issues)}\n\n"
        "**Safe Order Established:** "
        f"{'Yes' if assessment.safe_order_established else 'No'}\n\n"
        f"**Recommended Sequence:** {' -> '.join(assessment.deployment_sequence)}\n\n"
        "**Services to Deploy:** "
        f"{' -> '.join(assessment.services_to_deploy) or 'Not determined'}\n\n"
        "**Stale Consumers (breaking change, not updated by this PR):** "
        f"{_list(assessment.stale_consumers)}\n\n"
        f"**Justification:** {assessment.justification}"
    )


def compile_evidence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Emit the typed verdict and render the evidence as a Markdown report.

    ``release_assessment`` is the machine-readable output for the control
    plane; ``evidence_report`` is the human-readable summary.
    """
    assessment = build_release_assessment(state)
    return {
        "evidence_report": _render_report(assessment, state),
        "release_assessment": assessment,
    }
