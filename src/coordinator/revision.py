"""Verify a pull request's revisions and assess the topology its head ships.

Pure functions over source-control answers (``CommitComparison`` and the
head's ``RepositoryScan``); the coordinator node fetches them. Three checks
make the assessment describe the code that will actually ship:

1. The revisions are real and related: the base is an ancestor of the head,
   and the changed files the caller sent are exactly what the commits change.
2. The baseline describes the base: commits between the baseline's scanned
   revision and the PR's base must not change the services being assessed.
3. The head's own dependencies replace the baseline's static evidence for
   the changed services, so a dependency the PR adds is in the radius.
"""

from collections.abc import Collection, Iterable
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from src.coordinator.file_mapping import map_changed_files, normalize_changed_path
from src.core.edge_resolution import (
    pipeline_index,
    resolve_endpoint,
    service_environment_index,
    static_config_index,
    target_context,
)
from src.core.models import (
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    DependencyChange,
    DependencyEdge,
    DiscoveryMethod,
    EvidenceIssue,
    KnowledgeGraph,
    ReasonCode,
    ServiceChannelUse,
    ServiceStaticConfig,
    StaticDefaultUse,
    UnresolvedEntity,
)
from src.core.messaging import replace_service_uses
from src.core.service_ids import IdentityContext, IdentityIndex
from src.tools.devops_extractor import CommitComparison, ServiceScan

HEAD_SCAN_PROVENANCE = "pull_request_head"
# Confidence of a code-declared edge, as discovery records it.
_STATIC_CONFIDENCE = 0.7
_LISTED = 10


class RevisionLag(BaseModel):
    """Services changed between the baseline's scanned revision and the base.

    ``radius_services`` changed in commits the baseline hasn't seen (the
    base is ahead): any of them in the radius makes the baseline describe
    other code. ``target_services`` changed on the branch after the base
    (the baseline is ahead): the head scan lacks those changes, so they
    matter only for the services being changed.
    """

    radius_services: list[str] = Field(default_factory=list)
    target_services: list[str] = Field(default_factory=list)

    def issue(self, targets: Collection[str], radius: Collection[str]) -> EvidenceIssue | None:
        """Return BASELINE_REVISION_MISMATCH if the lag touches the release."""
        touched = sorted(
            {*self.radius_services}.intersection({*targets, *radius})
            | {*self.target_services}.intersection(targets)
        )
        if not touched:
            return None
        return _issue(
            ReasonCode.BASELINE_REVISION_MISMATCH,
            "Commits between the baseline's scanned revision and the pull "
            f"request's base change {_sample(touched)}; re-run discovery (or "
            "rebase the pull request) so the baseline describes the base.",
        )


class HeadTopology(BaseModel):
    """The graph a pull request's head ships, and how it differs.

    ``graph`` is the baseline with the changed services' code-declared
    edges replaced by the head's. ``added`` and ``removed`` list the
    code-declared dependencies that changed.
    """

    graph: KnowledgeGraph
    added: list[DependencyChange] = Field(default_factory=list)
    removed: list[DependencyChange] = Field(default_factory=list)


def _issue(code: ReasonCode, detail: str) -> EvidenceIssue:
    return EvidenceIssue(code=code, detail=detail)


def _sample(paths: Iterable[str]) -> str:
    listed = sorted(paths)
    shown = ", ".join(listed[:_LISTED])
    return shown + (f" (+{len(listed) - _LISTED} more)" if len(listed) > _LISTED else "")


def pull_request_issues(
    comparison: CommitComparison, changed_files: Collection[str]
) -> list[EvidenceIssue]:
    """Return why the PR's revisions can't be trusted, if they can't.

    The base must be an ancestor of the head, and the caller's changed
    files must be exactly the paths the commits change.
    """
    issues: list[EvidenceIssue] = []
    if not comparison.base_is_ancestor:
        issues.append(
            _issue(
                ReasonCode.REVISION_UNVERIFIED,
                f"Base {comparison.base} is not an ancestor of head "
                f"{comparison.target} in {comparison.repository}.",
            )
        )
    claimed = {normalize_changed_path(path) for path in changed_files}
    actual = set(comparison.changed_paths)
    if claimed != actual:
        missing, extra = actual - claimed, claimed - actual
        parts = []
        if missing:
            parts.append(f"changed by the commits but not listed: {_sample(missing)}")
        if extra:
            parts.append(f"listed but not changed by the commits: {_sample(extra)}")
        issues.append(
            _issue(
                ReasonCode.REVISION_UNVERIFIED,
                "The changed files don't match "
                f"{comparison.base[:12]}..{comparison.target[:12]}: " + "; ".join(parts) + ".",
            )
        )
    return issues


def changed_services(
    graph: KnowledgeGraph, repository: str, comparisons: Iterable[CommitComparison]
) -> set[str]:
    """Return the registered services whose files the comparisons change."""
    paths = [path for comparison in comparisons for path in comparison.changed_paths]
    if not paths:
        return set()
    return set(map_changed_files(graph, repository, paths).files_by_service)


def target_roots(graph: KnowledgeGraph, repository: str, targets: Iterable[str]) -> dict[str, str]:
    """Map each target's repository root to its FQURI, for ``repository``."""
    wanted = set(targets)
    return {
        service.metadata[SERVICE_ROOT_METADATA_KEY]: service.service_id
        for service in graph.services
        if service.service_id in wanted
        and str(service.metadata.get(REPOSITORY_METADATA_KEY, "")).lower() == repository.lower()
        and SERVICE_ROOT_METADATA_KEY in service.metadata
    }


def _is_static(edge: DependencyEdge) -> bool:
    return DiscoveryMethod.STATIC_CODE in edge.discovery_methods


