"""State schema for the Day-Zero discovery workflow."""

from typing import TypedDict

from src.core.models import DependencyEdge, DiscoveryStageReport, ServiceNode


class DiscoveryGraphState(TypedDict):
    """Data passed between nodes in the discovery engine graph.

    ``discovery_reports`` holds one report per extraction stage, including
    SKIPPED stages, so persistence can tell a complete baseline from one
    built on failed or partial extraction. ``tenant`` and ``project_id`` pick
    the baseline directory the run writes to; ``baseline_snapshot_id`` is
    set by persistence to the snapshot the run wrote.
    """

    tenant: str
    project_id: str
    workspace_id: str
    devops_org_url: str
    devops_project: str
    target_repositories: list[str]
    target_namespaces: list[str]
    discovered_services: list[ServiceNode]
    static_edges: list[DependencyEdge]
    runtime_edges: list[DependencyEdge]
    discovery_reports: list[DiscoveryStageReport]
    reconciled_graph: dict[str, object]
    baseline_snapshot_id: str
