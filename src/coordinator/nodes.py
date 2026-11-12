"""Node functions for the release orchestration graph."""

import logging
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Final, Literal

from pydantic import BaseModel

from src.coordinator.sequencing import DeploymentPlan, sequence_deployment
from src.coordinator.state import ReleaseGraphState
from src.core import config
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    PullRequestPayload,
    ReleaseAssessment,
    ServiceIdentity,
    TelemetryStatus,
)
from src.core.service_ids import IdentityIndex
from src.tools.baseline_store import BaselineScope, default_store
from src.tools.risk_reasoner import RiskAssessmentRequest, RiskReasoner
from src.tools.telemetry_extractor import TelemetryExtractor

logger = logging.getLogger(__name__)

BASELINE_STORE = default_store()

# Score reported when the assessment is refused for missing evidence: an
# unassessable release is treated as maximally risky.
DISCOVERY_GAP_RISK_SCORE = 1.0


UNRESOLVED_DEPENDENCY_ISSUE: Final[str] = "Unresolved dependency in critical path"


def _service_name(service_id: str) -> str:
    """Return the bare service name of a FQURI (or the ID itself otherwise)."""
    try:
        return ServiceIdentity.parse(service_id).service_name
    except ValueError:
        return service_id


def extract_trigger_node(state: ReleaseGraphState) -> dict[str, object]:
    """Validate the pull-request payload and resolve the target services."""
    pull_request = PullRequestPayload.model_validate(state["trigger_event"])
    targets = list(state.get("target_services") or [pull_request.target_service])
    return {"pull_request": pull_request, "target_services": targets}


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


def calculate_static_radius_node(state: ReleaseGraphState) -> dict[str, object]:
    """Compute the full transitive blast radius of the target services.

    The project's newest complete baseline snapshot is loaded (see
    ``BaselineStore.latest``); failed, partial or unreadable snapshots are
    skipped, so they never shadow an older good baseline. Targets are
    resolved to registered FQURIs, then the graph is walked breadth-first in
    both directions with no depth limit (see ``traverse_blast_radius``).

    Every reason the radius cannot be trusted is reported in
    ``discovery_issues``, which rejects the release without calling the LLM:
    no complete baseline, a target that does not resolve, or an unresolved
    dependency anywhere in the radius.
    """
    scope = BaselineScope(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        project_id=state.get("project_id") or config.DEFAULT_PROJECT_ID,
    )
    baseline = BASELINE_STORE.latest(scope)
    if baseline is None:
        logger.warning(
            "No complete discovery baseline for %s/%s; returning no static dependents.",
            scope.tenant,
            scope.project_id,
        )
        return {
            "upstream_services": [],
            "downstream_services": [],
            "matched_edges": [],
            "discovery_issues": [
                "No complete discovery baseline found for "
                f"{scope.tenant}/{scope.project_id}; run discovery first."
            ],
        }

    targets, target_issues = resolve_targets(
        baseline.graph, list(state.get("target_services", []))
    )
    radius = traverse_blast_radius(baseline.graph, targets)
    return {
        "target_services": targets,
        "upstream_services": radius.upstream,
        "downstream_services": radius.downstream,
        "matched_edges": radius.matched_edges,
        "discovery_issues": [*target_issues, *radius.issues],
        "baseline_snapshot_id": baseline.snapshot_id,
        "baseline_demo_mode": baseline.demo_mode,
    }


