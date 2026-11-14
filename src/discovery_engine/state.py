"""State schema for the Day-Zero discovery workflow."""

from typing import TypedDict

from src.core.models import (
    DependencyEdge,
    DiscoveryStageReport,
    PipelineVariables,
    RepositoryCoverage,
    ScannedRevision,
    ShapeFinding,
    ServiceEnvironment,
    ServiceNode,
    ServiceStaticConfig,
    TelemetryScope,
)
from src.core.telemetry_roles import TelemetryRoleFilter


class DiscoveryGraphState(TypedDict):
    """Data passed between nodes in the discovery engine graph.

    ``discovery_reports`` holds one report per extraction stage, including
    SKIPPED stages, so persistence can tell a complete baseline from one
    built on failed or partial extraction. ``tenant`` and ``project_id`` pick
    the baseline directory the run writes to; ``baseline_snapshot_id`` is
    set by persistence to the snapshot the run wrote. ``service_environments``
    holds the container environment variables Kubernetes sets per service,
    used to resolve ``env:`` dependency references during reconciliation.
    ``environment`` and ``cluster`` are the deployment target being scanned;
    every service FQURI carries them. ``scanned_revisions`` records the
    commit each repository was read at. ``kubernetes_api_server`` is the API
    server the manifest scan read and ``telemetry_scope`` the scope (cluster
    filter, Application Insights components) the telemetry query applied;
    both prove the ``cluster`` label. ``telemetry_additional_workspaces``
    are the workspaces read with ``workspace_id``.
    ``kubernetes_workload_names`` and ``kubernetes_pod_names`` are what the
    manifest scan read (pod names None when some namespace's pods could not
    be listed); ``telemetry_role_filter`` is the opt-in role filter the
    telemetry query applied (see ``core.telemetry_roles``).
    ``static_coverage`` and ``pipeline_variables`` are recorded per scanned
    repository, ``kubernetes_shape`` per out-of-shape namespace object, and
    ``manifest_edges`` are dependencies the manifests declare (an
    ExternalName Service's alias to its host). ``static_config`` holds what
    each scanned service's source declares as configuration defaults
    (image ``ENV``, framework configuration files, defaults in code), used
    when the runtime environment provably doesn't set a variable.
    """

    tenant: str
    project_id: str
    environment: str
    cluster: str
    workspace_id: str
    devops_org_url: str
    devops_project: str
    target_repositories: list[str]
    target_namespaces: list[str]
    discovered_services: list[ServiceNode]
    service_environments: list[ServiceEnvironment]
    static_edges: list[DependencyEdge]
    runtime_edges: list[DependencyEdge]
    discovery_reports: list[DiscoveryStageReport]
    scanned_revisions: list[ScannedRevision]
    reconciled_graph: dict[str, object]
    baseline_snapshot_id: str
    kubernetes_api_server: str
    telemetry_scope: TelemetryScope | None
    telemetry_additional_workspaces: list[str]
    kubernetes_workload_names: list[str]
    kubernetes_pod_names: list[str] | None
    telemetry_role_filter: TelemetryRoleFilter | None
    static_coverage: list[RepositoryCoverage]
    pipeline_variables: list[PipelineVariables]
    kubernetes_shape: list[ShapeFinding]
    manifest_edges: list[DependencyEdge]
    static_config: list[ServiceStaticConfig]
