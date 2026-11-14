"""Observability gaps: where the telemetry describes services less well than it could.

Pure functions over the telemetry role profiles (one probe query, see
``TelemetryExtractor.get_role_profiles``), the live edges and the baseline
inventory. They never change a verdict; they explain why runtime evidence
is thin and what the team could change. The same rules feed the release
briefing (radius services only) and the estate-wide report.
"""

from collections.abc import Collection, Iterable
from datetime import datetime, timedelta

from src.core import config
from src.core.models import (
    DependencyEdge,
    EdgeResolution,
    KnowledgeGraph,
    ObservabilityGap,
    ObservabilityGapKind,
    TargetDetailKind,
    TelemetryRoleProfile,
)
from src.core.service_ids import IdentityContext, IdentityIndex, resolve_reference
from src.core.telemetry_capability import TelemetryStack
from src.tools.telemetry_extractor import source_reference

# A producer span's dependency type, as Application Insights writes it for
# the classic SDK and the Azure Monitor exporter alike.
_SEND_TYPE_PREFIX = "queue message"
_DISTRO_CONSUMER_HINT = (
    "an Azure Monitor Distro consumer traces message processing only with "
    "AZURE_EXPERIMENTAL_ENABLE_ACTIVITY_SOURCE=true"
)


def _role_key(profile: TelemetryRoleProfile) -> str:
    return source_reference(profile.role, profile.namespace)


def _rows(count: int) -> str:
    return f"{count} row" if count == 1 else f"{count} rows"


def _stacks(profile: TelemetryRoleProfile) -> str:
    return ", ".join(stack.value for stack in profile.stacks) or "unknown instrumentation"


def _profile_gaps(profile: TelemetryRoleProfile, subject: str) -> list[ObservabilityGap]:
    """The gaps one resolved role shows on its own."""
    gaps: list[ObservabilityGap] = []

    def add(kind: ObservabilityGapKind, detail: str) -> None:
        gaps.append(ObservabilityGap(kind=kind, subject=subject, detail=detail, stacks=profile.stacks))

    if profile.rows_with_namespace == 0:
        add(
            ObservabilityGapKind.NO_NAMESPACE,
            f"Its telemetry ({_stacks(profile)}) carries no Kubernetes namespace, so it is "
            "matched by bare name; a service of the same name in another namespace makes "
            "it ambiguous.",
        )
    if profile.request_rows == 0 and profile.dependency_rows > 0:
        add(
            ObservabilityGapKind.NO_REQUEST_TELEMETRY,
            f"Its telemetry ({_stacks(profile)}) has dependencies but no requests, so calls "
            "to it are found only by the address callers used, never confirmed by its own "
            "telemetry.",
        )
    if profile.max_item_count > 1 and profile.represented_calls:
        kept = 100 * profile.rows / profile.represented_calls
        add(
            ObservabilityGapKind.SAMPLED,
            f"Its telemetry is sampled: {_rows(profile.rows)} stand for "
            f"{profile.represented_calls} calls (about {kept:.0f}% kept, up to "
            f"{profile.max_item_count} calls per row), so rarely used dependencies can be "
            "missing.",
        )
    return gaps


def _unconsumed_sends(
    edges: Iterable[DependencyEdge], since: datetime
) -> dict[str, list[str]]:
    """Message sends since ``since`` that no traced consumer processing joined.

    ``edges`` are normalized live edges (targets are hosts). Keyed by the
    sending source reference; values are the addresses (``host`` or
    ``host/entity``) sent to.
    """
    edges = list(edges)
    joined = {
        (edge.source_service_id, edge.dependency_type, detail.name)
        for edge in edges
        if edge.resolution and EdgeResolution.REQUEST_JOIN in edge.resolution
        for detail in edge.target_details or []
        if detail.kind is TargetDetailKind.ADDRESS
    }
    unconsumed: dict[str, set[str]] = {}
    for edge in edges:
        sends = (edge.dependency_type or "").lower().startswith(_SEND_TYPE_PREFIX)
        by_address = not edge.resolution or EdgeResolution.TARGET in edge.resolution
        if not (sends and by_address) or edge.last_observed_at < since:
            continue
        # Normalized edges: the target is the host the joined edge recorded as its address.
        host = edge.target_service_id
        if (edge.source_service_id, edge.dependency_type, host) in joined:
            continue
        entities = [
            detail.name
            for detail in edge.target_details or []
            if detail.kind is TargetDetailKind.RESOURCE
        ]
        addresses = unconsumed.setdefault(edge.source_service_id, set())
        if entities:
            addresses.update(f"{host}/{entity}" for entity in entities)
        else:
            addresses.add(host)
    return {source: sorted(addresses) for source, addresses in unconsumed.items()}


