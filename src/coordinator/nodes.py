"""Node functions for the release orchestration graph."""

import json
import logging
from pathlib import Path

from src.coordinator.state import ReleaseGraphState
from src.core import config
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    PullRequestPayload,
    TelemetryStatus,
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
    """Return a dependency-safe order: providers first, consumers last.

    Downstream services (called by the target) go first so the target's new
    code finds compatible dependencies, then the target, then upstream
    consumers that may need to adopt the target's changes.
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
    """
    empty: dict[str, object] = {
        "upstream_services": [],
        "downstream_services": [],
        "matched_edges": [],
    }
    try:
        raw_graph = json.loads(KNOWLEDGE_GRAPH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "Knowledge graph not found at %s; returning no static dependents.",
            KNOWLEDGE_GRAPH_PATH,
        )
        return empty

    edges = [DependencyEdge.model_validate(edge) for edge in raw_graph.get("edges", [])]
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
    }


def validate_runtime_telemetry_node(state: ReleaseGraphState) -> dict[str, object]:
    """Gather runtime evidence for the services in the release radius.

    With a blank ``workspace_id`` only the knowledge graph is used: runtime
    evidence is the matched edges that carry RUNTIME_TELEMETRY. Otherwise the
    telemetry workspace is queried live and its metrics are filtered to the
    services in the radius.
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
    extractor.get_active_dependencies(days_back=7)
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
    return {"telemetry_status": status}


def assess_risk_and_sequence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Ask the LLM reasoner for a risk score, deployment order and rationale."""
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
    return {
        "risk_score": assessment.risk_score,
        "deployment_sequence": assessment.deployment_order,
        "justification": assessment.justification,
    }


def compile_evidence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Render the graph evidence and the LLM verdict as a Markdown report."""
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
        f"**Recommended Sequence:** {' -> '.join(sequence) or 'Not determined'}\n\n"
        f"**Justification:** {state.get('justification') or 'Not provided'}"
    )
    return {"evidence_report": evidence_report}
