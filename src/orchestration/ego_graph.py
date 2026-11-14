"""The dependency neighbourhood of one service or one release, laid out in columns.

Column 0 holds the centre services (one service, or a pull request's changed
services); callers sit to the left (column -1 for direct callers, -2 for
their callers...), the services they call to the right (+1, +2...), up to
``depth`` hops. Unresolved endpoints (an external host, an IP, an unset
variable) are shown as such. A page draws it without a graph library: the
columns are the layout.
"""

from collections import deque
from collections.abc import Collection
from typing import Final, Literal

from pydantic import BaseModel, Field

from src.core.models import DiscoveryMethod, KnowledgeGraph, ServiceIdentity

MAX_DEPTH: Final[int] = 3
# Beyond this many nodes the farthest are left out (and counted), so the
# drawing stays readable.
MAX_NODES: Final[int] = 60


class EgoNode(BaseModel):
    id: str
    label: str
    namespace: str = ""
    kind: Literal["service", "unresolved"]
    column: int
    center: bool = False


class EgoEdge(BaseModel):
    source: str
    target: str
    methods: list[str]
    runtime: bool


class EgoGraph(BaseModel):
    """Nodes by column, the edges among them, and how many nodes were left out."""

    centers: list[str]
    depth: int = Field(ge=1, le=MAX_DEPTH)
    nodes: list[EgoNode]
    edges: list[EgoEdge]
    left_out: int = Field(ge=0)


def _label(node_id: str, reference: str = "") -> tuple[str, str]:
    """A node's label and namespace; ``reference`` is an unresolved entity's original text."""
    try:
        identity = ServiceIdentity.parse(node_id)
    except ValueError:
        reference = reference or node_id.removeprefix("unresolved:")
        if reference.startswith("env:"):
            # env:<service>/<VARIABLE>: an address read from a variable nothing sets.
            return f"${reference.rsplit('/', 1)[-1]}", ""
        return reference, ""
    return identity.service_name, identity.namespace


def build_ego_graph(graph: KnowledgeGraph, centers: Collection[str], depth: int = 2) -> EgoGraph:
    """The neighbourhood of ``centers`` up to ``depth`` hops each way (see module doc).

    Raises:
        ValueError: if no centre is a service of ``graph`` or ``depth`` is out of range.
    """
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be 1 to {MAX_DEPTH}")
    services = {service.service_id for service in graph.services}
    starts = sorted(set(centers) & services)
    if not starts:
        raise ValueError("none of the centres is a service in this baseline")
    callers: dict[str, list[str]] = {}
    callees: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.source_service_id == edge.target_service_id:
            continue
        callees.setdefault(edge.source_service_id, []).append(edge.target_service_id)
        callers.setdefault(edge.target_service_id, []).append(edge.source_service_id)
    column: dict[str, int] = {start: 0 for start in starts}
    for sign, neighbours in ((-1, callers), (1, callees)):
        queue = deque((start, 0) for start in starts)
        seen = set(starts)
        while queue:
            node, hops = queue.popleft()
            if hops == depth:
                continue
            for neighbour in sorted(neighbours.get(node, [])):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                # A node both caller and callee keeps the nearer side (callers on a tie).
                if neighbour not in column or abs(column[neighbour]) > hops + 1:
                    column[neighbour] = sign * (hops + 1)
                queue.append((neighbour, hops + 1))
    ranked = sorted(column, key=lambda node: (abs(column[node]), column[node], node))
    kept = ranked[:MAX_NODES]
    kept_set = set(kept)
    references = {entity.entity_id: entity.reference for entity in graph.unresolved_entities}
    nodes = []
    for node in kept:
        label, namespace = _label(node, references.get(node, ""))
        nodes.append(
            EgoNode(
                id=node,
                label=label,
                namespace=namespace,
                kind="service" if node in services else "unresolved",
                column=column[node],
                center=column[node] == 0,
            )
        )
    merged: dict[tuple[str, str], set[str]] = {}
    for edge in graph.edges:
        key = (edge.source_service_id, edge.target_service_id)
        if key[0] in kept_set and key[1] in kept_set and key[0] != key[1]:
            merged.setdefault(key, set()).update(method.value for method in edge.discovery_methods)
    edges = [
        EgoEdge(
            source=source,
            target=target,
            methods=sorted(methods),
            runtime=DiscoveryMethod.RUNTIME_TELEMETRY.value in methods,
        )
        for (source, target), methods in sorted(merged.items())
    ]
    return EgoGraph(centers=starts, depth=depth, nodes=nodes, edges=edges, left_out=len(ranked) - len(kept))
