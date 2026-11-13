"""Batch-processing nodes for the Day-Zero discovery workflow."""

import logging
from datetime import datetime, timezone

from src.core import config
from src.core.models import (
    LABEL_SEPARATOR,
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    UNKNOWN_LABEL,
    DependencyEdge,
    DiscoveryIncompleteError,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    KnowledgeGraph,
    ScanContext,
    ScannedRevision,
    ServiceIdentity,
    ServiceNode,
    UnresolvedEntity,
    UnresolvedEntityKind,
    baseline_problems,
    combine_statuses,
    unresolved_entity_id,
)
from src.core.edge_resolution import (
    resolve_endpoint,
    service_environment_index,
    target_context,
)
from src.core.service_ids import IdentityContext, IdentityIndex
from src.discovery_engine.state import DiscoveryGraphState
from src.tools.baseline_store import BaselineScope, default_store
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.policy_store import (
    PolicyInvalidError,
    PolicyMissingError,
    ProjectPolicy,
    default_policy_store,
)
from src.tools.telemetry_extractor import TelemetryExtractor

__all__ = ["DiscoveryIncompleteError"]

logger = logging.getLogger(__name__)
BASELINE_STORE = default_store()
POLICY_STORE = default_policy_store()
# Onboarding without a usable policy keeps the original 30-day window; such
# a baseline can't be approved anyway (POLICY_MISSING or a scope mismatch).
NO_POLICY_LOOKBACK_DAYS = 30
STATIC_CODE_PROVENANCE = "azure_devops"


def _append_report(
    state: DiscoveryGraphState, report: DiscoveryStageReport
) -> list[DiscoveryStageReport]:
    """Return the state's stage reports with ``report`` added."""
    return [*state.get("discovery_reports", []), report]


