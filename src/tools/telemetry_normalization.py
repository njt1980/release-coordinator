"""Backend normalization of telemetry dependency targets (gap G1).

Teams that bring their own telemetry don't run our collector, so the noise
it filters reaches the backend: loopback calls inside one pod, targets that
are an HTTP method or a span name rather than a host, URLs instead of
endpoints, and messaging deliveries reported in reverse (a broker
"calling" a consumer's ephemeral port). These pure functions clean the
targets in the telemetry adapter, before edges are resolved, and count
what they drop. The collector's own filters remain an optimization.
"""

import ipaddress
from collections import Counter
from collections.abc import Iterable
from enum import Enum
from typing import Final
from urllib.parse import urlsplit

from src.core.models import DependencyEdge

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


def normalize_target(target: str) -> str | DropReason:
    """Return the canonical ``host[:port]`` of a target, or why it is dropped.

    URLs lose their scheme, path and query; hosts are lower-cased; IPv6
    hosts keep their brackets when a port follows. Ports are kept whatever
    their range (see ``has_ephemeral_port``).
    """
    raw = target.strip()
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
    return f"{shown}:{port}" if port is not None else shown


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

    Returns the kept edges (targets rewritten) and the number of dropped
    edges per ``DropReason`` value.
    """
    kept: list[DependencyEdge] = []
    dropped: Counter[str] = Counter()
    for edge in edges:
        normalized = normalize_target(edge.target_service_id)
        if isinstance(normalized, DropReason):
            dropped[normalized.value] += 1
            continue
        uncertain = has_ephemeral_port(normalized)
        if uncertain and is_messaging_type(edge.dependency_type):
            dropped[DropReason.REVERSED_DELIVERY.value] += 1
            continue
        kept.append(
            edge.model_copy(
                update={
                    "target_service_id": normalized,
                    "direction_uncertain": edge.direction_uncertain or uncertain,
                }
            )
        )
    return kept, dict(dropped)
