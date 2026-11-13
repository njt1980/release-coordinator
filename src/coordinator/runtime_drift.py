"""Compare live telemetry with the baseline the release is assessed against.

Pure functions: live edges are resolved against the baseline's service
inventory exactly as discovery resolves runtime edges, then every resolved
edge touching the release radius that the baseline does not have is drift.
Drift means the baseline no longer describes the running system, so the
release is held (BASELINE_DRIFT) until discovery is re-run.
"""

from collections.abc import Collection, Iterable
from datetime import datetime, timezone

from src.core.models import (
    DependencyEdge,
    EvidenceIssue,
    KnowledgeGraph,
    ObservedEdge,
    ReasonCode,
)
from src.core.service_ids import (
    IdentityContext,
    IdentityIndex,
    resolve_reference,
    unresolved_entity,
)


def baseline_identity_context(tenant: str, graph: KnowledgeGraph) -> IdentityContext:
    """Return the identity context the baseline's discovery run used.

    Snapshots without a recorded scan context were built with unknown
    environment, cluster and default namespace, so those are used.
    """
    scan = graph.scan
    if scan is None:
        return IdentityContext.for_scan(tenant=tenant, namespaces=[])
    return IdentityContext.for_scan(
        tenant=tenant,
        namespaces=list(scan.namespaces),
        environment=scan.environment,
        cluster=scan.cluster,
    )


def _utc(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC so observations can be compared."""
    return moment if moment.utcoffset() is not None else moment.replace(tzinfo=timezone.utc)


def _endpoint(reference: str, index: IdentityIndex, context: IdentityContext) -> str:
    identity = resolve_reference(reference, index, context)
    return identity.uri if identity else unresolved_entity(reference).entity_id


def resolve_live_edges(
    graph: KnowledgeGraph, context: IdentityContext, edges: Iterable[DependencyEdge]
) -> list[ObservedEdge]:
    """Resolve raw live edges to baseline node IDs.

    An endpoint matching exactly one registered service becomes its FQURI;
    anything else becomes its unresolved entity ID. A target's bare name
    resolves in its source's namespace, as discovery and in-cluster DNS
    resolve it. Self-edges are dropped and duplicates merged, keeping the
    latest observation.
    """
    index = IdentityIndex(service.identity for service in graph.services)
    observed: dict[tuple[str, str], ObservedEdge] = {}
    for edge in edges:
        source_identity = resolve_reference(edge.source_service_id, index, context)
        source = (
            source_identity.uri
            if source_identity
            else unresolved_entity(edge.source_service_id).entity_id
        )
        target_context = (
            context.in_namespace(source_identity.namespace) if source_identity else context
        )
        target = _endpoint(edge.target_service_id, index, target_context)
        if source == target:
            continue
        seen_at = _utc(edge.last_observed_at)
        existing = observed.get((source, target))
        if existing is None or seen_at > existing.last_observed_at:
            observed[(source, target)] = ObservedEdge(
                source_id=source, target_id=target, last_observed_at=seen_at
            )
    return list(observed.values())


def unbaselined_edges(
    graph: KnowledgeGraph,
    radius: Collection[str],
    observed: Iterable[ObservedEdge],
    exempt_entities: Collection[str],
) -> list[ObservedEdge]:
    """Return observed edges touching ``radius`` that the baseline lacks.

    An edge counts as known when the baseline has an edge with the same
    source and target from any discovery method. Edges with an endpoint the
    project policy exempts (an exempted unresolved entity) are not drift.
    """
    known = {(edge.source_service_id, edge.target_service_id) for edge in graph.edges}
    return [
        edge
        for edge in observed
        if (edge.source_id in radius or edge.target_id in radius)
        and (edge.source_id, edge.target_id) not in known
        and edge.source_id not in exempt_entities
        and edge.target_id not in exempt_entities
    ]


def drift_issue(snapshot_id: str, edges: list[ObservedEdge]) -> EvidenceIssue | None:
    """Return a BASELINE_DRIFT issue naming ``edges``, if there are any."""
    if not edges:
        return None
    listed = ", ".join(f"{edge.source_id} -> {edge.target_id}" for edge in edges)
    return EvidenceIssue(
        code=ReasonCode.BASELINE_DRIFT,
        detail=(
            f"Live telemetry observed {len(edges)} dependency edge(s) in the "
            f"release radius that baseline snapshot {snapshot_id} does not have: "
            f"{listed}. Re-run discovery."
        ),
    )


def workspace_mismatch_issue(
    graph: KnowledgeGraph, workspace_id: str
) -> EvidenceIssue | None:
    """Return a DISCOVERY_GAP issue unless ``workspace_id`` is the telemetry
    workspace the baseline was built from.

    Live edges from another workspace (possibly another environment) cannot
    be compared with the baseline, in either direction. A baseline that
    records no scan context or queried no workspace cannot be matched.
    """
    recorded = graph.scan.telemetry_workspace_id if graph.scan else ""
    if recorded == workspace_id:
        return None
    baseline = recorded or "none (or not recorded)"
    return EvidenceIssue(
        code=ReasonCode.DISCOVERY_GAP,
        detail=(
            f"Live telemetry was queried from workspace {workspace_id}, but the "
            f"baseline was built from workspace {baseline}; live edges cannot "
            "be compared with it."
        ),
    )

