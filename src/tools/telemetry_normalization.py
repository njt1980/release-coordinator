"""Backend normalization of telemetry dependency targets (gap G1).

Teams that bring their own telemetry don't run our collector, so the noise
it filters reaches the backend: loopback calls inside one pod, targets that
are an HTTP method or a span name rather than a host, URLs instead of
endpoints, and messaging deliveries reported in reverse (a broker
"calling" a consumer's ephemeral port). Application Insights also writes
targets with details after the host (``host | cid-v1:<appId>``,
``tcp:server,1433 | database``, ``sb://ns/ | queue``) and OpenTelemetry
messaging targets as ``host/entity``; the host is kept as the target and
the details travel on the edge. These pure functions clean the
targets in the telemetry adapter, before edges are resolved, and count
what they drop. The collector's own filters remain an optimization.
"""

import ipaddress
import re
from collections import Counter
from collections.abc import Iterable
from enum import Enum
from typing import Final
from urllib.parse import urlsplit

from pydantic import BaseModel

from src.core.models import (
    DependencyEdge,
    TargetDetail,
    TargetDetailKind,
    merge_target_details,
)

# Linux's default ephemeral (client-side) port range starts here. A target
# port at or above it may be a client socket (a reply or a delivery reported
# in reverse), or a server that simply listens high: the direction is
# uncertain unless the dependency type settles it.
EPHEMERAL_PORT_START: Final[int] = 32768
# Lower-cased fragments of Application Insights / OpenTelemetry dependency
# types that denote messaging (e.g. "Queue Message | rabbitmq",
# "Azure Service Bus", "Kafka"). A messaging row to an ephemeral port is a
# broker delivering to a consumer, reported in reverse.
_MESSAGING_TYPE_FRAGMENTS: Final[tuple[str, ...]] = (
    "queue message", "messaging", "rabbitmq", "amqp", "kafka", "service bus",
    "servicebus", "event hubs", "eventhub", "nats", "mqtt", "pubsub",
)
# Span names some exporters put in Target when the call has no host.
_NON_HOST_TARGETS: Final[frozenset[str]] = frozenset(
    {"get", "head", "post", "put", "patch", "delete", "options", "trace", "connect"}
)
_LOOPBACK_NAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})
# Application Insights separates a target's host from what is behind it
# with " | " (classic SDK and Azure Monitor exporter alike).
_DETAIL_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"\s\|\s")
# The classic SDK's suffix naming the callee's Application Insights component.
_APP_COMPONENT_PREFIX: Final[str] = "cid-v1:"
# SqlClient data-source forms: an optional protocol prefix (tcp:, np:, lpc:,
# admin:), then host, host,port or host\instance. A prefix followed by a
# bare number is a service of that name and port (admin:8080), kept.
_SQL_PROTOCOL_PREFIX: Final[re.Pattern[str]] = re.compile(r"^(?:tcp|np|lpc|admin):(?!//)(?!\d+$)", re.I)
_SQL_HOST_COMMA_PORT: Final[re.Pattern[str]] = re.compile(r"^([^,\s]+),(\d+)$")


class DropReason(str, Enum):
    """Why a telemetry row was left out.

    LOOPBACK: the target is the calling pod itself.
    NO_HOST: the target names no host (an HTTP method, a span name, a path).
    REVERSED_DELIVERY: a messaging row to a client port: a broker delivery
        reported in the wrong direction.
    """

    LOOPBACK = "loopback"
    NO_HOST = "no_host"
    REVERSED_DELIVERY = "reversed_delivery"


class ParsedTarget(BaseModel):
    """A telemetry target's canonical ``host[:port]`` and what it names behind it."""

    host: str
    details: tuple[TargetDetail, ...] = ()

    model_config = {"frozen": True}


def _sql_data_source(host_part: str) -> str:
    """Rewrite a SqlClient data source (``tcp:host,1433``, ``host\\inst``) as ``host[:port]``.

    Anything else is returned unchanged.
    """
    raw = _SQL_PROTOCOL_PREFIX.sub("", host_part)
    if "://" not in raw and "\\" in raw:
        raw = raw.split("\\", 1)[0]
    comma_port = _SQL_HOST_COMMA_PORT.match(raw)
    return f"{comma_port.group(1)}:{comma_port.group(2)}" if comma_port else raw


def _detail(part: str) -> TargetDetail:
    if part.lower().startswith(_APP_COMPONENT_PREFIX):
        component = part[len(_APP_COMPONENT_PREFIX):].strip()
        if component:
            return TargetDetail(kind=TargetDetailKind.APP_COMPONENT, name=component)
    return TargetDetail(kind=TargetDetailKind.RESOURCE, name=part)


