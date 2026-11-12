"""Batch-processing nodes for the Day-Zero discovery workflow."""

from datetime import datetime, timezone
from urllib.parse import urlsplit

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
    ServiceEnvironment,
    ServiceIdentity,
    ServiceNode,
    UnresolvedEntity,
    baseline_problems,
    combine_statuses,
)
from src.core.service_ids import (
    IdentityContext,
    IdentityIndex,
    env_var_reference,
    resolve_env_var_reference,
    resolve_reference,
    unresolved_entity,
)
from src.discovery_engine.state import DiscoveryGraphState
from src.tools.baseline_store import BaselineScope, default_store
from src.tools.devops_extractor import (
    DevOpsExtractor,
    EndpointReference,
    ReferenceKind,
)
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor

__all__ = ["DiscoveryIncompleteError"]

BASELINE_STORE = default_store()
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


def _static_target(reference: EndpointReference) -> str | None:
    """Return the edge target for a static reference.

    URLs are reduced to their hostname; environment variables become a
    per-service ``env:`` reference that reconciliation resolves through the
    variable's Kubernetes-configured value, when one is known.
    """
    if reference.kind is ReferenceKind.ENV_VAR:
        return env_var_reference(reference.service_name, reference.endpoint)
    return urlsplit(reference.endpoint).hostname or None


def extract_static_code_baseline(state: DiscoveryGraphState) -> dict[str, object]:
    """Build the initial service inventory and static dependency baseline.

    The Day-Zero static pass inspects the scoped repositories to identify
    microservices and code-declared dependencies. Every service the
    extractor returns is registered as a service node (services declared in
    ``release-coord.yaml`` are returned even without dependencies), and its
    edges originate from that microservice, not from the repository.
    Services are registered under a FQURI carrying the onboarded
    environment and cluster and an ``unknown`` namespace, with their repository and repository-relative
    root directory in metadata (used to map PR file changes to services). Endpoint URLs are reduced to their hostnames
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
    registered = {service.service_id for service in discovered_services}
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
            source_id = context.identity(scan.service.name).uri
            if source_id not in registered:
                registered.add(source_id)
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
                target_service_id = _static_target(reference)
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


def extract_k8s_topology(state: DiscoveryGraphState) -> dict[str, object]:
    """Enrich the service baseline with Kubernetes deployment topology.

    During the initial deep dive, this stage will inspect manifests and
    deployment configuration in the selected namespaces to find deployed
    services and topology-based relationships. It also records the container
    environment variables each service's workloads receive
    (``service_environments``), which reconciliation uses to resolve
    ``env:`` references. A namespace whose services or workload environments
    could not be fully read makes the stage PARTIAL. The stage is skipped
    when no namespaces are configured.
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


def _service_identity(
    service: ServiceNode, index: IdentityIndex, context: IdentityContext
) -> ServiceIdentity:
    """Return the identity ``service`` is merged under.

    A service whose namespace is unknown (registered from source code) gets
    the default namespace, if any, and merges into the single namespaced
    service it matches; otherwise it keeps its own identity.
    """
    identity = service.identity
    resolved = index.resolve(identity)
    if resolved is None and identity.namespace == UNKNOWN_LABEL:
        if context.default_namespace:
            identity = identity.model_copy(
                update={"namespace": context.default_namespace}
            )
        resolved = index.resolve(identity)
    return resolved or identity


def _canonicalize_services(
    services: list[ServiceNode], context: IdentityContext
) -> list[ServiceNode]:
    """Resolve services to their merged identities and merge duplicates.

    A repository-derived service (``...:unknown:order-service``) and its
    Kubernetes counterpart (``...:default:order-service``) collapse into a
    single node whose metadata combines both sources.
    """
    namespaced = IdentityIndex(
        service.identity
        for service in services
        if service.identity.namespace != UNKNOWN_LABEL
    )
    merged: dict[str, ServiceNode] = {}
    for service in services:
        service_id = _service_identity(service, namespaced, context).uri
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


def _environment_index(
    environments: list[ServiceEnvironment],
) -> dict[str, dict[str, str]]:
    """Map service FQURIs to their Kubernetes environment variables."""
    index: dict[str, dict[str, str]] = {}
    for environment in environments:
        index.setdefault(environment.service_id, {}).update(environment.variables)
    return index


def _resolve_endpoint(
    reference: str,
    index: IdentityIndex,
    context: IdentityContext,
    environments: dict[str, dict[str, str]],
    unresolved: dict[str, UnresolvedEntity],
) -> str:
    """Return the node ID an edge endpoint points at.

    References that resolve to exactly one registered service become that
    service's FQURI, including ``env:`` references whose Kubernetes value
    names one. Anything else is recorded in ``unresolved`` and its
    unresolved-entity ID is returned.
    """
    identity = resolve_reference(
        reference, index, context
    ) or resolve_env_var_reference(reference, environments, index, context)
    if identity is not None:
        return identity.uri
    entity = unresolved_entity(reference)
    unresolved.setdefault(entity.entity_id, entity)
    return entity.entity_id


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
    stays visible on an edge that real evidence also supports.
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
    )


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
    are merged and self-referencing edges are dropped.
    """
    context = identity_context(state)
    services = _canonicalize_services(
        list(state.get("discovered_services", [])), context
    )
    index = IdentityIndex(service.identity for service in services)
    environments = _environment_index(list(state.get("service_environments", [])))

    unresolved: dict[str, UnresolvedEntity] = {}
    merged_edges: dict[tuple[str, str], DependencyEdge] = {}
    for edge in [
        *state.get("static_edges", []),
        *state.get("runtime_edges", []),
    ]:
        source = _resolve_endpoint(
            edge.source_service_id, index, context, environments, unresolved
        )
        target = _resolve_endpoint(
            edge.target_service_id, index, context, environments, unresolved
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
            "edges": list(merged_edges.values()),
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
        }
    )
    scope = BaselineScope(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        project_id=state.get("project_id") or config.DEFAULT_PROJECT_ID,
    )
    stored = BASELINE_STORE.write(scope, graph)
    return {"baseline_snapshot_id": stored.snapshot_id}