def _without_static(edge: DependencyEdge) -> DependencyEdge | None:
    """Return the edge minus its code evidence, or None if that was all."""
    methods = [m for m in edge.discovery_methods if m is not DiscoveryMethod.STATIC_CODE]
    if not methods:
        return None
    files: list[str] | None = None if edge.evidence_files is None else []
    return edge.model_copy(update={"discovery_methods": methods, "evidence_files": files})


def head_topology(
    graph: KnowledgeGraph,
    context: IdentityContext,
    roots: dict[str, str],
    head_services: list[ServiceScan],
) -> HeadTopology:
    """Return the graph with each changed service's code edges read at head.

    ``roots`` maps the changed services' repository roots to their FQURIs;
    ``head_services`` is the head scan of those roots. Head references are
    resolved exactly as discovery resolves them, through the baseline's
    inventory and recorded container environments, with the changed
    services' configuration defaults read at the head. Edges with runtime or
    manifest evidence keep it even when the head no longer declares them.
    The changed services' message channel uses are replaced by the head's.
    """
    index = IdentityIndex(service.identity for service in graph.services)
    environments = service_environment_index(list(graph.service_environments or []))
    pipelines = pipeline_index(graph.services, graph.pipeline_variables)
    configs = static_config_index(graph.services, _head_configs(graph, head_services))
    defaults_used: dict[tuple[str, str], StaticDefaultUse] = {}
    unresolved: dict[str, UnresolvedEntity] = {
        entity.entity_id: entity for entity in graph.unresolved_entities
    }
    head_edges: dict[str, dict[str, set[str]]] = {source: {} for source in roots.values()}
    for scan in head_services:
        source = roots.get(scan.service.root)
        if source is None:
            continue
        for reference in scan.references:
            reference_target = reference.dependency_target()
            if not reference_target:
                continue
            target = resolve_endpoint(
                reference_target,
                index,
                target_context(source, context),
                environments,
                unresolved,
                pipelines,
                configs,
                defaults_used,
            )
            if target != source:
                head_edges[source].setdefault(target, set()).update(reference.evidence_files)

    sources = set(head_edges)
    baseline_static = {
        (edge.source_service_id, edge.target_service_id)
        for edge in graph.edges
        if edge.source_service_id in sources and _is_static(edge)
    }
    head_static = {(source, target) for source, targets in head_edges.items() for target in targets}
    added = sorted(head_static - baseline_static)
    removed = sorted(baseline_static - head_static)

    def head_files(source: str, target: str) -> list[str]:
        return sorted(head_edges[source][target])

    edges: list[DependencyEdge] = []
    for edge in graph.edges:
        pair = (edge.source_service_id, edge.target_service_id)
        if pair in removed:
            kept = _without_static(edge)
            if kept is not None:
                edges.append(kept)
        elif pair in head_static:
            edges.append(edge.model_copy(update={"evidence_files": head_files(*pair)}))
        else:
            edges.append(edge)
    now = datetime.now(timezone.utc)
    known = {(edge.source_service_id, edge.target_service_id): i for i, edge in enumerate(edges)}
    for source, target in added:
        position = known.get((source, target))
        if position is not None:
            existing = edges[position]
            edges[position] = existing.model_copy(
                update={
                    "discovery_methods": [*existing.discovery_methods, DiscoveryMethod.STATIC_CODE],
                    "evidence_files": head_files(source, target),
                }
            )
            continue
        edges.append(
            DependencyEdge(
                source_service_id=source,
                target_service_id=target,
                discovery_methods=[DiscoveryMethod.STATIC_CODE],
                confidence_score=_STATIC_CONFIDENCE,
                last_observed_at=now,
                provenance=HEAD_SCAN_PROVENANCE,
                evidence_files=head_files(source, target),
            )
        )
    used = {edge.target_service_id for edge in edges} | {edge.source_service_id for edge in edges}
    defaults = (
        None
        if graph.static_defaults_used is None
        else [
            *(use for use in graph.static_defaults_used if use.service_id not in sources),
            *defaults_used.values(),
        ]
    )
    channels = (
        None
        if graph.channels is None
        else replace_service_uses(
            graph.channels,
            roots.values(),
            [
                ServiceChannelUse(service_id=roots[scan.service.root], use=use)
                for scan in head_services
                if scan.service.root in roots
                for use in scan.channel_uses
            ],
        )
    )
    return HeadTopology(
        graph=graph.model_copy(
            update={
                "edges": edges,
                "unresolved_entities": [e for e in unresolved.values() if e.entity_id in used],
                "static_config": _head_configs(graph, head_services),
                "static_defaults_used": defaults,
                "channels": channels,
            }
        ),
        added=[DependencyChange(source_id=s, target_id=t) for s, t in added],
        removed=[DependencyChange(source_id=s, target_id=t) for s, t in removed],
    )


def _head_configs(
    graph: KnowledgeGraph, head_services: list[ServiceScan]
) -> list[ServiceStaticConfig] | None:
    """The baseline's static configuration with the changed services' read at head.

    None for a baseline predating static configuration records, so the head
    resolves exactly as that baseline did.
    """
    if graph.static_config is None:
        return None
    head = {
        (scan.config.repository.lower(), scan.config.root): scan.config
        for scan in head_services
        if scan.config is not None
    }
    kept = [c for c in graph.static_config if (c.repository.lower(), c.root) not in head]
    return [*kept, *head.values()]


def missing_head_services(roots: dict[str, str], head_services: list[ServiceScan]) -> list[str]:
    """Return the changed services the head no longer has a boundary for."""
    found = {scan.service.root for scan in head_services}
    return sorted(service for root, service in roots.items() if root not in found)