def _messaging_entity(host_part: str) -> str | None:
    """The entity of an OpenTelemetry messaging target ``host/entity``, if any."""
    raw = host_part.strip()
    try:
        path = urlsplit(raw if "://" in raw else f"//{raw}").path
    except ValueError:
        return None
    entity = path.strip("/")
    return entity or None


def _split_host_port(target: str) -> tuple[str, int | None] | None:
    """Return (host, port) of a host, ``host:port``, ``[v6]:port`` or URL.

    Returns None when no host can be read.
    """
    raw = target.strip().lower()
    if not raw:
        return None
    try:
        parts = urlsplit(raw if "://" in raw else f"//{raw}")
        host = (parts.hostname or "").rstrip(".")
        port = parts.port
    except ValueError:
        # A bare IPv6 address (several colons, no brackets).
        try:
            return str(ipaddress.ip_address(raw)), None
        except ValueError:
            return None
    return (host, port) if host else None


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_messaging_type(dependency_type: str | None) -> bool:
    """Whether a telemetry dependency type denotes messaging."""
    lowered = (dependency_type or "").lower()
    return any(fragment in lowered for fragment in _MESSAGING_TYPE_FRAGMENTS)


def parse_target(target: str, dependency_type: str | None = None) -> ParsedTarget | DropReason:
    """Return a target's canonical ``host[:port]`` and details, or why it is dropped.

    URLs lose their scheme, path and query; hosts are lower-cased; IPv6
    hosts keep their brackets when a port follows. Ports are kept whatever
    their range (see ``has_ephemeral_port``). Parts after a `` | ``
    separator become details: ``cid-v1:<appId>`` an application component,
    anything else (a database, queue, event hub) a resource. A SqlClient
    data source (``tcp:server,1433``, ``server\\instance``) becomes
    ``server:1433`` / ``server``. For a messaging ``dependency_type`` the
    path of ``host/entity`` is the entity, kept as a resource.
    """
    host_part, *rest = (part.strip() for part in _DETAIL_SEPARATOR.split(target.strip()))
    raw = _sql_data_source(host_part)
    # A host never contains whitespace; "find orderdb.orders" is a database
    # span name some exporters put in Target.
    if raw.lower() in _NON_HOST_TARGETS or raw.startswith("/") or any(c.isspace() for c in raw):
        return DropReason.NO_HOST
    parsed = _split_host_port(raw)
    if parsed is None:
        return DropReason.NO_HOST
    host, port = parsed
    if _is_loopback(host):
        return DropReason.LOOPBACK
    shown = f"[{host}]" if ":" in host and port is not None else host
    details = [_detail(part) for part in rest if part]
    if is_messaging_type(dependency_type) and (entity := _messaging_entity(raw)):
        details.append(TargetDetail(kind=TargetDetailKind.RESOURCE, name=entity))
    return ParsedTarget(
        host=f"{shown}:{port}" if port is not None else shown,
        details=tuple(dict.fromkeys(details)),
    )


def normalize_target(target: str) -> str | DropReason:
    """Return the canonical ``host[:port]`` of a target, or why it is dropped.

    See ``parse_target``; the details behind the host are left out.
    """
    parsed = parse_target(target)
    return parsed if isinstance(parsed, DropReason) else parsed.host


def has_ephemeral_port(target: str) -> bool:
    """Whether a target's port is in the ephemeral (client-side) range."""
    parsed = _split_host_port(target)
    return bool(parsed and parsed[1] is not None and parsed[1] >= EPHEMERAL_PORT_START)


def normalize_edges(
    edges: Iterable[DependencyEdge],
) -> tuple[list[DependencyEdge], dict[str, int]]:
    """Normalize every edge's target and drop the noise.

    A messaging edge to an ephemeral port is dropped as a reversed
    delivery; any other edge to one is kept with ``direction_uncertain``.

    Returns the kept edges (targets rewritten, details behind the host in
    ``target_details``) and the number of dropped edges per ``DropReason``
    value.
    """
    kept: list[DependencyEdge] = []
    dropped: Counter[str] = Counter()
    for edge in edges:
        parsed = parse_target(edge.target_service_id, edge.dependency_type)
        if isinstance(parsed, DropReason):
            dropped[parsed.value] += 1
            continue
        normalized = parsed.host
        uncertain = has_ephemeral_port(normalized)
        if uncertain and is_messaging_type(edge.dependency_type):
            dropped[DropReason.REVERSED_DELIVERY.value] += 1
            continue
        kept.append(
            edge.model_copy(
                update={
                    "target_service_id": normalized,
                    "direction_uncertain": edge.direction_uncertain or uncertain,
                    "target_details": merge_target_details(edge.target_details, list(parsed.details)),
                }
            )
        )
    return kept, dict(dropped)
