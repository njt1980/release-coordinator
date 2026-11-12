"""Run the release coordination graph with local mock telemetry."""

from src.coordinator.graph import app
from src.coordinator.state import ReleaseGraphState


if __name__ == "__main__":
    initial_state: ReleaseGraphState = {
        "trigger_event": "PR-1024",
        "workspace_id": "local-mock-workspace",
        "target_services": ["checkout-service"],
        "static_dependents": [],
        "runtime_metrics": {},
        "risk_score": 0.0,
        "deployment_sequence": [],
        "evidence_report": "",
    }
    final_state = app.invoke(initial_state)
    print(final_state["evidence_report"])