def observability_gaps(
    graph: KnowledgeGraph,
    context: IdentityContext,
    profiles: Iterable[TelemetryRoleProfile],
    edges: Iterable[DependencyEdge],
    now: datetime,
    services: Collection[str] | None = None,
) -> list[ObservabilityGap]:
    """Return the observability gaps of ``services`` (every inventory service when None).

    Roles are resolved to inventory services as live edges are. With
    ``services`` given (a release radius), only gaps of those services are
    returned, plus rows without a role name (they may belong to any);
    without it, roles matching no service and services with no telemetry
    are reported too. A message send counts as unconsumed only when it
    happened within the request-join window, where a traced consumer would
    have joined.
    """
    index = IdentityIndex(service.identity for service in graph.services)
    estate = services is None
    in_scope = set(services) if services is not None else {s.service_id for s in graph.services}
    gaps: list[ObservabilityGap] = []
    seen: set[str] = set()
    for profile in profiles:
        if not profile.role:
            gaps.append(
                ObservabilityGap(
                    kind=ObservabilityGapKind.NO_ROLE_NAME,
                    subject="",
                    detail=(
                        f"{_rows(profile.rows)} ({_stacks(profile)}) without a service name, so "
                        "whose calls are attributed to no service; usually telemetry sent "
                        "before the role name is set (startup)."
                    ),
                    stacks=profile.stacks,
                )
            )
            continue
        identity = resolve_reference(_role_key(profile), index, context)
        if identity is None:
            if estate:
                gaps.append(
                    ObservabilityGap(
                        kind=ObservabilityGapKind.ROLE_NOT_IN_INVENTORY,
                        subject=_role_key(profile),
                        detail=(
                            f"Telemetry role {_role_key(profile)!r} ({_stacks(profile)}, "
                            f"{_rows(profile.rows)}) matches no inventory service, so its calls "
                            "can't be placed in the graph. Is it another application, or named "
                            "differently from its Kubernetes Service?"
                        ),
                        stacks=profile.stacks,
                    )
                )
            continue
        seen.add(identity.uri)
        if identity.uri in in_scope:
            gaps.extend(_profile_gaps(profile, identity.uri))

    since = now - timedelta(days=config.REQUEST_JOIN_DAYS)
    for source, addresses in _unconsumed_sends(edges, since).items():
        identity = resolve_reference(source, index, context)
        subject = identity.uri if identity else source
        if subject not in in_scope:
            continue
        gaps.append(
            ObservabilityGap(
                kind=ObservabilityGapKind.CONSUMER_NOT_TRACED,
                subject=subject,
                detail=(
                    f"It sends messages to {', '.join(addresses)}, but no consumer's processing "
                    f"of them is traced (checked over the last {config.REQUEST_JOIN_DAYS} days), "
                    "so who consumes them is not observed; "
                    f"{_DISTRO_CONSUMER_HINT}."
                ),
            )
        )
    if estate:
        gaps.extend(
            ObservabilityGap(
                kind=ObservabilityGapKind.SERVICE_WITHOUT_TELEMETRY,
                subject=service.service_id,
                detail=(
                    "No request or dependency rows in scope over the lookback. Expected for "
                    "a database or broker, which send no telemetry of their own; for an "
                    "application, its instrumentation or scope is missing."
                ),
            )
            for service in graph.services
            if service.service_id not in seen
        )
    return gaps


def gap_stacks(profiles: Iterable[TelemetryRoleProfile]) -> dict[TelemetryStack, int]:
    """How many named roles each instrumentation stack covers."""
    counts: dict[TelemetryStack, int] = {}
    for profile in profiles:
        if profile.role:
            for stack in profile.stacks:
                counts[stack] = counts.get(stack, 0) + 1
    return counts
