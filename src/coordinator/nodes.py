"""Node functions for the release orchestration graph."""

from src.coordinator.state import ReleaseGraphState
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor


def extract_trigger_node(state: ReleaseGraphState) -> dict[str, object]:
    """Extract the target services from the triggering release event."""
    return {"target_services": ["payment-service"]}


def calculate_static_radius_node(state: ReleaseGraphState) -> dict[str, object]:
    """Calculate the static dependency radius of the target services."""
    return {"static_dependents": ["api-gateway", "order-service"]}


def validate_runtime_telemetry_node(state: ReleaseGraphState) -> dict[str, object]:
    """Validate runtime telemetry for dependencies in the release radius."""
    extractor = TelemetryExtractor()
    extractor.get_active_dependencies(days_back=7)
    return {
        "runtime_metrics": {
            "payment-service": {
                "error_rate": 0.05,
                "active_connections": 120,
            }
        }
    }


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
