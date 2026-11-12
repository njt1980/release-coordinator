"""Batch-processing nodes for the Day-Zero discovery workflow."""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from src.core import config
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    KnowledgeGraph,
    ServiceNode,
    baseline_problems,
    combine_statuses,
)
from src.core.service_ids import build_name_index, canonical_service_id
from src.discovery_engine.state import DiscoveryGraphState
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor

KNOWLEDGE_GRAPH_PATH = Path(__file__).resolve().parents[2] / config.KNOWLEDGE_GRAPH_PATH


class DiscoveryIncompleteError(RuntimeError):
    """Raised when discovery evidence is too incomplete to become a baseline."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            "Baseline not established; discovery is incomplete: "
            + "; ".join(problems)
        )
        self.problems = problems


def _append_report(
    state: DiscoveryGraphState, report: DiscoveryStageReport
) -> list[DiscoveryStageReport]:
    """Return the state's stage reports with ``report`` added."""
    return [*state.get("discovery_reports", []), report]


def _skipped(state: DiscoveryGraphState, source: DiscoveryMethod) -> dict[str, object]:
    report = DiscoveryStageReport(source=source, status=DiscoveryStatus.SKIPPED)
    return {"discovery_reports": _append_report(state, report)}


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

    The Day-Zero static pass inspects the scoped repositories to identify
    microservices and code-declared dependencies. Each microservice directory
    that references an internal endpoint is registered as a service node, and
    its edges originate from that microservice, not from the repository.
    Endpoint URLs are reduced to their hostnames before being stored as
    dependency targets; IDs are made canonical later during reconciliation.
    The stage is skipped when no Azure DevOps organization URL or project is
    configured. A repository that cannot be scanned makes the stage PARTIAL
    (or FAILED if no repository could be scanned) instead of silently
    contributing no edges.
    """
    if not state.get("devops_org_url") or not state.get("devops_project"):
        return _skipped(state, DiscoveryMethod.STATIC_CODE)

    extractor = DevOpsExtractor(
        org_url=state["devops_org_url"],
        project=state["devops_project"],
        repositories=list(state["target_repositories"]),
    )
    static_edges = list(state.get("static_edges", []))
    discovered_services = list(state.get("discovered_services", []))
    registered = {service.service_id for service in discovered_services}
    statuses: list[DiscoveryStatus] = []
    errors: list[str] = []
    edge_count = 0

    for repo_name in state["target_repositories"]:
        extractor.get_pipeline_variables(repo_name)
        result = extractor.scan_repository_for_endpoints(repo_name)
        statuses.append(result.status)
        errors.extend(result.errors)
        for reference in result.items:
            target_service_id = urlsplit(reference.endpoint).hostname
            if not target_service_id:
                continue
            if reference.service_name not in registered:
                registered.add(reference.service_name)
                discovered_services.append(
                    ServiceNode(
                        service_id=reference.service_name,
                        name=reference.service_name,
                        metadata={
                            "source": DiscoveryMethod.STATIC_CODE.value,
                            "devops_project": state["devops_project"],
                            "repository": repo_name,
                        },
                    )
                )
            static_edges.append(
                DependencyEdge(
                    source_service_id=reference.service_name,
                    target_service_id=target_service_id,
                    discovery_methods=[DiscoveryMethod.STATIC_CODE],
                    confidence_score=0.7,
                    last_observed_at=datetime.now(timezone.utc),
                )
            )
            edge_count += 1

    report = DiscoveryStageReport(
        source=DiscoveryMethod.STATIC_CODE,
        # Configured DevOps scope with no repositories still ran: nothing to scan.
        status=combine_statuses(statuses) if statuses else DiscoveryStatus.SUCCESS,
        item_count=edge_count,
        errors=errors,
    )
    return {
        "static_edges": static_edges,
        "discovered_services": discovered_services,
        "discovery_reports": _append_report(state, report),
    }


def extract_k8s_topology(state: DiscoveryGraphState) -> dict[str, object]:
    """Enrich the service baseline with Kubernetes deployment topology.

    During the initial deep dive, this stage will inspect manifests and
    deployment configuration in the selected namespaces to find deployed
    services and topology-based relationships. The stage is skipped when no
    namespaces are configured.
    """
    if not state.get("target_namespaces"):
        return _skipped(state, DiscoveryMethod.K8S_MANIFEST)

    extractor = KubernetesExtractor()
    discovered_services = list(state.get("discovered_services", []))
    statuses: list[DiscoveryStatus] = []
    errors: list[str] = []
    service_count = 0
    for namespace in state["target_namespaces"]:
        result = extractor.get_namespace_services(namespace)
        statuses.append(result.status)
        errors.extend(result.errors)
        discovered_services.extend(result.items)
        service_count += len(result.items)
    report = DiscoveryStageReport(
        source=DiscoveryMethod.K8S_MANIFEST,
        status=combine_statuses(statuses),
        item_count=service_count,
        errors=errors,
    )
    return {
        "discovered_services": discovered_services,
        "discovery_reports": _append_report(state, report),
    }


def extract_runtime_telemetry(state: DiscoveryGraphState) -> dict[str, object]:
    """Collect observed service-to-service relationships from runtime data.

    This Day-Zero batch stage will establish a runtime dependency baseline and
    place its evidence-backed relationships in ``runtime_edges``. The stage is
    skipped when no telemetry workspace ID is configured.
    """
    workspace_id = (state.get("workspace_id") or "").strip()
    if not workspace_id:
        return _skipped(state, DiscoveryMethod.RUNTIME_TELEMETRY)

    extractor = TelemetryExtractor(workspace_id=workspace_id)
    result = extractor.get_active_dependencies(days_back=30)
    report = DiscoveryStageReport(
        source=DiscoveryMethod.RUNTIME_TELEMETRY,
        status=result.status,
        item_count=len(result.items),
        errors=result.errors,
    )
    return {
        "runtime_edges": list(result.items),
        "discovery_reports": _append_report(state, report),
    }


def _canonicalize_services(
    services: list[ServiceNode],
    name_index: dict[str, str],
    default_namespace: str | None,
) -> list[ServiceNode]:
    """Rename services to canonical IDs and merge duplicates by ID.

    A repository-derived service (``order-service``) and its Kubernetes
    counterpart (``default/order-service``) collapse into a single node whose
    metadata combines both sources.
    """
    merged: dict[str, ServiceNode] = {}
    for service in services:
        service_id = canonical_service_id(
            service.service_id, name_index, default_namespace
        )
        existing = merged.get(service_id)
        if existing is None:
            merged[service_id] = service.model_copy(
                update={"service_id": service_id}
            )
            continue
        merged[service_id] = existing.model_copy(
            update={
                "repository_url": existing.repository_url or service.repository_url,
                "metadata": {**service.metadata, **existing.metadata},
            }
        )
    return list(merged.values())


def _merge_edge(existing_edge: DependencyEdge, edge: DependencyEdge) -> DependencyEdge:
    """Combine the evidence of two edges between the same services."""
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
    return DependencyEdge(
        source_service_id=existing_edge.source_service_id,
        target_service_id=existing_edge.target_service_id,
        discovery_methods=discovery_methods,
        confidence_score=max(existing_edge.confidence_score, edge.confidence_score),
        last_observed_at=max(existing_observed_at, edge_observed_at),
    )


def reconcile_knowledge_graph(state: DiscoveryGraphState) -> dict[str, object]:
    """Reconcile static, Kubernetes, and runtime evidence into one graph.

    Service and edge endpoints are rewritten to the canonical
    ``<namespace>/<name>`` form (see ``src.core.service_ids``) so that edges
    join to service nodes. Bare names resolve against the discovered
    Kubernetes services, falling back to the scanned namespace when exactly
    one was configured. Duplicate services and relationships are merged and
    self-referencing edges are dropped.
    """
    services = list(state.get("discovered_services", []))
    namespaces = list(state.get("target_namespaces") or [])
    default_namespace = namespaces[0] if len(namespaces) == 1 else None
    name_index = build_name_index(service.service_id for service in services)

    merged_edges: dict[tuple[str, str], DependencyEdge] = {}
    for edge in [
        *state.get("static_edges", []),
        *state.get("runtime_edges", []),
    ]:
        source = canonical_service_id(
            edge.source_service_id, name_index, default_namespace
        )
        target = canonical_service_id(
            edge.target_service_id, name_index, default_namespace
        )
        if source == target:
            continue
        edge = edge.model_copy(
            update={"source_service_id": source, "target_service_id": target}
        )
        edge_key = (source, target)
        existing_edge = merged_edges.get(edge_key)
        merged_edges[edge_key] = (
            edge if existing_edge is None else _merge_edge(existing_edge, edge)
        )

    # Zombie service identification will be added here.
    return {
        "reconciled_graph": {
            "services": _canonicalize_services(
                services, name_index, default_namespace
            ),
            "edges": list(merged_edges.values()),
        }
    }


def persist_to_graph_db(state: DiscoveryGraphState) -> dict[str, object]:
    """Persist the reconciled Day-Zero graph to the local JSON repository.

    The reconciled services, dependency edges and per-stage discovery reports
    are serialized from their Pydantic models and written to
    ``knowledge_graph.json`` in the project root, where the Release
    Coordinator reads them back.

    Raises:
        DiscoveryIncompleteError: if any stage FAILED, was PARTIAL, or did not
            report. The existing knowledge graph is left untouched, so an
            incomplete crawl never replaces the baseline.
    """
    reports = list(state.get("discovery_reports", []))
    problems = baseline_problems(reports)
    if problems:
        raise DiscoveryIncompleteError(problems)

    reconciled_graph = state.get("reconciled_graph") or {}
    services: list[ServiceNode] = list(reconciled_graph.get("services", []))
    edges: list[DependencyEdge] = list(reconciled_graph.get("edges", []))
    graph = KnowledgeGraph(services=services, edges=edges, discovery=reports)
    KNOWLEDGE_GRAPH_PATH.write_text(
        json.dumps(graph.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return {}