def validate_runtime_telemetry_node(state: ReleaseGraphState) -> dict[str, object]:
    """Gather runtime evidence for the services in the release radius.

    With a blank ``workspace_id`` only the knowledge graph is used: runtime
    evidence is the matched edges that carry RUNTIME_TELEMETRY. Otherwise the
    telemetry workspace is queried live and its metrics are filtered to the
    services in the radius. A FAILED or PARTIAL live query is added to
    ``discovery_issues``.
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
            f"Live telemetry {result.status.value}: {'; '.join(result.errors)}"
        )
    runtime_metrics = {
        service: {name: float(value) for name, value in metrics.items()}
        for service, metrics in extractor.get_runtime_metrics().items()
        if _service_name(service) in radius
    }
    status = TelemetryStatus(
        source="live",
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


def discovery_gap_rejection(
    issues: list[str], deployment_sequence: list[str]
) -> dict[str, object]:
    """Return the deterministic verdict for a release with discovery gaps.

    No LLM is consulted: a verdict over incomplete dependency evidence is not
    safe to act on, so the release is rejected at maximum risk and every gap
    is listed. ``deployment_sequence`` is computed from incomplete evidence
    and is reported for reference only.
    """
    gaps = " ".join(f"({index}) {issue}" for index, issue in enumerate(issues, 1))
    return {
        "risk_score": DISCOVERY_GAP_RISK_SCORE,
        "approved": False,
        "justification": (
            "Rejected without LLM risk assessment because discovery evidence "
            f"is incomplete. Discovery gaps: {gaps} Resolve these gaps and "
            "re-run discovery before re-assessing; the deployment sequence "
            "shown is computed from incomplete evidence."
        ),
    }


def dependency_cycle_rejection(plan: DeploymentPlan) -> dict[str, object]:
    """Return the deterministic verdict for a release subgraph with a cycle.

    No deployment order satisfies every dependency edge, so the release is
    rejected at maximum risk without consulting the LLM.
    """
    return {
        "risk_score": DISCOVERY_GAP_RISK_SCORE,
        "approved": False,
        "justification": (
            "Rejected without LLM risk assessment because the release "
            "subgraph has a dependency cycle, so no deployment order "
            f"satisfies every dependency for a {plan.change_type.value} "
            "change. Services in or blocked by the cycle: "
            f"{', '.join(plan.cyclic_services)}. The deployment sequence "
            "shown breaks the cycle arbitrarily and needs human review."
        ),
    }


def assess_risk_and_sequence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Ask the LLM reasoner to score and justify the deterministic sequence.

    The deployment sequence comes from ``sequence_deployment_node`` and is
    never changed here. The Anthropic API is not called when
    ``discovery_issues`` is non-empty (``discovery_gap_rejection``) or the
    subgraph has a dependency cycle (``dependency_cycle_rejection``).
    """
    plan: DeploymentPlan = state["deployment_plan"]
    issues = list(state.get("discovery_issues", []))
    if issues:
        return discovery_gap_rejection(issues, plan.sequence)
    if not plan.is_acyclic:
        return dependency_cycle_rejection(plan)

    request = RiskAssessmentRequest(
        pull_request=state["pull_request"],
        change_type=plan.change_type,
        target_services=list(state.get("target_services", [])),
        upstream_services=list(state.get("upstream_services", [])),
        downstream_services=list(state.get("downstream_services", [])),
        deployment_sequence=plan.sequence,
        matched_edges=list(state.get("matched_edges", [])),
        telemetry=state.get("telemetry_status")
        or TelemetryStatus(source="knowledge_graph"),
    )
    assessment = RiskReasoner().assess(request)
    return {
        "risk_score": assessment.risk_score,
        "approved": assessment.approved,
        "justification": assessment.justification,
    }


def build_release_assessment(state: ReleaseGraphState) -> ReleaseAssessment:
    """Return the typed verdict the control plane parses from the run."""
    return ReleaseAssessment(
        risk_score=state["risk_score"],
        approved=state["approved"],
        deployment_sequence=state["deployment_sequence"],
        justification=state["justification"],
    )


def compile_evidence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Emit the typed verdict and render the evidence as a Markdown report.

    ``release_assessment`` is the machine-readable output for the control
    plane; ``evidence_report`` is the human-readable summary.
    """
    pull_request = state.get("pull_request")
    pr_text = (
        f"{pull_request.pr_id}: {pull_request.title}" if pull_request else "Unknown"
    )

    def _list(services: list[str]) -> str:
        return ", ".join(services) or "None identified"

    sequence = state.get("deployment_sequence", [])
    snapshot = state.get("baseline_snapshot_id") or "None"
    if state.get("baseline_demo_mode"):
        snapshot += " (DEMO MODE: relies on mock fixture data)"
    evidence_report = (
        "## Release Impact Assessment\n\n"
        f"**Pull Request:** {pr_text}\n\n"
        f"**Baseline Snapshot:** {snapshot}\n\n"
        f"**Target Services:** {_list(state.get('target_services', []))}\n\n"
        "**Blast Radius:**\n"
        "- **Upstream (depend on the target, directly or transitively):** "
        f"{_list(state.get('upstream_services', []))}\n"
        "- **Downstream (called by the target, directly or transitively):** "
        f"{_list(state.get('downstream_services', []))}\n\n"
        f"**Risk Score:** {state.get('risk_score', 0.0):.2f}\n\n"
        f"**Approved:** {'Yes' if state.get('approved') else 'No'}\n\n"
        f"**Discovery Gaps:** {_list(state.get('discovery_issues', []))}\n\n"
        f"**Recommended Sequence:** {' -> '.join(sequence) or 'Not determined'}\n\n"
        f"**Justification:** {state.get('justification') or 'Not provided'}"
    )
    return {
        "evidence_report": evidence_report,
        "release_assessment": build_release_assessment(state),
    }
