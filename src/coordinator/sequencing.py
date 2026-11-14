"""Deterministic deployment sequencing over the release subgraph.

An edge ``A -> B`` means A calls (depends on) B: A is the consumer, B the
provider. The order is a topological sort of the subgraph spanned by the
target, upstream and downstream services (Kahn's algorithm):

- ADDITIVE changes deploy providers before consumers: for every edge
  ``A -> B``, B precedes A. Downstream dependencies go first, then the
  targets, then upstream consumers.
- BREAKING changes deploy consumers before providers: for every edge
  ``A -> B``, A precedes B. Upstream consumers go first (so they already
  send the newly required input), then the targets, then downstream
  dependencies.

Among services that are ready at the same time, ties are broken by phase
(the position the rule above gives the service's role) and then by service
ID, so the same subgraph always yields the same sequence.

The targets are the services the pull request changes. The sequence still
covers the whole subgraph, so it can be checked against every edge; the plan
also splits it into the services to deploy (the targets) and those that are
unchanged. For a BREAKING change, upstream consumers that are not targets are
reported as ``stale_consumers``: the rule above needs them redeployed before
the targets, but the pull request does not change them.
"""

import heapq
from collections.abc import Iterable

from pydantic import BaseModel, Field

from src.core.models import ContractChange, DependencyEdge


class DeploymentPlan(BaseModel):
    """A deterministic deployment sequence for one release.

    ``sequence`` holds every service in the release subgraph exactly once.
    ``cyclic_services`` lists the services that were in, or blocked behind,
    a dependency cycle; when it is non-empty no order satisfies every edge
    and the part of ``sequence`` covering them is not edge-safe.

    ``services_to_deploy`` and ``unchanged_services`` split ``sequence``
    (keeping its order) into the services the pull request changes and the
    rest of the release subgraph. ``stale_consumers`` lists, for a BREAKING
    change only, the unchanged upstream consumers that must already run a
    compatible version before the targets deploy.

    ``compatibility_verified`` records whether contract compatibility
    evidence proves the order safe. The order rules above are assumptions
    drawn from the declared change type (a new consumer may, for example,
    need a field an old provider cannot return); ``sequence_deployment``
    leaves it False and the coordinator sets it from the contract check
    (``coordinator.contracts``), as it sets ``contract_breaking`` when a
    changed contract breaks compatibility.
    """

    change_type: ContractChange
    sequence: list[str] = Field(min_length=1)
    cyclic_services: list[str] = Field(default_factory=list)
    services_to_deploy: list[str] = Field(default_factory=list)
    unchanged_services: list[str] = Field(default_factory=list)
    stale_consumers: list[str] = Field(default_factory=list)
    compatibility_verified: bool = False
    contract_breaking: bool = False

    @property
    def is_acyclic(self) -> bool:
        """Whether ``sequence`` satisfies every dependency edge."""
        return not self.cyclic_services

    @property
    def safe_order_established(self) -> bool:
        """Whether the order is both acyclic and proven compatible."""
        return self.is_acyclic and self.compatibility_verified


def _phase_ranks(
    targets: list[str],
    upstream: list[str],
    downstream: list[str],
    change_type: ContractChange,
) -> dict[str, int]:
    """Return each service's tie-break phase (lower deploys earlier).

    Targets are phase 1. A non-target service that is both upstream and
    downstream (possible with several targets) is also phase 1, leaving its
    position entirely to the edges.
    """
    first, last = (
        (downstream, upstream)
        if change_type is ContractChange.ADDITIVE
        else (upstream, downstream)
    )
    target_set, first_set, last_set = set(targets), set(first), set(last)
    ranks: dict[str, int] = {}
    for service in [*targets, *upstream, *downstream]:
        if service in target_set or (service in first_set and service in last_set):
            ranks[service] = 1
        elif service in first_set:
            ranks[service] = 0
        else:
            ranks[service] = 2
    return ranks


def _precedence(
    edges: Iterable[DependencyEdge], nodes: set[str], change_type: ContractChange
) -> set[tuple[str, str]]:
    """Return ``(before, after)`` pairs implied by edges inside ``nodes``.

    Edges with an endpoint outside the subgraph (for example an unresolved
    entity) and self-calls carry no deployment constraint.
    """
    pairs: set[tuple[str, str]] = set()
    for edge in edges:
        caller, callee = edge.source_service_id, edge.target_service_id
        if caller == callee or caller not in nodes or callee not in nodes:
            continue
        pairs.add(
            (callee, caller)
            if change_type is ContractChange.ADDITIVE
            else (caller, callee)
        )
    return pairs


def _stale_consumers(
    sequence: list[str],
    targets: list[str],
    upstream: list[str],
    change_type: ContractChange,
) -> list[str]:
    """Return the unchanged consumers a BREAKING change needs updated first.

    An ADDITIVE change keeps the target compatible with the consumers' current
    versions, so it has none. Services are returned in ``sequence`` order.
    """
    if change_type is not ContractChange.BREAKING:
        return []
    consumers = set(upstream) - set(targets)
    return [service for service in sequence if service in consumers]


def sequence_deployment(
    targets: list[str],
    upstream: list[str],
    downstream: list[str],
    edges: Iterable[DependencyEdge],
    change_type: ContractChange,
) -> DeploymentPlan:
    """Topologically sort the release subgraph for ``change_type``.

    Uses Kahn's algorithm with a ``(phase, service ID)`` priority queue. If
    the subgraph has a cycle, the services still waiting when the queue
    first runs dry are recorded in ``cyclic_services``; the lowest-priority
    waiting service is then released so the sequence still covers every
    service. ``targets`` are the services the pull request changes; see
    ``DeploymentPlan`` for how the sequence is split by them.

    Raises:
        ValueError: if ``targets`` is empty.
    """
    if not targets:
        raise ValueError("at least one target service is required")
    ranks = _phase_ranks(targets, upstream, downstream, change_type)
    successors: dict[str, list[str]] = {service: [] for service in ranks}
    indegree = dict.fromkeys(ranks, 0)
    for before, after in _precedence(edges, set(ranks), change_type):
        successors[before].append(after)
        indegree[after] += 1

    ready = [(ranks[s], s) for s, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    sequence: list[str] = []
    placed: set[str] = set()
    cyclic: list[str] = []
    while len(sequence) < len(ranks):
        if not ready:
            waiting = sorted(
                (s for s in ranks if s not in placed), key=lambda s: (ranks[s], s)
            )
            if not cyclic:
                cyclic = sorted(waiting)
            indegree[waiting[0]] = 0
            heapq.heappush(ready, (ranks[waiting[0]], waiting[0]))
        _, service = heapq.heappop(ready)
        if service in placed:
            continue
        sequence.append(service)
        placed.add(service)
        for successor in successors[service]:
            indegree[successor] -= 1
            if indegree[successor] == 0 and successor not in placed:
                heapq.heappush(ready, (ranks[successor], successor))
    target_set = set(targets)
    return DeploymentPlan(
        change_type=change_type,
        sequence=sequence,
        cyclic_services=cyclic,
        services_to_deploy=[s for s in sequence if s in target_set],
        unchanged_services=[s for s in sequence if s not in target_set],
        stale_consumers=_stale_consumers(sequence, targets, upstream, change_type),
    )