def identity_context(state: DiscoveryGraphState) -> IdentityContext:
    """Return the identity segments known for this discovery run.

    Environment and cluster come from onboarding (``unknown`` when not
    given). The single scanned namespace, if exactly one was configured, is
    the default for bare service names.
    """
    return IdentityContext.for_scan(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        namespaces=list(state.get("target_namespaces") or []),
        environment=state.get("environment") or UNKNOWN_LABEL,
        cluster=state.get("cluster") or UNKNOWN_LABEL,
    )


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
    microservices and code-declared dependencies. Every service the
    extractor returns is registered as a service node (services declared in
    ``release-coord.yaml`` are returned even without dependencies), and its
    edges originate from that microservice, not from the repository.
    Services are registered under a FQURI carrying the onboarded
    environment and cluster and the namespace declared in
    ``release-coord.yaml`` (``unknown`` when none is declared), with their
    repository and repository-relative root directory in metadata (used to
    map PR file changes to services). Every repository directory gets its
    own node, even when two share a FQURI: reconciliation decides whether
    they are one service or an ambiguity, never this stage. Endpoint URLs are reduced to their hostnames
    and environment variables to ``env:<service>/<VARIABLE>`` before being
    stored as dependency targets; both ends are resolved against the
    inventory later, during reconciliation.
    The stage is skipped when no Azure DevOps organization URL or project is
    configured, and FAILED when they are configured but no repositories are
    selected. A repository that cannot be scanned makes the stage PARTIAL
    (or FAILED if no repository could be scanned) instead of silently
    contributing no edges. Each scanned repository's pinned commit is added
    to ``scanned_revisions``.
    """
    if not state.get("devops_org_url") or not state.get("devops_project"):
        return _skipped(state, DiscoveryMethod.STATIC_CODE)

    extractor = DevOpsExtractor(
        org_url=state["devops_org_url"],
        project=state["devops_project"],
        repositories=list(state["target_repositories"]),
    )
    context = identity_context(state)
    static_edges = list(state.get("static_edges", []))
    discovered_services = list(state.get("discovered_services", []))
    registered = {_static_key(service) for service in discovered_services}
    scanned_revisions: list[ScannedRevision] = list(state.get("scanned_revisions", []))
    statuses: list[DiscoveryStatus] = []
    errors: list[str] = []
    edge_count = 0

    for repo_name in state["target_repositories"]:
        extractor.get_pipeline_variables(repo_name)
        result = extractor.scan_repository(repo_name)
        statuses.append(result.status)
        errors.extend(result.errors)
        scans = []
        for repository_scan in result.items:
            scanned_revisions.append(repository_scan.revision)
            scans.extend(repository_scan.services)
        for scan in scans:
            source_id = context.identity(scan.service.name, scan.service.namespace).uri
            key = (source_id, repo_name, scan.service.root)
            if key not in registered:
                registered.add(key)
                discovered_services.append(
                    ServiceNode(
                        service_id=source_id,
                        name=scan.service.name,
                        metadata={
                            "source": DiscoveryMethod.STATIC_CODE.value,
                            "devops_project": state["devops_project"],
                            REPOSITORY_METADATA_KEY: repo_name,
                            SERVICE_ROOT_METADATA_KEY: scan.service.root,
                        },
                    )
                )
            for reference in scan.references:
                target_service_id = reference.dependency_target()
                if not target_service_id:
                    continue
                static_edges.append(
                    DependencyEdge(
                        source_service_id=source_id,
                        target_service_id=target_service_id,
                        discovery_methods=[DiscoveryMethod.STATIC_CODE],
                        confidence_score=0.7,
                        last_observed_at=datetime.now(timezone.utc),
                        provenance=STATIC_CODE_PROVENANCE,
                    )
                )
                edge_count += 1

    if not statuses:
        # Repository discovery was requested, but nothing was selected to
        # scan: an empty result would look like a codebase with no services.
        statuses.append(DiscoveryStatus.FAILED)
        errors.append(
            "Azure DevOps is configured but no repositories were selected to scan"
        )
    report = DiscoveryStageReport(
        source=DiscoveryMethod.STATIC_CODE,
        status=combine_statuses(statuses),
        item_count=edge_count,
        errors=errors,
    )
    return {
        "static_edges": static_edges,
        "discovered_services": discovered_services,
        "scanned_revisions": scanned_revisions,
        "discovery_reports": _append_report(state, report),
    }


def _static_key(service: ServiceNode) -> tuple[str, str | None, str | None]:
    """Identify a node by its FQURI and the repository directory it came from."""
    return (
        service.service_id,
        service.metadata.get(REPOSITORY_METADATA_KEY),
        service.metadata.get(SERVICE_ROOT_METADATA_KEY),
    )


def extract_k8s_topology(state: DiscoveryGraphState) -> dict[str, object]:
    """Enrich the service baseline with Kubernetes deployment topology.

    During the initial deep dive, this stage will inspect manifests and
    deployment configuration in the selected namespaces to find deployed
    services and topology-based relationships. It also records the container
    environment variables each service's workloads receive
    (``service_environments``), which reconciliation uses to resolve
    ``env:`` references. A namespace whose services or workload environments
    could not be fully read makes the stage PARTIAL. The stage is skipped
    when no namespaces are configured. The API server actually read is
    recorded (``kubernetes_api_server``) so the baseline proves which
    cluster it describes.
    """
    if not state.get("target_namespaces"):
        return _skipped(state, DiscoveryMethod.K8S_MANIFEST)

    extractor = KubernetesExtractor()
    context = identity_context(state)
    discovered_services = list(state.get("discovered_services", []))
    service_environments = list(state.get("service_environments", []))
    statuses: list[DiscoveryStatus] = []
    errors: list[str] = []
    service_count = 0
    for namespace in state["target_namespaces"]:
        result = extractor.get_namespace_services(namespace, context)
        statuses.append(result.status)
        errors.extend(result.errors)
        discovered_services.extend(result.items)
        service_count += len(result.items)
        env_result = extractor.get_namespace_environments(namespace, context)
        statuses.append(env_result.status)
        errors.extend(env_result.errors)
        service_environments.extend(env_result.items)
    report = DiscoveryStageReport(
        source=DiscoveryMethod.K8S_MANIFEST,
        status=combine_statuses(statuses),
        item_count=service_count,
        errors=errors,
    )
    return {
        "discovered_services": discovered_services,
        "service_environments": service_environments,
        "kubernetes_api_server": extractor.api_server,
        "discovery_reports": _append_report(state, report),
    }


def extract_runtime_telemetry(state: DiscoveryGraphState) -> dict[str, object]:
    """Collect observed service-to-service relationships from runtime data.

    This Day-Zero batch stage will establish a runtime dependency baseline and
    place its evidence-backed relationships in ``runtime_edges``. The stage is
    skipped when no telemetry workspace ID is configured. With a valid
    project policy, the query is restricted to its ``telemetry_cluster``
    and reads ``runtime_lookback_days`` back; the filter applied is
    recorded (``telemetry_cluster``) and checked against the policy at
    assessment. Without one, it is unfiltered and reads
    ``NO_POLICY_LOOKBACK_DAYS`` back. Rows dropped as noise are counted in
    the stage report.
    """
    workspace_id = (state.get("workspace_id") or "").strip()
    if not workspace_id:
        return _skipped(state, DiscoveryMethod.RUNTIME_TELEMETRY)

    policy = _project_policy(state)
    cluster_filter = policy.telemetry_cluster if policy else None
    extractor = TelemetryExtractor(workspace_id=workspace_id)
    result = extractor.get_active_dependencies(
        days_back=policy.runtime_lookback_days if policy else NO_POLICY_LOOKBACK_DAYS,
        cluster_filter=cluster_filter,
    )
    report = DiscoveryStageReport(
        source=DiscoveryMethod.RUNTIME_TELEMETRY,
        status=result.status,
        item_count=len(result.items),
        errors=result.errors,
        dropped_records=extractor.dropped_records,
    )
    return {
        "runtime_edges": list(result.items),
        "telemetry_cluster": cluster_filter,
        "discovery_reports": _append_report(state, report),
    }


def _project_policy(state: DiscoveryGraphState) -> ProjectPolicy | None:
    """Return the project's valid policy, or None.

    Without a usable policy telemetry is unfiltered; the snapshot then
    records no filter, which a policy that requires one rejects.
    """
    scope = BaselineScope(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        project_id=state.get("project_id") or config.DEFAULT_PROJECT_ID,
    )
    try:
        return POLICY_STORE.load(scope).policy
    except (PolicyMissingError, PolicyInvalidError) as error:
        logger.warning("Telemetry is not scoped to a cluster: %s", error)
        return None


def _service_identity(
    service: ServiceNode, index: IdentityIndex, context: IdentityContext
) -> ServiceIdentity | None:
    """Return the identity ``service`` is merged under, or None if ambiguous.

    A service whose namespace is unknown (registered from source code
    without a declared namespace) gets the default namespace, if any, and
    merges into the single namespaced service it matches. Without a default
    namespace, a name matching services in several namespaces is ambiguous:
    it is never attached to one of them by guessing. Otherwise the service
    keeps its own identity.
    """
    identity = service.identity
    if identity.namespace != UNKNOWN_LABEL:
        return index.resolve(identity) or identity
    if context.default_namespace:
        identity = identity.model_copy(update={"namespace": context.default_namespace})
    matches = index.candidates(identity)
    if len(matches) > 1:
        return None
    return matches[0] if matches else identity


def _ambiguous_entity(reference: str) -> UnresolvedEntity:
    reference = reference.strip().lower()
    return UnresolvedEntity(
        entity_id=unresolved_entity_id(reference),
        reference=reference,
        kind=UnresolvedEntityKind.AMBIGUOUS_SERVICE,
    )


def _is_static(service: ServiceNode) -> bool:
    return REPOSITORY_METADATA_KEY in service.metadata


def _canonicalize_services(
    services: list[ServiceNode], context: IdentityContext
) -> tuple[list[ServiceNode], dict[str, UnresolvedEntity]]:
    """Resolve services to their merged identities and merge duplicates.

    A repository-derived service (``...:unknown:order-service``) and its
    Kubernetes counterpart (``...:default:order-service``) collapse into a
    single node whose metadata combines both sources.

    Source-code services that cannot be tied to exactly one identity are
    ambiguous: two repository directories claiming the same identity, or a
    name matching services in several namespaces. They are left out of the
    inventory (their files then map to no service) and returned as
    AMBIGUOUS_SERVICE entities keyed by their original service ID, so
    reconciliation can attribute their code edges to the entity. A
    Kubernetes service with the same identity stays registered.

    Returns:
        The merged services, and the ambiguous entity for each original
        service ID of an ambiguous source-code service.
    """
    namespaced = IdentityIndex(
        service.identity
        for service in services
        if service.identity.namespace != UNKNOWN_LABEL
    )
    groups: dict[str, list[ServiceNode]] = {}
    ambiguous: dict[str, UnresolvedEntity] = {}
    for service in services:
        identity = _service_identity(service, namespaced, context)
        if identity is None:
            ambiguous[service.service_id] = _ambiguous_entity(service.service_id)
            continue
        groups.setdefault(identity.uri, []).append(service)

    merged: list[ServiceNode] = []
    for service_id, group in groups.items():
        directories = {_static_key(s)[1:] for s in group if _is_static(s)}
        if len(directories) > 1:
            entity = _ambiguous_entity(service_id)
            for service in group:
                if _is_static(service):
                    ambiguous[service.service_id] = entity
            group = [s for s in group if not _is_static(s)]
            if not group:
                continue
        node = group[0].model_copy(update={"service_id": service_id})
        for service in group[1:]:
            node = node.model_copy(
                update={
                    "repository_url": node.repository_url or service.repository_url,
                    "metadata": {**service.metadata, **node.metadata},
                }
            )
        merged.append(node)
    return merged, ambiguous


def _merge_labels(first: str, second: str) -> str:
    """Union two comma-separated label sets, keeping first-seen order."""
    labels: list[str] = []
    for label in [*first.split(LABEL_SEPARATOR), *second.split(LABEL_SEPARATOR)]:
        if label not in labels:
            labels.append(label)
    return LABEL_SEPARATOR.join(labels)


def _merge_edge(existing_edge: DependencyEdge, edge: DependencyEdge) -> DependencyEdge:
    """Combine the evidence of two edges between the same services.

    Provenance and environment labels are unioned so mock fixture evidence
    stays visible on an edge that real evidence also supports. The merged
    edge's direction is uncertain only if both edges' directions are.
    """
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
        provenance=_merge_labels(existing_edge.provenance, edge.provenance),
        environment=_merge_labels(existing_edge.environment, edge.environment),
        dependency_type=existing_edge.dependency_type or edge.dependency_type,
        direction_uncertain=existing_edge.direction_uncertain and edge.direction_uncertain,
    )


def drop_reversed_sightings(
    edges: dict[tuple[str, str], DependencyEdge],
) -> list[DependencyEdge]:
    """Return the edges without uncertain sightings of a known edge reversed.

    An edge whose direction is uncertain (see ``DependencyEdge``) is a
    reply reported in reverse when the opposite edge is known with a
    certain direction; it is then dropped. Any other uncertain edge is
    kept, still marked uncertain, so the dependency is not lost.
    """
    return [
        edge
        for (source, target), edge in edges.items()
        if not (
            edge.direction_uncertain
            and (reverse := edges.get((target, source))) is not None
            and not reverse.direction_uncertain
        )
    ]


def reconcile_knowledge_graph(state: DiscoveryGraphState) -> dict[str, object]:
    """Reconcile static, Kubernetes, and runtime evidence into one graph.

    Services are merged under their FQURIs (see ``src.core.service_ids``).
    Each edge endpoint is then resolved against that inventory: a reference
    matching exactly one registered service becomes its FQURI, and any other
    endpoint (an IP address, a private or external DNS name, or an unknown
    service) becomes an ``UnresolvedEntity`` node. An ``env:`` reference is
    resolved through the variable's value in ``service_environments``; if
    it cannot be mapped to exactly one registered service it stays an
    ENVIRONMENT_VARIABLE entity, so release gating still sees it. Duplicate relationships
    are merged and self-referencing edges are dropped, as are uncertain
    telemetry sightings of a known edge reversed (see
    ``drop_reversed_sightings``).

    A target's bare name resolves in its source's namespace, as in-cluster
    DNS does. Code edges of an ambiguous source-code service (see
    ``_canonicalize_services``) start from its AMBIGUOUS_SERVICE entity.
    """
    context = identity_context(state)
    services, ambiguous = _canonicalize_services(
        list(state.get("discovered_services", [])), context
    )
    index = IdentityIndex(service.identity for service in services)
    environments = service_environment_index(
        list(state.get("service_environments", []))
    )

    unresolved: dict[str, UnresolvedEntity] = {
        entity.entity_id: entity for entity in ambiguous.values()
    }
    merged_edges: dict[tuple[str, str], DependencyEdge] = {}
    static_edges = list(state.get("static_edges", []))
    for position, edge in enumerate([*static_edges, *state.get("runtime_edges", [])]):
        entity = (
            ambiguous.get(edge.source_service_id)
            if position < len(static_edges)
            else None
        )
        source = entity.entity_id if entity else resolve_endpoint(
            edge.source_service_id, index, context, environments, unresolved
        )
        target = resolve_endpoint(
            edge.target_service_id,
            index,
            target_context(source, context),
            environments,
            unresolved,
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
            "services": services,
            "edges": drop_reversed_sightings(merged_edges),
            "unresolved_entities": list(unresolved.values()),
        }
    }


def scan_context(state: DiscoveryGraphState) -> ScanContext:
    """Return what this run scanned, to be recorded in its snapshot.

    Raises:
        pydantic.ValidationError: if the run's environment or cluster is
            missing or ``unknown``; such a run cannot become a baseline.
    """
    return ScanContext(
        environment=state.get("environment") or UNKNOWN_LABEL,
        cluster=state.get("cluster") or UNKNOWN_LABEL,
        namespaces=list(state.get("target_namespaces") or []),
        telemetry_workspace_id=(state.get("workspace_id") or "").strip(),
        scanned_revisions=list(state.get("scanned_revisions", [])),
        kubernetes_api_server=state.get("kubernetes_api_server") or "",
        telemetry_cluster=state.get("telemetry_cluster"),
    )


def persist_to_graph_db(state: DiscoveryGraphState) -> dict[str, object]:
    """Persist the reconciled Day-Zero graph as a new versioned snapshot.

    The reconciled services, dependency edges, unresolved entities,
    per-stage discovery reports and the scan context (environment, cluster,
    namespaces, telemetry workspace and scanned revisions) are written to ``baselines/{tenant}/{project_id}/{snapshot_id}.json``.
    Every run writes its own immutable file, so concurrent runs cannot
    overwrite each other and the Release Coordinator reads the newest
    complete snapshot for its project.

    Returns:
        ``baseline_snapshot_id`` naming the snapshot this run wrote.

    Raises:
        DiscoveryIncompleteError: if any stage FAILED, was PARTIAL, or did not
            report. Nothing is written, so an incomplete crawl never becomes
            the project's latest baseline.
        ValueError: if the scan context is incomplete (environment or
            cluster unknown, a service outside them, or a repository with no
            scanned revision); nothing is written.
    """
    reports = list(state.get("discovery_reports", []))
    problems = baseline_problems(reports)
    if problems:
        raise DiscoveryIncompleteError(problems)

    reconciled_graph = state.get("reconciled_graph") or {}
    graph = KnowledgeGraph.model_validate(
        {
            "services": reconciled_graph.get("services", []),
            "edges": reconciled_graph.get("edges", []),
            "unresolved_entities": reconciled_graph.get("unresolved_entities", []),
            "discovery": reports,
            "scan": scan_context(state),
            # Only endpoint values, stripped of credentials, are stored.
            "service_environments": [
                environment.for_storage()
                for environment in state.get("service_environments", [])
            ],
        }
    )
    scope = BaselineScope(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        project_id=state.get("project_id") or config.DEFAULT_PROJECT_ID,
    )
    stored = BASELINE_STORE.write(scope, graph)
    return {"baseline_snapshot_id": stored.snapshot_id}
