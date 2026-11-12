"""Batch-processing nodes for the Day-Zero discovery workflow."""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from src.core import config
from src.core.models import DependencyEdge, DiscoveryMethod, ServiceNode
from src.discovery_engine.state import DiscoveryGraphState
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor

KNOWLEDGE_GRAPH_PATH = Path(__file__).resolve().parents[2] / config.KNOWLEDGE_GRAPH_PATH


def initialize_scan_scope(state: DiscoveryGraphState) -> dict[str, object]:
    """Establish the repository and Kubernetes namespace scope for the initial
    ecosystem-wide deep dive.

    Scope supplied in the incoming state (for example, from the onboarding
    API) is returned unchanged. An empty scope stays empty, so the extraction
    stages for unconfigured integrations are skipped.
    """
    return {
        "target_repositories": list(state.get("target_repositories") or []),
        "target_namespaces": list(state.get("target_namespaces") or []),
    }


def extract_static_code_baseline(state: DiscoveryGraphState) -> dict[str, object]:
    """Build the initial service inventory and static dependency baseline.

    The Day-Zero static pass will inspect the scoped repositories to identify
    microservices and code-declared dependencies. Endpoint URLs are reduced to
    their hostnames before being stored as dependency targets. The stage is
    skipped when no Azure DevOps organization URL or project is configured.
    """
    if not state.get("devops_org_url") or not state.get("devops_project"):
        return {}

    extractor = DevOpsExtractor(
        org_url=state["devops_org_url"],
        project=state["devops_project"],
        repositories=list(state["target_repositories"]),
    )
    static_edges = list(state.get("static_edges", []))

    for repo_name in state["target_repositories"]:
        extractor.get_pipeline_variables(repo_name)
        endpoints = extractor.scan_repository_for_endpoints(repo_name)
        for endpoint in endpoints:
            try:
                parsed_endpoint = urlsplit(endpoint)
                target_service_id = parsed_endpoint.hostname
            except ValueError:
                continue

            if (
                parsed_endpoint.scheme.lower() not in {"http", "https"}
                or not target_service_id
            ):
                continue

            static_edges.append(
                DependencyEdge(
                    source_service_id=repo_name,
                    target_service_id=target_service_id,
                    discovery_methods=[DiscoveryMethod.STATIC_CODE],
                    confidence_score=0.7,
                    last_observed_at=datetime.now(timezone.utc),
                )
            )

    return {"static_edges": static_edges}


def extract_k8s_topology(state: DiscoveryGraphState) -> dict[str, object]:
    """Enrich the service baseline with Kubernetes deployment topology.

    During the initial deep dive, this stage will inspect manifests and
    deployment configuration in the selected namespaces to find deployed
    services and topology-based relationships. The stage is skipped when no
    namespaces are configured.
    """
    if not state.get("target_namespaces"):
        return {}

    extractor = KubernetesExtractor()
    discovered_services = list(state.get("discovered_services", []))
    for namespace in state["target_namespaces"]:
        discovered_services.extend(extractor.get_namespace_services(namespace))
    return {"discovered_services": discovered_services}


def extract_runtime_telemetry(state: DiscoveryGraphState) -> dict[str, object]:
    """Collect observed service-to-service relationships from runtime data.

    This Day-Zero batch stage will establish a runtime dependency baseline and
    place its evidence-backed relationships in ``runtime_edges``. The stage is
    skipped when no telemetry workspace ID is configured.
    """
    workspace_id = (state.get("workspace_id") or "").strip()
    if not workspace_id:
        return {}

    extractor = TelemetryExtractor(workspace_id=workspace_id)
    runtime_edges = extractor.get_active_dependencies(days_back=30)
    return {"runtime_edges": runtime_edges}


def reconcile_knowledge_graph(state: DiscoveryGraphState) -> dict[str, object]:
    """Reconcile static, Kubernetes, and runtime evidence into one graph.

    This stage combines discovered services and dependency evidence, merging
    duplicate relationships into ``reconciled_graph`` for downstream
    persistence.
    """
    merged_edges: dict[tuple[str, str], DependencyEdge] = {}
    for edge in [
        *state.get("static_edges", []),
        *state.get("runtime_edges", []),
    ]:
        edge_key = (edge.source_service_id, edge.target_service_id)
        existing_edge = merged_edges.get(edge_key)
        if existing_edge is None:
            merged_edges[edge_key] = edge
            continue

        discovery_methods = list(existing_edge.discovery_methods)
        for method in edge.discovery_methods:
            if method not in discovery_methods:
                discovery_methods.append(method)
        existing_observed_at = existing_edge.last_observed_at
        edge_observed_at = edge.last_observed_at
        if existing_observed_at.utcoffset() is None:
            existing_observed_at = existing_observed_at.replace(tzinfo=timezone.utc)
        if edge_observed_at.utcoffset() is None:
            edge_observed_at = edge_observed_at.replace(tzinfo=timezone.utc)
        merged_edges[edge_key] = DependencyEdge(
            source_service_id=edge.source_service_id,
            target_service_id=edge.target_service_id,
            discovery_methods=discovery_methods,
            confidence_score=max(
                existing_edge.confidence_score, edge.confidence_score
            ),
            last_observed_at=max(existing_observed_at, edge_observed_at),
        )

    # Zombie service identification will be added here.
    return {
        "reconciled_graph": {
            "services": list(state.get("discovered_services", [])),
            "edges": list(merged_edges.values()),
        }
    }


def persist_to_graph_db(state: DiscoveryGraphState) -> dict[str, object]:
    """Persist the reconciled Day-Zero graph to the local JSON repository.

    The reconciled services and dependency edges are serialized from their
    Pydantic models and written to ``knowledge_graph.json`` in the project
    root, where the Release Coordinator reads them back.
    """
    reconciled_graph = state.get("reconciled_graph") or {}
    services: list[ServiceNode] = list(reconciled_graph.get("services", []))
    edges: list[DependencyEdge] = list(reconciled_graph.get("edges", []))
    payload = {
        "services": [service.model_dump(mode="json") for service in services],
        "edges": [edge.model_dump(mode="json") for edge in edges],
    }
    KNOWLEDGE_GRAPH_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {}
