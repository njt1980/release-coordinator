"""LangGraph workflow for release orchestration."""

from langgraph.graph import StateGraph

from src.coordinator.nodes import (
    assess_risk_and_sequence_node,
    calculate_static_radius_node,
    check_change_flags_node,
    check_contracts_node,
    compile_evidence_node,
    extract_trigger_node,
    load_policy_node,
    load_review_node,
    sequence_deployment_node,
    validate_runtime_telemetry_node,
    verify_revision_node,
)
from src.coordinator.state import ReleaseGraphState


workflow = StateGraph(ReleaseGraphState)
workflow.add_node("load_policy_node", load_policy_node)
workflow.add_node("extract_trigger_node", extract_trigger_node)
workflow.add_node("verify_revision_node", verify_revision_node)
workflow.add_node("load_review_node", load_review_node)
workflow.add_node("calculate_static_radius_node", calculate_static_radius_node)
workflow.add_node("validate_runtime_telemetry_node", validate_runtime_telemetry_node)
workflow.add_node("check_contracts_node", check_contracts_node)
workflow.add_node("check_change_flags_node", check_change_flags_node)
workflow.add_node("sequence_deployment_node", sequence_deployment_node)
workflow.add_node("assess_risk_and_sequence_node", assess_risk_and_sequence_node)
workflow.add_node("compile_evidence_node", compile_evidence_node)

workflow.set_entry_point("load_policy_node")
workflow.add_edge("load_policy_node", "extract_trigger_node")
workflow.add_edge("extract_trigger_node", "verify_revision_node")
workflow.add_edge("verify_revision_node", "load_review_node")
workflow.add_edge("load_review_node", "calculate_static_radius_node")
workflow.add_edge("calculate_static_radius_node", "validate_runtime_telemetry_node")
workflow.add_edge("validate_runtime_telemetry_node", "check_contracts_node")
workflow.add_edge("check_contracts_node", "check_change_flags_node")
workflow.add_edge("check_change_flags_node", "sequence_deployment_node")
workflow.add_edge("sequence_deployment_node", "assess_risk_and_sequence_node")
workflow.add_edge("assess_risk_and_sequence_node", "compile_evidence_node")
workflow.set_finish_point("compile_evidence_node")

app = workflow.compile()
