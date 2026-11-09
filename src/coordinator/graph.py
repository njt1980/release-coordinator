"""LangGraph workflow for release orchestration."""

from langgraph.graph import StateGraph

from src.coordinator.nodes import (
    assess_risk_and_sequence_node,
    calculate_static_radius_node,
    compile_evidence_node,
    extract_trigger_node,
    validate_runtime_telemetry_node,
)
from src.coordinator.state import ReleaseGraphState


workflow = StateGraph(ReleaseGraphState)
workflow.add_node("extract_trigger_node", extract_trigger_node)
workflow.add_node("calculate_static_radius_node", calculate_static_radius_node)
workflow.add_node("validate_runtime_telemetry_node", validate_runtime_telemetry_node)
workflow.add_node("assess_risk_and_sequence_node", assess_risk_and_sequence_node)
workflow.add_node("compile_evidence_node", compile_evidence_node)

workflow.set_entry_point("extract_trigger_node")
workflow.add_edge("extract_trigger_node", "calculate_static_radius_node")
workflow.add_edge("calculate_static_radius_node", "validate_runtime_telemetry_node")
workflow.add_edge("validate_runtime_telemetry_node", "assess_risk_and_sequence_node")
workflow.add_edge("assess_risk_and_sequence_node", "compile_evidence_node")
workflow.set_finish_point("compile_evidence_node")

app = workflow.compile()
