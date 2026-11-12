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

    Environment and cluster are not known to static or namespace-scoped
    extraction, so they are ``unknown``. The single scanned namespace, if
    exactly one was configured, is the default for bare service names.
    """
    namespaces = list(state.get("target_namespaces") or [])
    return IdentityContext(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        default_namespace=namespaces[0] if len(namespaces) == 1 else None,
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
    per-service ``env:`` reference that reconciliation keeps unresolved.
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
    Services are registered under a FQURI whose environment, cluster and
    namespace are ``unknown``, with their repository and repository-relative
    root directory in metadata (used to map PR file changes to services). Endpoint URLs are reduced to their hostnames
    and environment variables to ``env:<service>/<VARIABLE>`` before being
    stored as dependency targets; both ends are resolved against the
    inventory later, during reconciliation.
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
    context = identity_context(state)
    static_edges = list(state.get("static_edges", []))
    discovered_services = list(state.get("discovered_services", []))
    registered = {service.service_id for service in discovered_services}
    statuses: list[DiscoveryStatus] = []
    errors: list[str] = []
    edge_count = 0

    for repo_name in state["target_repositories"]:
        extractor.get_pipeline_variables(repo_name)
        result = extractor.scan_repository(repo_name)
        statuses.append(result.status)
        errors.extend(result.errors)
        for scan in result.items:
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
    context = identity_context(state)
    discovered_services = list(state.get("discovered_services", []))
    statuses: list[DiscoveryStatus] = []
    errors: list[str] = []
    service_count = 0
    for namespace in state["target_namespaces"]:
        result = extractor.get_namespace_services(namespace, context)
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


def _resolve_endpoint(
    reference: str,
    index: IdentityIndex,
    context: IdentityContext,
    unresolved: dict[str, UnresolvedEntity],
) -> str:
    """Return the node ID an edge endpoint points at.

    References that resolve to exactly one registered service become that
    service's FQURI; anything else is recorded in ``unresolved`` and its
    unresolved-entity ID is returned.
    """
    identity = resolve_reference(reference, index, context)
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
    service) becomes an ``UnresolvedEntity`` node. Duplicate relationships
    are merged and self-referencing edges are dropped.
    """
    context = identity_context(state)
    services = _canonicalize_services(
        list(state.get("discovered_services", [])), context
    )
    index = IdentityIndex(service.identity for service in services)

    unresolved: dict[str, UnresolvedEntity] = {}
    merged_edges: dict[tuple[str, str], DependencyEdge] = {}
    for edge in [
        *state.get("static_edges", []),
        *state.get("runtime_edges", []),
    ]:
        source = _resolve_endpoint(edge.source_service_id, index, context, unresolved)
        target = _resolve_endpoint(edge.target_service_id, index, context, unresolved)
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


def persist_to_graph_db(state: DiscoveryGraphState) -> dict[str, object]:
    """Persist the reconciled Day-Zero graph as a new versioned snapshot.

    The reconciled services, dependency edges, unresolved entities and
    per-stage discovery reports are written to ``baselines/{tenant}/{project_id}/{snapshot_id}.json``.
    Every run writes its own immutable file, so concurrent runs cannot
    overwrite each other and the Release Coordinator reads the newest
    complete snapshot for its project.

    Returns:
        ``baseline_snapshot_id`` naming the snapshot this run wrote.

    Raises:
        DiscoveryIncompleteError: if any stage FAILED, was PARTIAL, or did not
            report. Nothing is written, so an incomplete crawl never becomes
            the project's latest baseline.
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
        }
    )
    scope = BaselineScope(
        tenant=state.get("tenant") or config.DEFAULT_TENANT,
        project_id=state.get("project_id") or config.DEFAULT_PROJECT_ID,
    )
    stored = BASELINE_STORE.write(scope, graph)
    return {"baseline_snapshot_id": stored.snapshot_id}
