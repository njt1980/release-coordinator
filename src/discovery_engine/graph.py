"""LangGraph workflow for the Day-Zero discovery batch."""

from langgraph.graph import StateGraph

from src.discovery_engine.nodes import (
    extract_k8s_topology,
    extract_runtime_telemetry,
    extract_static_code_baseline,
    initialize_scan_scope,
    persist_to_graph_db,
    reconcile_knowledge_graph,
)
from src.discovery_engine.state import DiscoveryGraphState


workflow = StateGraph(DiscoveryGraphState)
workflow.add_node("initialize_scan_scope", initialize_scan_scope)
workflow.add_node("extract_static_code_baseline", extract_static_code_baseline)
workflow.add_node("extract_k8s_topology", extract_k8s_topology)
workflow.add_node("extract_runtime_telemetry", extract_runtime_telemetry)
workflow.add_node("reconcile_knowledge_graph", reconcile_knowledge_graph)
workflow.add_node("persist_to_graph_db", persist_to_graph_db)

workflow.set_entry_point("initialize_scan_scope")
workflow.add_edge("initialize_scan_scope", "extract_static_code_baseline")
workflow.add_edge("extract_static_code_baseline", "extract_k8s_topology")
workflow.add_edge("extract_k8s_topology", "extract_runtime_telemetry")
workflow.add_edge("extract_runtime_telemetry", "reconcile_knowledge_graph")
workflow.add_edge("reconcile_knowledge_graph", "persist_to_graph_db")
workflow.set_finish_point("persist_to_graph_db")

discovery_app = workflow.compile()
