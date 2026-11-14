"""Which wire protocols the configured telemetry can see.

Runtime coverage judges a radius service by whether live telemetry saw it.
That only means something for traffic the instrumentation can parse: a
service that talks only a protocol outside its capability is never seen,
however busy it is. Such a service is *unobservable*, not *silent*: absence
proves nothing, and an allowance for a quiet service would record a false
statement.

Protocols are named from an edge's address scheme (``wire_protocol``). A
scheme that doesn't say which protocol version runs (``amqp://`` is AMQP
0-9-1 for RabbitMQ.Client and AMQP 1.0 for rhea) is unknown, and an
unknown protocol counts as unobservable (fail-closed).

The built-in profile describes Grafana Beyla 3.x (OpenTelemetry eBPF
Instrumentation) as deployed in the lab: HTTP/1.1, HTTP/2 and gRPC
(including TLS through supported libraries), Redis, PostgreSQL, MySQL,
SQL Server, MongoDB, Kafka, NATS and MQTT. It parses AMQP 1.0 only, and
never AMQP over TLS (Azure Service Bus). Source: the OBI support matrix,
docs/research findings 04.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

UNKNOWN_PROTOCOL: Final[str] = "unknown"

# Address scheme -> wire protocol label. Schemes not listed are unknown.
_SCHEME_PROTOCOLS: Final[dict[str, str]] = {
    "http": "http",
    "https": "https",
    "http+https": "http",
    "https+http": "https",
    "grpc": "grpc",
    "grpcs": "grpcs",
    "dns": "grpc",  # gRPC name-resolver target (dns:///host:port)
    "kafka": "kafka",
    "redis": "redis",
    "rediss": "rediss",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlserver": "sqlserver",
    "mongodb": "mongodb",
    "mongodb+srv": "mongodb",
    "nats": "nats",
    "mqtt": "mqtt",
    # amqp:// doesn't say 0-9-1 or 1.0; amqps:// and sb:// are AMQP over TLS.
    "amqp": "amqp",
    "amqps": "amqps",
    "sb": "amqps",
    "lb": "http",  # Spring Cloud LoadBalancer: HTTP to a registry name
    "tcp": UNKNOWN_PROTOCOL,
}


class TelemetryCapability(BaseModel):
    """What one telemetry source can see, by wire protocol."""

    model_config = ConfigDict(frozen=True)

    profile: str = Field(min_length=1)
    observable: frozenset[str]

    def can_observe(self, protocol: str | None) -> bool:
        """Whether traffic of ``protocol`` would show up; unknown never does."""
        return protocol is not None and protocol in self.observable


BEYLA: Final[TelemetryCapability] = TelemetryCapability(
    profile="beyla-3",
    observable=frozenset(
        {
            "http", "https", "grpc", "grpcs", "redis", "rediss", "postgres", "mysql",
            "sqlserver", "mongodb", "kafka", "nats", "mqtt",
        }
    ),
)


def wire_protocol(address: str) -> str | None:
    """The wire protocol an address's scheme names, ``unknown`` for a
    scheme that doesn't say, or None when ``address`` has no scheme."""
    scheme, separator, _ = address.strip().partition("://")
    if not separator:
        return None
    scheme = scheme.lower().removeprefix("jdbc:")
    return _SCHEME_PROTOCOLS.get(scheme, UNKNOWN_PROTOCOL)
