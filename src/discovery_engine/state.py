"""State schema for the Day-Zero discovery workflow."""

from typing import TypedDict

from src.core.models import DependencyEdge, ServiceNode


class DiscoveryGraphState(TypedDict):
    """Data passed between nodes in the discovery engine graph."""

    workspace_id: str
    devops_org_url: str
    devops_project: str
    target_repositories: list[str]
    target_namespaces: list[str]
    discovered_services: list[ServiceNode]
    static_edges: list[DependencyEdge]
    runtime_edges: list[DependencyEdge]
    reconciled_graph: dict[str, object]
