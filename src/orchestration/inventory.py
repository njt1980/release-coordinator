"""Service inventory and criticality report, built from one baseline snapshot.

One row per service: where it lives (repository, directory, namespace),
which evidence sources found it, what the cluster runs for it, how it is
connected (direct callers and callees, everything that depends on it
transitively), whether live telemetry saw it, and how completely its code
was scanned.

Criticality is a ranking derived from the graph, shown with its inputs:
services that more services depend on (transitively, then directly) rank
first, because a failure or a breaking change there reaches further. It is
context for people (and later the order of a briefing); it never enters a
verdict, and it doesn't know business importance, which only the team can
add.
"""

import csv
import io
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from src.core.models import (
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    ServiceIdentity,
)


class InventoryRow(BaseModel):
    """One service in the inventory (see module doc).

    ``found_by`` lists the evidence sources that know the service: a
    dependency edge from or to it, its scanned directory (static code), or
    its workload or container environment (Kubernetes manifests). ``callers``/``callees`` count direct neighbours that are
    services; ``dependents`` every service with a dependency path to this
    one. ``unresolved_targets`` counts its dependencies that resolve to no
    service. ``runtime_last_seen`` is the newest telemetry edge touching
    it (None: telemetry never saw it). ``unscanned_files`` is None when the
    snapshot has no scan coverage for its directory.
    """

    criticality_rank: int = Field(ge=1)
    service_id: str
    name: str
    namespace: str
    repository: str = ""
    service_root: str = ""
    found_by: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    callers: int = Field(ge=0)
    callees: int = Field(ge=0)
    dependents: int = Field(ge=0)
    unresolved_targets: int = Field(ge=0)
    runtime_last_seen: datetime | None = None
    files_scanned: int | None = None
    unscanned_files: int | None = None


class InventoryReport(BaseModel):
    """The inventory of one baseline snapshot, most critical first."""

    snapshot_id: str
    criticality_basis: str = (
        "Ranked by how many services depend on it (transitively), then by direct callers. "
        "Derived from the dependency graph only; never used in a verdict."
    )
    rows: list[InventoryRow]


def _dependents(service_ids: set[str], edges: Iterable[DependencyEdge]) -> dict[str, int]:
    """For each service, how many services reach it over dependency edges."""
    callers: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.source_service_id in service_ids and edge.target_service_id in service_ids:
            if edge.source_service_id != edge.target_service_id:
                callers[edge.target_service_id].add(edge.source_service_id)
    counts: dict[str, int] = {}
    for service in service_ids:
        seen: set[str] = set()
        frontier = list(callers.get(service, ()))
        while frontier:
            caller = frontier.pop()
            if caller in seen or caller == service:
                continue
            seen.add(caller)
            frontier.extend(callers.get(caller, ()))
        counts[service] = len(seen)
    return counts


def _coverage(graph: KnowledgeGraph) -> dict[tuple[str, str], tuple[int, int]]:
    """(repository, service root) -> (files scanned, unscanned source files)."""
    found: dict[tuple[str, str], tuple[int, int]] = {}
    for repository in graph.static_coverage or []:
        for service in repository.services:
            found[(repository.repository, service.root)] = (service.files_scanned, len(service.unscanned))
    return found


def build_inventory(graph: KnowledgeGraph, snapshot_id: str) -> InventoryReport:
    """The inventory of ``graph``, most critical first (see module doc)."""
    ids = {service.service_id for service in graph.services}
    dependents = _dependents(ids, graph.edges)
    callers: dict[str, set[str]] = defaultdict(set)
    callees: dict[str, set[str]] = defaultdict(set)
    unresolved: dict[str, int] = defaultdict(int)
    methods: dict[str, set[str]] = defaultdict(set)
    last_seen: dict[str, datetime] = {}
    for edge in graph.edges:
        source, target = edge.source_service_id, edge.target_service_id
        for end in (source, target):
            methods[end].update(method.value for method in edge.discovery_methods)
            if DiscoveryMethod.RUNTIME_TELEMETRY in edge.discovery_methods and not edge.is_mock_fixture:
                seen = edge.last_observed_at
                last_seen[end] = max(last_seen.get(end, seen), seen)
        if target in ids and source in ids:
            callers[target].add(source)
            callees[source].add(target)
        elif source in ids:
            unresolved[source] += 1
    coverage = _coverage(graph)
    # A recorded container environment means the manifest scan found the workload.
    in_cluster = {environment.service_id for environment in graph.service_environments or []}
    rows: list[InventoryRow] = []
    for service in graph.services:
        identity = ServiceIdentity.parse(service.service_id)
        repository = service.metadata.get(REPOSITORY_METADATA_KEY, "")
        root = service.metadata.get(SERVICE_ROOT_METADATA_KEY, "")
        found_by = set(methods.get(service.service_id, set()))
        if repository:
            found_by.add(DiscoveryMethod.STATIC_CODE.value)
        # Services enter a baseline from the code scan (which records the
        # repository) or the manifest scan; one without a repository came
        # from the cluster.
        if service.deployed_workloads or service.service_id in in_cluster or not repository:
            found_by.add(DiscoveryMethod.K8S_MANIFEST.value)
        scanned = coverage.get((repository, root))
        rows.append(
            InventoryRow(
                criticality_rank=1,
                service_id=service.service_id,
                name=service.name,
                namespace=identity.namespace,
                repository=repository,
                service_root=root,
                found_by=sorted(found_by),
                images=sorted({image for w in service.deployed_workloads or [] for image in w.images}),
                callers=len(callers.get(service.service_id, ())),
                callees=len(callees.get(service.service_id, ())),
                dependents=dependents.get(service.service_id, 0),
                unresolved_targets=unresolved.get(service.service_id, 0),
                runtime_last_seen=last_seen.get(service.service_id),
                files_scanned=scanned[0] if scanned else None,
                unscanned_files=scanned[1] if scanned else None,
            )
        )
    rows.sort(key=lambda row: (-row.dependents, -row.callers, row.namespace, row.name))
    ranked = [row.model_copy(update={"criticality_rank": position}) for position, row in enumerate(rows, 1)]
    return InventoryReport(snapshot_id=snapshot_id, rows=ranked)


CSV_COLUMNS: tuple[str, ...] = tuple(InventoryRow.model_fields)


def _cell(value: object) -> str:
    """A CSV cell; text a spreadsheet would run as a formula is quoted with ``'``."""
    text = ";".join(value) if isinstance(value, list) else ("" if value is None else str(value))
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def inventory_csv(report: InventoryReport) -> str:
    """The inventory as CSV (one row per service; lists joined with ``;``)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in report.rows:
        values = row.model_dump(mode="json")
        writer.writerow([_cell(values[column]) for column in CSV_COLUMNS])
    return buffer.getvalue()
