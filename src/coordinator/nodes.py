"""Node functions for the release orchestration graph."""

import json
import logging
from pathlib import Path

from src.coordinator.state import ReleaseGraphState
from src.core import config
from src.core.models import DependencyEdge
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor

logger = logging.getLogger(__name__)

KNOWLEDGE_GRAPH_PATH = Path(__file__).resolve().parents[2] / config.KNOWLEDGE_GRAPH_PATH


def extract_trigger_node(state: ReleaseGraphState) -> dict[str, object]:
    """Extract the target services from the triggering release event."""
    return {"target_services": state.get("target_services") or ["payment-service"]}


def calculate_static_radius_node(state: ReleaseGraphState) -> dict[str, object]:
    """Calculate the static dependency radius of the target services.

    Edges persisted by the discovery engine in ``knowledge_graph.json`` are
    scanned for any relationship touching a target service; the service on
    the other end of each such edge is a static dependent.
    """
    try:
        raw_graph = json.loads(KNOWLEDGE_GRAPH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "Knowledge graph not found at %s; returning no static dependents.",
            KNOWLEDGE_GRAPH_PATH,
        )
        return {"static_dependents": []}

    edges = [DependencyEdge.model_validate(edge) for edge in raw_graph.get("edges", [])]
    targets = set(state.get("target_services", []))
    static_dependents: list[str] = []
    for edge in edges:
        connected: list[str] = []
        if edge.source_service_id in targets:
            connected.append(edge.target_service_id)
        if edge.target_service_id in targets:
            connected.append(edge.source_service_id)
        for service_id in connected:
            if service_id not in targets and service_id not in static_dependents:
                static_dependents.append(service_id)
    return {"static_dependents": static_dependents}


def validate_runtime_telemetry_node(state: ReleaseGraphState) -> dict[str, object]:
    """Validate runtime telemetry for dependencies in the release radius."""
    extractor = TelemetryExtractor()
    dependencies = extractor.get_active_dependencies(days_back=7)
    active_sources = {edge.source_service_id for edge in dependencies}
    runtime_metrics = {
        service: metrics
        for service, metrics in extractor.get_runtime_metrics().items()
        if service in active_sources
    }
    return {"runtime_metrics": runtime_metrics}


def assess_risk_and_sequence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Assess release risk and determine a safe deployment sequence."""
    runtime_metrics = state.get("runtime_metrics", {})
    has_errors = any(
        isinstance(metrics, dict)
        and isinstance(metrics.get("error_rate"), (int, float))
        and metrics["error_rate"] > 0.0
        for metrics in runtime_metrics.values()
    )
    targets = state.get("target_services", [])
    deployment_sequence = [
        service
        for service in state.get("static_dependents", [])
        if service not in targets
    ] + targets
    return {
        "risk_score": 0.8 if has_errors else 0.2,
        "deployment_sequence": deployment_sequence,
    }


def compile_evidence_node(state: ReleaseGraphState) -> dict[str, object]:
    """Compile the dependency and risk results into an evidence report."""
    targets = state.get("target_services", [])
    dependents = state.get("static_dependents", [])
    deployment_sequence = state.get("deployment_sequence", [])
    target_text = ", ".join(targets) or "None identified"
    dependent_text = ", ".join(dependents) or "None identified"
    sequence_text = (
        " -> ".join(deployment_sequence)
        if deployment_sequence
        else "Not determined"
    )
    evidence_report = (
        "## Release Impact Assessment\n\n"
        f"**Target Services:** {target_text}\n\n"
        "**Upstream / Downstream Impacts:**\n"
        "- **Upstream:** Not identified by the current dependency evidence.\n"
        f"- **Downstream:** {dependent_text}\n\n"
        f"**Risk Score:** {state.get('risk_score', 0.0):.1f}\n\n"
        f"**Recommended Sequence:** {sequence_text}"
    )
    return {"evidence_report": evidence_report}
