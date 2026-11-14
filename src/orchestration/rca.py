"""Where to look first when a service fails: an advisory root-cause view.

Given a failing service, it lists what that service depends on (the
services it calls, then theirs, by hop), each with the evidence for the
dependency and what changed in it across the most recent baselines; the
failing service's own changes, the unresolved endpoints it reaches
(external hosts, unset variables), and recent pull request assessments
that changed any of them.

Ranking: nearer first; within a hop, a dependency that changed recently
first, then stronger evidence (seen live and found in code or the cluster,
then live only, then code or cluster only, then declared by a person only).

It is advisory: it reads what discovery recorded, not logs, metrics or
incidents, and it never decides anything. A failure can come from what
calls the service, from something no baseline knows about, or from a change
the baselines don't record (a value in Key Vault, a data change).
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import Enum
from typing import Final

from pydantic import BaseModel, Field

from src.core.models import (
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    ServiceIdentity,
    ServiceNode,
)

MAX_DEPTH: Final[int] = 4
MAX_BASELINES: Final[int] = 20
# Beyond this many dependencies the farthest are left out (and counted).
MAX_UPSTREAMS: Final[int] = 60
ADVISORY_NOTE: Final[str] = (
    "Advisory. Built from discovery baselines and PR assessments only: no logs, metrics or incidents. "
    "A failure can also come from callers, from dependencies no baseline knows, or from changes baselines "
    "don't record (secrets, data, platform)."
)


class EvidenceStrength(str, Enum):
    """How a dependency is known, strongest first."""

    LIVE_AND_DECLARED = "live_and_declared"  # telemetry saw it, and code or the cluster declares it
    LIVE = "live"
    DECLARED = "declared"  # code or the cluster only
    HUMAN = "human"  # a person declared it in the dependency review, nothing else


_STRENGTH_ORDER: Final[dict[EvidenceStrength, int]] = {
    EvidenceStrength.LIVE_AND_DECLARED: 0,
    EvidenceStrength.LIVE: 1,
    EvidenceStrength.DECLARED: 2,
    EvidenceStrength.HUMAN: 3,
}


class ChangeKind(str, Enum):
    SERVICE_ADDED = "service_added"
    SERVICE_REMOVED = "service_removed"
    DEPENDENCY_ADDED = "dependency_added"
    DEPENDENCY_REMOVED = "dependency_removed"
    IMAGE_CHANGED = "image_changed"
    SOURCE_REVISION_CHANGED = "source_revision_changed"
    CONFIG_CHANGED = "config_changed"
    CONTRACTS_CHANGED = "contracts_changed"


class RecordedChange(BaseModel):
    """One difference between two consecutive baselines for one service.

    ``detail`` names what changed; configuration values are never shown,
    only their names.
    """

    kind: ChangeKind
    detail: str
    snapshot_id: str
    previous_snapshot_id: str
    detected_at: datetime | None = None


class PullRequestTouch(BaseModel):
    """A completed PR assessment of this project, as the RCA needs it."""

    job_id: str
    pr_id: str
    headline: str = ""
    services: list[str] = Field(default_factory=list)


class RecentPullRequest(BaseModel):
    job_id: str
    pr_id: str
    headline: str = ""
    services: list[str] = Field(description="The services in this view the pull request changes.")


class Upstream(BaseModel):
    """A service the failing service depends on, ``hops`` calls away.

    ``via`` is the service it is reached from (the failing service at hop 1).
    """

    service_id: str
    name: str
    namespace: str
    hops: int = Field(ge=1)
    via: list[str]
    strength: EvidenceStrength
    methods: list[str]
    provenance: list[str]
    runtime_last_seen: datetime | None = None
    changes: list[RecordedChange] = Field(default_factory=list)


class UnresolvedDependency(BaseModel):
    """An endpoint reached within the view that resolves to no service."""

    reference: str
    hops: int = Field(ge=1)
    from_services: list[str]


class RcaReport(BaseModel):
    """The advisory root-cause view for one service (see module doc)."""

    service_id: str
    name: str
    snapshot_id: str
    baselines_compared: list[str]
    depth: int = Field(ge=1, le=MAX_DEPTH)
    ranking_basis: str = (
        "Nearer first; within a hop, recently changed first, then stronger evidence "
        "(live and declared, live, declared, person only)."
    )
    advisory: str = ADVISORY_NOTE
    service_changes: list[RecordedChange] = Field(default_factory=list)
    upstreams: list[Upstream] = Field(default_factory=list)
    unresolved: list[UnresolvedDependency] = Field(default_factory=list)
    recent_pull_requests: list[RecentPullRequest] = Field(default_factory=list)
    left_out: int = Field(ge=0, default=0)
    notes: list[str] = Field(default_factory=list)


class BaselineView(BaseModel):
    """One baseline snapshot: its ID and graph (newest first in a list)."""

    snapshot_id: str
    graph: KnowledgeGraph
    created_at: datetime | None = None


def resolve_service(graph: KnowledgeGraph, service: str) -> ServiceNode:
    """The service named by ID, or by name when exactly one service has it.

    Raises:
        ValueError: if no service matches, or several share the name.
    """
    for node in graph.services:
        if node.service_id == service:
            return node
    named = [node for node in graph.services if node.name.lower() == service.lower()]
    if len(named) == 1:
        return named[0]
    if named:
        raise ValueError(f"several services are named {service!r}; use the service ID")
    raise ValueError(f"no service {service!r} in this baseline")


def _strength(methods: set[DiscoveryMethod]) -> EvidenceStrength:
    live = DiscoveryMethod.RUNTIME_TELEMETRY in methods
    declared = bool(methods & {DiscoveryMethod.STATIC_CODE, DiscoveryMethod.K8S_MANIFEST})
    if live and declared:
        return EvidenceStrength.LIVE_AND_DECLARED
    if live:
        return EvidenceStrength.LIVE
    if declared:
        return EvidenceStrength.DECLARED
    return EvidenceStrength.HUMAN


def _label(node_id: str, references: Mapping[str, str]) -> str:
    reference = references.get(node_id) or node_id.removeprefix("unresolved:")
    if reference.startswith("env:"):
        return f"${reference.rsplit('/', 1)[-1]}"
    return reference


# --- what changed between baselines ------------------------------------------------


class _Facts(BaseModel):
    """What one baseline records about one service, for comparison."""

    present: bool
    targets: set[str] = Field(default_factory=set)
    images: set[str] = Field(default_factory=set)
    revision: str = ""
    repository: str = ""
    config: dict[str, str] = Field(default_factory=dict)
    contracts: set[str] = Field(default_factory=set)


def _facts(graph: KnowledgeGraph, service_ids: Iterable[str]) -> _Facts:
    ids = set(service_ids)
    nodes = [node for node in graph.services if node.service_id in ids]
    if not nodes:
        return _Facts(present=False)
    references = {entity.entity_id: entity.reference for entity in graph.unresolved_entities}
    facts = _Facts(present=True)
    for edge in graph.edges:
        if edge.source_service_id in ids and edge.target_service_id not in ids:
            facts.targets.add(_target_name(edge.target_service_id, references))
    for node in nodes:
        facts.images.update(image for workload in node.deployed_workloads or [] for image in workload.images)
        repository = node.metadata.get(REPOSITORY_METADATA_KEY, "")
        root = node.metadata.get(SERVICE_ROOT_METADATA_KEY, "")
        if repository:
            facts.repository = repository
            if graph.scan:
                for revision in graph.scan.scanned_revisions:
                    if revision.repository == repository:
                        facts.revision = revision.commit_sha
            for config in graph.static_config or []:
                if config.repository == repository and config.root == root:
                    for binding in config.bindings:
                        facts.config[f"default of {binding.key}"] = repr(
                            sorted(default.template for default in binding.defaults)
                        )
                    for name, value in config.image_variables.items():
                        facts.config[f"image ENV {name}"] = value
            for coverage in graph.static_coverage or []:
                if coverage.repository == repository:
                    for service in coverage.services:
                        if service.root == root:
                            facts.contracts.update(f"{link.path} ({link.role.value})" for link in service.contracts)
    for environment in graph.service_environments or []:
        if environment.service_id in ids:
            for name, value in environment.variables.items():
                facts.config[f"container env {name}"] = value
    return facts


def _target_name(target: str, references: Mapping[str, str]) -> str:
    try:
        return ServiceIdentity.parse(target).service_name
    except ValueError:
        return _label(target, references)


def _listed(values: Iterable[str]) -> str:
    return ", ".join(sorted(values))


def _diff(older: _Facts, newer: _Facts) -> list[tuple[ChangeKind, str]]:
    """The differences from ``older`` to ``newer`` (configuration by name only)."""
    if not older.present and not newer.present:
        return []
    if not older.present:
        return [(ChangeKind.SERVICE_ADDED, "first found in this baseline")]
    if not newer.present:
        return [(ChangeKind.SERVICE_REMOVED, "no longer found")]
    found: list[tuple[ChangeKind, str]] = []
    if added := newer.targets - older.targets:
        found.append((ChangeKind.DEPENDENCY_ADDED, _listed(added)))
    if removed := older.targets - newer.targets:
        found.append((ChangeKind.DEPENDENCY_REMOVED, _listed(removed)))
    if older.images != newer.images and (older.images or newer.images):
        found.append(
            (ChangeKind.IMAGE_CHANGED, f"{_listed(older.images) or 'none'} → {_listed(newer.images) or 'none'}")
        )
    if older.revision and newer.revision and older.revision != newer.revision:
        found.append(
            (
                ChangeKind.SOURCE_REVISION_CHANGED,
                f"{newer.repository} {older.revision[:7]} → {newer.revision[:7]} (repository-wide; "
                "the service's own files may be unchanged)",
            )
        )
    changed = {
        name
        for name in older.config.keys() | newer.config.keys()
        if older.config.get(name) != newer.config.get(name)
    }
    if changed:
        found.append((ChangeKind.CONFIG_CHANGED, _listed(changed)))
    if older.contracts != newer.contracts:
        parts = []
        if added_contracts := newer.contracts - older.contracts:
            parts.append(f"added {_listed(added_contracts)}")
        if removed_contracts := older.contracts - newer.contracts:
            parts.append(f"removed {_listed(removed_contracts)}")
        found.append((ChangeKind.CONTRACTS_CHANGED, "; ".join(parts)))
    return found


def recorded_changes(baselines: Sequence[BaselineView], service_ids: Iterable[str]) -> list[RecordedChange]:
    """What changed for a service across consecutive ``baselines`` (newest first), newest first.

    ``service_ids`` are every ID the service has gone by (a source-code ID a
    review merged into its runtime ID counts as the same service).
    """
    ids = list(service_ids)
    facts = [_facts(baseline.graph, ids) for baseline in baselines]
    changes: list[RecordedChange] = []
    for index in range(len(baselines) - 1):
        newer, older = baselines[index], baselines[index + 1]
        for kind, detail in _diff(facts[index + 1], facts[index]):
            changes.append(
                RecordedChange(
                    kind=kind,
                    detail=detail,
                    snapshot_id=newer.snapshot_id,
                    previous_snapshot_id=older.snapshot_id,
                    detected_at=newer.created_at,
                )
            )
    return changes


# --- the report --------------------------------------------------------------------


def _walk(
    graph: KnowledgeGraph, start: str, depth: int
) -> tuple[dict[str, int], dict[str, list[DependencyEdge]], dict[str, tuple[int, set[str]]]]:
    """Breadth-first over callees: hop per service, the edges reaching each, unresolved targets by hop."""
    services = {node.service_id for node in graph.services}
    outgoing: dict[str, list[DependencyEdge]] = defaultdict(list)
    for edge in graph.edges:
        if edge.source_service_id != edge.target_service_id:
            outgoing[edge.source_service_id].append(edge)
    hops: dict[str, int] = {start: 0}
    reaching: dict[str, list[DependencyEdge]] = defaultdict(list)
    unresolved: dict[str, tuple[int, set[str]]] = {}
    frontier = [start]
    for hop in range(1, depth + 1):
        next_frontier: list[str] = []
        for node in frontier:
            for edge in outgoing.get(node, []):
                target = edge.target_service_id
                if target not in services:
                    known_hop, sources = unresolved.get(target, (hop, set()))
                    if known_hop == hop:
                        sources.add(node)
                    unresolved[target] = (known_hop, sources)
                    continue
                if target not in hops:
                    hops[target] = hop
                    next_frontier.append(target)
                if hops[target] == hop:
                    reaching[target].append(edge)
        frontier = next_frontier
    hops.pop(start)
    return hops, reaching, unresolved


def build_rca(
    graph: KnowledgeGraph,
    service: str,
    baselines: Sequence[BaselineView],
    depth: int = 3,
    pull_requests: Iterable[PullRequestTouch] = (),
    aliases: Mapping[str, Iterable[str]] | None = None,
) -> RcaReport:
    """The advisory root-cause view of ``service`` (see module doc).

    ``graph`` is the map to walk (the newest baseline, with the dependency
    review applied); ``baselines`` the snapshots to compare, newest first;
    ``aliases`` maps a service ID to the older IDs it went by.

    Raises:
        ValueError: if the service isn't in ``graph`` or ``depth`` is out of range.
    """
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be 1 to {MAX_DEPTH}")
    if not baselines:
        raise ValueError("at least one baseline is needed")
    node = resolve_service(graph, service)
    aliases = aliases or {}
    names = {item.service_id: item for item in graph.services}
    references = {entity.entity_id: entity.reference for entity in graph.unresolved_entities}

    def changes_of(service_id: str) -> list[RecordedChange]:
        return recorded_changes(baselines, [service_id, *aliases.get(service_id, ())])

    hops, reaching, unresolved = _walk(graph, node.service_id, depth)
    upstreams: list[Upstream] = []
    for service_id, hop in hops.items():
        edges = reaching[service_id]
        methods = {method for edge in edges for method in edge.discovery_methods}
        live = [
            edge.last_observed_at
            for edge in edges
            if DiscoveryMethod.RUNTIME_TELEMETRY in edge.discovery_methods and not edge.is_mock_fixture
        ]
        identity = ServiceIdentity.parse(service_id)
        upstreams.append(
            Upstream(
                service_id=service_id,
                name=names[service_id].name,
                namespace=identity.namespace,
                hops=hop,
                via=sorted({names[edge.source_service_id].name for edge in edges}),
                strength=_strength(methods),
                methods=sorted(method.value for method in methods),
                provenance=sorted({edge.provenance for edge in edges}),
                runtime_last_seen=max(live) if live else None,
                changes=changes_of(service_id),
            )
        )
    upstreams.sort(key=lambda u: (u.hops, not u.changes, _STRENGTH_ORDER[u.strength], u.namespace, u.name))
    kept = upstreams[:MAX_UPSTREAMS]

    focus = {node.service_id, *(u.service_id for u in kept)}
    recent = []
    for pull_request in pull_requests:
        touched = sorted(names[s].name for s in set(pull_request.services) & focus)
        if touched:
            recent.append(
                RecentPullRequest(
                    job_id=pull_request.job_id,
                    pr_id=pull_request.pr_id,
                    headline=pull_request.headline,
                    services=touched,
                )
            )

    notes = []
    if len(baselines) < 2:
        notes.append("Only one baseline: there is nothing to compare, so no changes are listed.")
    return RcaReport(
        service_id=node.service_id,
        name=node.name,
        snapshot_id=baselines[0].snapshot_id,
        baselines_compared=[baseline.snapshot_id for baseline in baselines],
        depth=depth,
        service_changes=changes_of(node.service_id),
        upstreams=kept,
        unresolved=sorted(
            (
                UnresolvedDependency(
                    reference=_label(target, references),
                    hops=hop,
                    from_services=sorted(names[source].name for source in sources if source in names),
                )
                for target, (hop, sources) in unresolved.items()
            ),
            key=lambda u: (u.hops, u.reference),
        ),
        recent_pull_requests=recent,
        left_out=len(upstreams) - len(kept),
        notes=notes,
    )
