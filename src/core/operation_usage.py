"""Which caller uses which HTTP operation (consumer attribution).

A breaking OpenAPI change names operations (``GET /orders/{}``); the
contract check lists every caller of the service serving the document.
Telemetry can say more: a server span (``AppRequests``) is named after the
route it handled (``GET /api/orders/{id}``), and its parent is the caller's
dependency span. ``attribute_operations`` matches those observed routes to
the broken operations, so the briefing can say which callers were seen
using each one.

Advisory only. Telemetry covers what ran in its window and may be sampled,
so a caller seen using other operations only may still use a broken one
rarely; such callers are listed apart, never dropped, and the contract
check's status never changes. A caller with no observed request is
unattributed (nothing is known). Route names are matched case-insensitively
by method and path segments; a ``{param}`` segment on either side matches
any segment, so a concrete path (``/orders/5``, from an SDK naming spans by
URL) still matches its template.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from pydantic import BaseModel, Field

from src.core.contract_findings import ContractFinding

_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"}
)
_PARAMETER: Final[re.Pattern[str]] = re.compile(r"^\{[^}]*\}$")


class ObservedOperation(BaseModel, frozen=True):
    """Requests a callee served for a caller, by operation (request name)."""

    caller: str = Field(min_length=1, description="The caller's telemetry role name.")
    callee: str = Field(min_length=1, description="The callee's telemetry role name.")
    operation: str = Field(min_length=1, description="The request name, e.g. 'GET /api/orders/{id}'.")
    calls: int = Field(ge=0, default=0)


class OperationAttribution(BaseModel):
    """The callers seen using each broken operation (advisory).

    ``operation_callers``: broken operation label -> callers seen using it.
    ``other_operations_only``: callers seen calling the server, but none of
    its broken operations. ``unattributed``: callers with no observed request
    (nothing known). ``unmatched_roles``: caller role names seen using a
    broken operation that match no service in the graph. ``lookback_days``
    is the telemetry window.
    """

    operation_callers: dict[str, list[str]] = Field(default_factory=dict)
    other_operations_only: list[str] = Field(default_factory=list)
    unattributed: list[str] = Field(default_factory=list)
    unmatched_roles: list[str] = Field(default_factory=list)
    lookback_days: int = Field(ge=1)


def split_operation(name: str) -> tuple[str | None, list[str]] | None:
    """A request or operation name's method (None when absent) and path
    segments, lower-case; None when it has no path.

    ``GET /api/orders/{id}?x=1`` -> ``("GET", ["api", "orders", "{id}"])``.
    """
    text = name.strip()
    method: str | None = None
    head, _, rest = text.partition(" ")
    if head.upper() in _METHODS and rest.strip():
        method, text = head.upper(), rest.strip()
    text = text.split("?", 1)[0].split("#", 1)[0]
    if "://" in text:
        text = "/" + text.split("://", 1)[1].partition("/")[2]
    if not text.startswith("/"):
        return None
    return method, [segment.lower() for segment in text.split("/") if segment]


def _segments_match(first: list[str], second: list[str]) -> bool:
    return len(first) == len(second) and all(
        a == b or _PARAMETER.match(a) is not None or _PARAMETER.match(b) is not None
        for a, b in zip(first, second)
    )


def operation_matches(operation: str, observed: str) -> bool:
    """Whether an observed request name is ``operation`` (both ``METHOD /path``).

    The methods must be equal when both are known; the paths must match
    segment by segment, a ``{param}`` segment matching any segment.
    """
    wanted, seen = split_operation(operation), split_operation(observed)
    if wanted is None or seen is None:
        return False
    if wanted[0] is not None and seen[0] is not None and wanted[0] != seen[0]:
        return False
    return _segments_match(wanted[1], seen[1])


def broken_operations(findings: Iterable[ContractFinding], documents: Iterable[str]) -> dict[str, list[str]]:
    """Document path -> the operations its breaking findings name (``METHOD /path``).

    Only findings in ``documents`` (the OpenAPI files) count; a finding
    without an operation (a removed file) names none, and the document's
    callers then stay unattributed.
    """
    wanted = set(documents)
    found: dict[str, dict[str, None]] = {}
    for finding in findings:
        if not finding.breaking or finding.path not in wanted:
            continue
        method, _, rest = finding.symbol.partition(" ")
        path = rest.split(" ", 1)[0]
        if method.upper() in _METHODS and path.startswith("/"):
            found.setdefault(finding.path, {})[f"{method.upper()} {path}"] = None
    return {path: list(operations) for path, operations in sorted(found.items())}


def attribute_operations(
    operations: Iterable[str],
    callers: Iterable[str],
    observed: Iterable[ObservedOperation],
    role_services: Mapping[str, str],
    lookback_days: int,
) -> OperationAttribution:
    """Attribute ``operations`` (broken, ``METHOD /path``) to ``callers``.

    ``observed`` are requests the serving services handled; ``role_services``
    maps a lower-case caller role name to its service id. Callers not in
    ``callers`` are ignored unless they match no service at all
    (``unmatched_roles``).
    """
    wanted = list(dict.fromkeys(operations))
    caller_set = set(callers)
    seen_callers: set[str] = set()
    using: dict[str, set[str]] = {operation: set() for operation in wanted}
    unmatched: set[str] = set()
    for row in observed:
        role = row.caller.strip().lower().rsplit("/", 1)[-1]
        service = role_services.get(role)
        hits = [operation for operation in wanted if operation_matches(operation, row.operation)]
        if service is None:
            if hits:
                unmatched.add(row.caller)
            continue
        if service not in caller_set:
            continue
        seen_callers.add(service)
        for operation in hits:
            using[operation].add(service)
    attributed = {service for services in using.values() for service in services}
    return OperationAttribution(
        operation_callers={operation: sorted(services) for operation, services in using.items()},
        other_operations_only=sorted(seen_callers - attributed),
        unattributed=sorted(caller_set - seen_callers),
        unmatched_roles=sorted(unmatched),
        lookback_days=lookback_days,
    )
