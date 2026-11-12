"""Node functions for the release orchestration graph."""

import json
import logging
from pathlib import Path

from src.coordinator.state import ReleaseGraphState
from src.core import config
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    PullRequestPayload,
    ReleaseAssessment,
    TelemetryStatus,
    baseline_problems,
)
from src.tools.risk_reasoner import RiskAssessmentRequest, RiskReasoner
from src.tools.telemetry_extractor import TelemetryExtractor

logger = logging.getLogger(__name__)

KNOWLEDGE_GRAPH_PATH = Path(__file__).resolve().parents[2] / config.KNOWLEDGE_GRAPH_PATH


def _service_name(service_id: str) -> str:
    """Return the bare service name of a ``<namespace>/<name>`` ID."""
    return service_id.rsplit("/", 1)[-1]


def baseline_deployment_order(
    targets: list[str], upstream: list[str], downstream: list[str]
) -> list[str]:
    """Return the order for an additive change: providers first, consumers last.

    Downstream services (called by the target) go first so the target's new
    code finds compatible dependencies, then the target, then upstream
    consumers that may start using the target's new capability. For
    tightening or breaking contract changes the consumers must go before the
    target; the risk reasoner is instructed to reorder in that case.
    """
    order: list[str] = []
    for service in [*downstream, *targets, *upstream]:
        if service not in order:
            order.append(service)
    return order


def extract_trigger_node(state: ReleaseGraphState) -> dict[str, object]:
    """Validate the pull-request payload and resolve the target services."""
    pull_request = PullRequestPayload.model_validate(state["trigger_event"])
    targets = list(state.get("target_services") or [pull_request.target_service])
    return {"pull_request": pull_request, "target_services": targets}


def calculate_static_radius_node(state: ReleaseGraphState) -> dict[str, object]:
    """Split the target's graph neighbours into upstream and downstream.

    An edge ``A -> B`` in ``knowledge_graph.json`` means A depends on (calls)
    B. For a target T, ``A -> T`` makes A upstream (a consumer of T) and
    ``T -> B`` makes B downstream (a dependency of T).

    A missing graph, or one whose discovery was not recorded as complete, is
    reported in ``discovery_issues`` so the release cannot be approved on an
    empty or partial radius.
    """
    try:
        raw_graph = json.loads(KNOWLEDGE_GRAPH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "Knowledge graph not found at %s; returning no static dependents.",
            KNOWLEDGE_GRAPH_PATH,
        )
        return {
            "upstream_services": [],
            "downstream_services": [],
            "matched_edges": [],
            "discovery_issues": [
                "Knowledge graph not found; no discovery baseline exists."
            ],
        }

    graph = KnowledgeGraph.model_validate(raw_graph)
    if graph.discovery is None:
        issues = [
            "Knowledge graph does not record discovery outcomes; re-run discovery."
        ]
    else:
        issues = [
            f"Discovery baseline is incomplete: {problem}"
            for problem in baseline_problems(graph.discovery)
        ]
    edges = graph.edges
    targets = set(state.get("target_services", []))
    upstream: list[str] = []
    downstream: list[str] = []
    matched: list[DependencyEdge] = []
    for edge in edges:
        source, target = edge.source_service_id, edge.target_service_id
        if source not in targets and target not in targets:
            continue
        matched.append(edge)
        if target in targets and source not in targets and source not in upstream:
            upstream.append(source)
        if source in targets and target not in targets and target not in downstream:
            downstream.append(target)
    return {
        "upstream_services": upstream,
        "downstream_services": downstream,
        "matched_edges": matched,
        "discovery_issues": issues,
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


def assess_risk_and_sequence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Ask the LLM reasoner for a risk score, deployment order and rationale.

    The LLM's approval is overridden to False whenever ``discovery_issues``
    is non-empty: a verdict over incomplete dependency evidence is not safe
    to act on, however low the reasoner scores the risk.
    """
    targets = list(state.get("target_services", []))
    upstream = list(state.get("upstream_services", []))
    downstream = list(state.get("downstream_services", []))
    request = RiskAssessmentRequest(
        pull_request=state["pull_request"],
        target_services=targets,
        upstream_services=upstream,
        downstream_services=downstream,
        baseline_deployment_order=baseline_deployment_order(
            targets, upstream, downstream
        ),
        matched_edges=list(state.get("matched_edges", [])),
        telemetry=state.get("telemetry_status")
        or TelemetryStatus(source="knowledge_graph"),
    )
    assessment = RiskReasoner().assess(request)
    issues = list(state.get("discovery_issues", []))
    justification = assessment.justification
    if issues:
        justification += (
            " Approval withheld because discovery evidence is incomplete: "
            + " ".join(issues)
        )
    return {
        "risk_score": assessment.risk_score,
        "approved": assessment.approved and not issues,
        "deployment_sequence": assessment.deployment_order,
        "justification": justification,
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
    evidence_report = (
        "## Release Impact Assessment\n\n"
        f"**Pull Request:** {pr_text}\n\n"
        f"**Target Services:** {_list(state.get('target_services', []))}\n\n"
        "**Blast Radius:**\n"
        "- **Upstream (depend on the target):** "
        f"{_list(state.get('upstream_services', []))}\n"
        "- **Downstream (called by the target):** "
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
