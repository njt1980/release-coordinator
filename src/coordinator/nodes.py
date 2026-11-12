"""Node functions for the release orchestration graph."""

import logging
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Final, Literal

from pydantic import BaseModel

from src.coordinator.policy import decide_release, needs_model
from src.coordinator.sequencing import DeploymentPlan, sequence_deployment
from src.coordinator.state import ReleaseGraphState
from src.core import config
from src.core.models import (
    AssessmentEvidence,
    DependencyEdge,
    DiscoveryIncompleteError,
    DiscoveryMethod,
    EvidenceIssue,
    KnowledgeGraph,
    PullRequestPayload,
    ReasonCode,
    ReleaseAssessment,
    ServiceIdentity,
    TelemetryStatus,
)
from src.core.service_ids import IdentityIndex
from src.tools.baseline_store import BaselineScope, StoredBaseline, default_store
from src.tools.risk_reasoner import RiskAssessmentRequest, RiskReasoner
from src.tools.telemetry_extractor import TelemetryExtractor

logger = logging.getLogger(__name__)

BASELINE_STORE = default_store()

UNRESOLVED_DEPENDENCY_ISSUE: Final[str] = "Unresolved dependency in critical path"


def _service_name(service_id: str) -> str:
    """Return the bare service name of a FQURI (or the ID itself otherwise)."""
    try:
        return ServiceIdentity.parse(service_id).service_name
    except ValueError:
        return service_id


def _gap(detail: str) -> EvidenceIssue:
    return EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail=detail)


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
    UNMAPPED_FILES issue: their impact was never assessed.
    """
    pull_request = PullRequestPayload.model_validate(state["trigger_event"])
    targets = list(state.get("target_services") or [pull_request.target_service])
    issues = list(state.get("discovery_issues", []))
    unmapped = unmapped_files_issue(
        pull_request.repository, list(state.get("unmapped_files", []))
    )
    if unmapped is not None:
        issues.append(unmapped)
    return {
        "pull_request": pull_request,
        "target_services": targets,
        "discovery_issues": issues,
    }


Direction = Literal["upstream", "downstream"]
_Adjacency = Mapping[str, list[tuple[str, int]]]


class BlastRadius(BaseModel):
    """Transitive blast radius of a set of target services.

    ``upstream`` and ``downstream`` hold registered services only, in
    breadth-first order (nearest first). ``matched_edges`` are every edge
    walked in either direction. ``issues`` holds one entry per unresolved
    node reached.
    """

    upstream: list[str]
    downstream: list[str]
    matched_edges: list[DependencyEdge]
    issues: list[str]


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
    kinds = {e.entity_id: e.kind.value for e in graph.unresolved_entities}
    walked: set[int] = set()
    radius: dict[Direction, list[str]] = {"upstream": [], "downstream": []}
    issues: list[str] = []
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
                kind = kinds.get(node, "not in service inventory")
                issues.append(_unresolved_issue(node, kind, direction, paths[node]))
    return BlastRadius(
        upstream=radius["upstream"],
        downstream=radius["downstream"],
        matched_edges=[graph.edges[position] for position in sorted(walked)],
        issues=issues,
    )


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
    dependency anywhere in the radius, or a baseline relying on mock data.
    """
    scope = BaselineScope(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        project_id=state.get("project_id") or config.DEFAULT_PROJECT_ID,
    )
    issues = list(state.get("discovery_issues", []))
    baseline, problem = load_baseline(scope, state.get("baseline_snapshot_id"))
    if baseline is None:
        assert problem is not None
        return {
            "upstream_services": [],
            "downstream_services": [],
            "matched_edges": [],
            "discovery_issues": [*issues, problem],
        }

    targets, target_issues = resolve_targets(
        baseline.graph, list(state.get("target_services", []))
    )
    radius = traverse_blast_radius(baseline.graph, targets)
    issues.extend(_gap(issue) for issue in [*target_issues, *radius.issues])
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
    return {
        "target_services": targets,
        "upstream_services": radius.upstream,
        "downstream_services": radius.downstream,
        "matched_edges": radius.matched_edges,
        "discovery_issues": issues,
        "baseline_snapshot_id": baseline.snapshot_id,
        "baseline_demo_mode": baseline.demo_mode,
    }


def validate_runtime_telemetry_node(state: ReleaseGraphState) -> dict[str, object]:
    """Gather runtime evidence for the services in the release radius.

    With a blank ``workspace_id`` only the knowledge graph is used: runtime
    evidence is the matched edges that carry RUNTIME_TELEMETRY. Otherwise the
    telemetry workspace is queried live and its metrics are filtered to the
    services in the radius. A FAILED or PARTIAL live query is added to
    ``discovery_issues`` as a DISCOVERY_GAP, and a query answered from the
    mock fixture as DEMO_EVIDENCE (its status ``source`` is then ``mock``).
    """
    observed_edges = [
        f"{edge.source_service_id} -> {edge.target_service_id}"
        for edge in state.get("matched_edges", [])
        if DiscoveryMethod.RUNTIME_TELEMETRY in edge.discovery_methods
    ]
    workspace_id = (state.get("workspace_id") or "").strip()
    if not workspace_id:
        status = TelemetryStatus(
            source="knowledge_graph", runtime_observed_edges=observed_edges
        )
        return {"telemetry_status": status}

    radius = {
        _service_name(service)
        for service in [
            *state.get("target_services", []),
            *state.get("upstream_services", []),
            *state.get("downstream_services", []),
        ]
    }
    extractor = TelemetryExtractor(workspace_id=workspace_id)
    result = extractor.get_active_dependencies(days_back=7)
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
    runtime_metrics = {
        service: {name: float(value) for name, value in metrics.items()}
        for service, metrics in extractor.get_runtime_metrics().items()
        if _service_name(service) in radius
    }
    status = TelemetryStatus(
        source="mock" if is_mock else "live",
        runtime_observed_edges=observed_edges,
        runtime_metrics=runtime_metrics,
    )
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
    if needs_model(issues, plan):
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
        advisory = RiskReasoner().assess(request)
    return {
        "advisory_assessment": advisory,
        "verdict": decide_release(issues, plan, advisory),
    }


def assessment_evidence(state: ReleaseGraphState) -> AssessmentEvidence:
    """Return the exact inputs this run assessed."""
    pull_request: PullRequestPayload = state["pull_request"]
    telemetry = state.get("telemetry_status")
    return AssessmentEvidence(
        pr_id=pull_request.pr_id,
        repository=pull_request.repository,
        base_sha=pull_request.base_sha,
        head_sha=pull_request.head_sha,
        baseline_snapshot_id=state.get("baseline_snapshot_id") or None,
        changed_files=list(pull_request.changed_files),
        unmapped_files=list(state.get("unmapped_files", [])),
        demo_mode=bool(state.get("baseline_demo_mode"))
        or (telemetry is not None and telemetry.source == "mock"),
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
    return (
        "## Release Impact Assessment\n\n"
        f"**Pull Request:** {pull_request.pr_id}: {pull_request.title}\n\n"
        f"**Revision:** {revision}\n\n"
        f"**Baseline Snapshot:** {snapshot}\n\n"
        f"**Target Services:** {_list(state.get('target_services', []))}\n\n"
        f"**Unmapped Files:** {_list(evidence.unmapped_files)}\n\n"
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
