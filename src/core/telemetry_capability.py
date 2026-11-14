"""Which wire protocols each telemetry stack can see.

Runtime coverage judges a radius service by whether live telemetry saw it.
That only means something for traffic the instrumentation can parse: a
service that talks only a protocol outside its capability is never seen,
however busy it is. Such a service is *unobservable*, not *silent*: absence
proves nothing, and an allowance for a quiet service would record a false
statement.

Protocols are named from an edge's address scheme (``wire_protocol``). A
scheme that doesn't say which protocol version runs (``amqp://`` is AMQP
0-9-1 for RabbitMQ.Client and AMQP 1.0 for rhea) is unknown, and an
unknown protocol counts as unobservable (fail-closed). Azure Service Bus
(``sb://``, or ``amqps://`` to ``*.servicebus.windows.net``) is named
``servicebus``: the Application Insights stacks trace its client library,
which says nothing about other AMQP-over-TLS brokers.

Each profile (``PROFILES``) lists protocol claims with their evidence:
observed in the probe lab (findings 05), stated in vendor documentation,
or unverified. Only the first two make a protocol observable; an
unverified claim is shown, never used to clear an issue. Profiles:

- Grafana Beyla 3.x (OpenTelemetry eBPF Instrumentation): HTTP (lab:
  client and server spans, requests join callers), HTTP/2 and gRPC, Redis, PostgreSQL, MySQL, SQL Server, MongoDB, Kafka,
  NATS and MQTT; AMQP 1.0 only, never AMQP over TLS (OBI support matrix,
  research findings 04).
- Classic Application Insights SDK 2.x for .NET: HTTP and SQL Server (lab)
  and Service Bus (lab: sends, ``Azure Service Bus`` rows, and a consumer
  whose ``ProcessMessage`` requests join the send when the module lists
  ``Azure.Messaging.ServiceBus``); HTTPS from the
  dependency auto-collection documentation.
- Azure Monitor OpenTelemetry Distro for .NET: HTTP (lab); HTTPS and SQL
  Server from the Distro documentation (HttpClient and SqlClient
  instrumentation). Service Bus only with
  ``AZURE_EXPERIMENTAL_ENABLE_ACTIVITY_SOURCE=true`` (lab: without it the
  consumer records nothing; with it, ``ServiceBusProcessor.ProcessMessage``
  requests join the producer's send).
- A plain OpenTelemetry SDK: what it sees depends on the instrumentation
  libraries installed, so every claim is unverified.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

UNKNOWN_PROTOCOL: Final[str] = "unknown"
SERVICE_BUS_PROTOCOL: Final[str] = "servicebus"
_SERVICE_BUS_SUFFIX: Final[str] = ".servicebus.windows.net"

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
    # amqp:// doesn't say 0-9-1 or 1.0; amqps:// is AMQP over TLS (Service Bus
    # when the host is *.servicebus.windows.net, see wire_protocol).
    "amqp": "amqp",
    "amqps": "amqps",
    "sb": SERVICE_BUS_PROTOCOL,
    "lb": "http",  # Spring Cloud LoadBalancer: HTTP to a registry name
    "tcp": UNKNOWN_PROTOCOL,
}


class CapabilityEvidence(str, Enum):
    """Why a profile claims a protocol.

    LAB_VERIFIED: observed in the probe lab (findings 05).
    VENDOR_DOCUMENTED: stated in the instrumentation's documentation.
    UNVERIFIED: plausible, but neither observed nor documented for this
        setup; shown for context, never used to clear an issue.
    """

    LAB_VERIFIED = "lab_verified"
    VENDOR_DOCUMENTED = "vendor_documented"
    UNVERIFIED = "unverified"


class ProtocolClaim(BaseModel):
    """One protocol a profile claims to see, and the evidence for it."""

    model_config = ConfigDict(frozen=True)

    protocol: str = Field(min_length=1)
    evidence: CapabilityEvidence
    note: str = ""


class TelemetryCapability(BaseModel):
    """What one telemetry source can see, by wire protocol.

    Only claims with lab or vendor evidence are ``observable``; unverified
    claims never make a protocol observable.
    """

    model_config = ConfigDict(frozen=True)

    profile: str = Field(min_length=1)
    claims: tuple[ProtocolClaim, ...] = ()
    source: str = ""

    @model_validator(mode="after")
    def _one_claim_per_protocol(self) -> Self:
        protocols = [claim.protocol for claim in self.claims]
        if len(protocols) != len(set(protocols)):
            raise ValueError(f"profile {self.profile!r} claims a protocol twice")
        return self

    @property
    def observable(self) -> frozenset[str]:
        """Protocols backed by lab or vendor evidence."""
        return frozenset(
            claim.protocol
            for claim in self.claims
            if claim.evidence is not CapabilityEvidence.UNVERIFIED
        )

    @property
    def unverified(self) -> frozenset[str]:
        """Protocols claimed without evidence (never observable)."""
        return frozenset(
            claim.protocol
            for claim in self.claims
            if claim.evidence is CapabilityEvidence.UNVERIFIED
        )

    def can_observe(self, protocol: str | None) -> bool:
        """Whether traffic of ``protocol`` would show up; unknown never does."""
        return protocol is not None and protocol in self.observable


def _claims(
    evidence: CapabilityEvidence, protocols: str, note: str = ""
) -> tuple[ProtocolClaim, ...]:
    return tuple(
        ProtocolClaim(protocol=protocol, evidence=evidence, note=note)
        for protocol in protocols.split()
    )


_LAB = CapabilityEvidence.LAB_VERIFIED
_DOCS = CapabilityEvidence.VENDOR_DOCUMENTED
_UNVERIFIED = CapabilityEvidence.UNVERIFIED


class TelemetryProfile(str, Enum):
    """The capability profiles ``PROFILES`` describes."""

    BEYLA = "beyla-3"
    APPINSIGHTS_CLASSIC = "appinsights-classic-dotnet-2"
    AZURE_MONITOR_DISTRO = "azure-monitor-distro-dotnet"
    AZURE_MONITOR_DISTRO_ACTIVITY_SOURCE = "azure-monitor-distro-dotnet+activity-source"
    OTEL_SDK = "otel-sdk"


BEYLA: Final[TelemetryCapability] = TelemetryCapability(
    profile=TelemetryProfile.BEYLA.value,
    claims=(
        *_claims(
            _LAB,
            "http",
            "aks-store client and server spans; requests join their callers (findings 06 F6)",
        ),
        *_claims(
            _DOCS,
            "https grpc grpcs redis rediss postgres mysql sqlserver mongodb kafka nats mqtt",
        ),
    ),
    source="OBI (Beyla) support matrix; research findings 04; lab run findings 06",
)

_DISTRO_COMMON: Final[tuple[ProtocolClaim, ...]] = (
    *_claims(_LAB, "http", "gateway and fulfillment HTTP calls (findings 05 §1)"),
    *_claims(_DOCS, "https sqlserver", "HttpClient and SqlClient instrumentation in the Distro"),
    *_claims(_UNVERIFIED, "grpc grpcs", "gRPC over HttpClient; not observed"),
)

PROFILES: Final[dict[TelemetryProfile, TelemetryCapability]] = {
    TelemetryProfile.BEYLA: BEYLA,
    TelemetryProfile.APPINSIGHTS_CLASSIC: TelemetryCapability(
        profile=TelemetryProfile.APPINSIGHTS_CLASSIC.value,
        claims=(
            *_claims(_LAB, "http", "orders -> inventory (findings 05 §1)"),
            *_claims(_LAB, "sqlserver", "orders -> mssql, type SQL (findings 05 §1)"),
            *_claims(
                _LAB,
                SERVICE_BUS_PROTOCOL,
                "orders' sends, type Azure Service Bus (findings 05 §1); with "
                "Azure.Messaging.ServiceBus in IncludeDiagnosticSourceActivities the consumer's "
                "ServiceBusProcessor.ProcessMessage requests join the send, 132/132 (findings 06 F8)",
            ),
            *_claims(_DOCS, "https", "dependency auto-collection: HTTP/HTTPS"),
            *_claims(_UNVERIFIED, "grpc grpcs", "gRPC over HttpClient; not observed"),
        ),
        source="probe lab findings 05; Application Insights dependency auto-collection docs",
    ),
    TelemetryProfile.AZURE_MONITOR_DISTRO: TelemetryCapability(
        profile=TelemetryProfile.AZURE_MONITOR_DISTRO.value,
        claims=_DISTRO_COMMON,
        source=(
            "probe lab findings 05 (Service Bus consumer records nothing by default); "
            "Azure Monitor OpenTelemetry Distro docs"
        ),
    ),
    TelemetryProfile.AZURE_MONITOR_DISTRO_ACTIVITY_SOURCE: TelemetryCapability(
        profile=TelemetryProfile.AZURE_MONITOR_DISTRO_ACTIVITY_SOURCE.value,
        claims=(
            *_DISTRO_COMMON,
            *_claims(
                _LAB,
                SERVICE_BUS_PROTOCOL,
                "ServiceBusProcessor.ProcessMessage joins the send, 37/37 (findings 05 §3)",
            ),
        ),
        source="probe lab findings 05 with AZURE_EXPERIMENTAL_ENABLE_ACTIVITY_SOURCE=true",
    ),
    TelemetryProfile.OTEL_SDK: TelemetryCapability(
        profile=TelemetryProfile.OTEL_SDK.value,
        claims=_claims(
            _UNVERIFIED,
            "http https grpc grpcs redis postgres mysql sqlserver mongodb kafka",
            "depends on the instrumentation libraries installed",
        ),
        source="OpenTelemetry: an SDK sees only what its instrumentation libraries trace",
    ),
}


class CollectorSource(str, Enum):
    """What feeds an OpenTelemetry Collector's Azure Monitor exporter.

    Its rows (``otelc-…``) don't say whether Beyla or an application's
    OpenTelemetry SDK made the spans, so a project policy says which.
    """

    BEYLA = "beyla"
    OTEL_SDK = "otel_sdk"


class TelemetryStack(str, Enum):
    """Which instrumentation wrote a telemetry row, from its ``SDKVersion``.

    APPINSIGHTS_CLASSIC: the Application Insights SDK 2.x (``rdddsc:2.22.0``,
        ``aspnet5c:2.22.0``): Kubernetes facts only with its enricher.
    AZURE_MONITOR_OTEL: Azure Monitor OpenTelemetry in the application (the
        Distro or the exporter, ``dotnet…:otel1.12.0:ext1.4.0``): resource
        attributes such as the namespace are not written to the row.
    OTEL_COLLECTOR: an OpenTelemetry Collector's Azure Monitor exporter
        (``otelc-…``), for example Beyla's: Kubernetes attributes when the
        Collector adds them, server spans only if the Collector forwards them.
    UNKNOWN: anything else.
    """

    APPINSIGHTS_CLASSIC = "appinsights_classic"
    AZURE_MONITOR_OTEL = "azure_monitor_otel"
    OTEL_COLLECTOR = "otel_collector"
    UNKNOWN = "unknown"


# Observed in the probe lab (findings 05) except the Distro labels dst/mot
# and SDK 3.x (sh?), which come from the exporter's source.
_OTEL_IN_APP: Final[re.Pattern[str]] = re.compile(r":(?:otel|ext|dst|mot|sh[a-z])\d")
_CLASSIC: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+:2\.\d")


def telemetry_stack(sdk_version: str) -> TelemetryStack:
    """Classify an Application Insights ``SDKVersion`` value."""
    version = sdk_version.strip().lower()
    if version.startswith("otelc"):
        return TelemetryStack.OTEL_COLLECTOR
    if _OTEL_IN_APP.search(version):
        return TelemetryStack.AZURE_MONITOR_OTEL
    if _CLASSIC.match(version):
        return TelemetryStack.APPINSIGHTS_CLASSIC
    return TelemetryStack.UNKNOWN


def profiles_for_stacks(
    stacks: Iterable[TelemetryStack],
    collector_source: CollectorSource,
    activity_source: bool = False,
) -> list[TelemetryProfile]:
    """The capability profiles behind a role's instrumentation stacks.

    ``activity_source`` says the role's telemetry shows Service Bus
    activity that the Distro writes only with the experimental activity
    source on. An unknown stack has no profile.
    """
    found: set[TelemetryProfile] = set()
    for stack in stacks:
        if stack is TelemetryStack.APPINSIGHTS_CLASSIC:
            found.add(TelemetryProfile.APPINSIGHTS_CLASSIC)
        elif stack is TelemetryStack.AZURE_MONITOR_OTEL:
            found.add(
                TelemetryProfile.AZURE_MONITOR_DISTRO_ACTIVITY_SOURCE
                if activity_source
                else TelemetryProfile.AZURE_MONITOR_DISTRO
            )
        elif stack is TelemetryStack.OTEL_COLLECTOR:
            found.add(
                TelemetryProfile.BEYLA
                if collector_source is CollectorSource.BEYLA
                else TelemetryProfile.OTEL_SDK
            )
    return sorted(found, key=lambda profile: profile.value)


def combined_capability(profiles: Iterable[TelemetryProfile]) -> TelemetryCapability:
    """One capability seeing what any of ``profiles`` sees (the best evidence wins)."""
    names: list[str] = []
    best: dict[str, ProtocolClaim] = {}
    rank = {_LAB: 0, _DOCS: 1, _UNVERIFIED: 2}
    for profile in profiles:
        names.append(profile.value)
        for claim in PROFILES[profile].claims:
            held = best.get(claim.protocol)
            if held is None or rank[claim.evidence] < rank[held.evidence]:
                best[claim.protocol] = claim
    return TelemetryCapability(
        profile=" + ".join(names) or "none",
        claims=tuple(best[protocol] for protocol in sorted(best)),
    )


def wire_protocol(address: str) -> str | None:
    """The wire protocol an address's scheme names, ``unknown`` for a
    scheme that doesn't say, or None when ``address`` has no scheme."""
    scheme, separator, rest = address.strip().partition("://")
    if not separator:
        return None
    scheme = scheme.lower().removeprefix("jdbc:")
    protocol = _SCHEME_PROTOCOLS.get(scheme, UNKNOWN_PROTOCOL)
    if protocol == "amqps":
        host = rest.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0].lower()
        if host.endswith(_SERVICE_BUS_SUFFIX):
            return SERVICE_BUS_PROTOCOL
    return protocol
