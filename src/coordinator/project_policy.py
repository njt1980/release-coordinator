"""Per-project release eligibility checks.

Pure functions that compare an assessment's evidence with the project's
``ProjectPolicy``: the baseline's environment and cluster, baseline
freshness, required evidence sources, the runtime evidence requirement,
and the exemption allowlist. Each failed
check is an ``EvidenceIssue`` whose reason code limits actionability, so
the advisory model is not consulted and the release is not approved.
"""

from collections.abc import Collection, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone

from src.core import config
from src.core.models import (
    DependencyChange,
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    UNRESOLVED_ID_PREFIX,
    EvidenceIssue,
    Exemption,
    LiveCoverage,
    ObservedEdge,
    ReasonCode,
    RegulatedImpact,
    RuntimeEvidenceState,
    ScanContext,
    ServiceIdentity,
    UnresolvedEntityKind,
)
from src.core.service_ids import classify_unresolved, host_of
from src.core.telemetry_capability import BEYLA, TelemetryCapability
from src.tools.policy_store import ProjectPolicy, RuntimeEvidenceRequirement

_ACCEPTED_RUNTIME: dict[RuntimeEvidenceRequirement, frozenset[RuntimeEvidenceState]] = {
    RuntimeEvidenceRequirement.LIVE: frozenset({RuntimeEvidenceState.LIVE}),
    RuntimeEvidenceRequirement.BASELINE: frozenset(
        {RuntimeEvidenceState.LIVE, RuntimeEvidenceState.BASELINE}
    ),
}


def baseline_scope_issue(
    policy: ProjectPolicy, snapshot_id: str, scan: ScanContext | None
) -> EvidenceIssue | None:
    """Return a BASELINE_SCOPE_MISMATCH issue unless the snapshot scanned the
    policy's environment and cluster.

    The cluster is proven, not just labeled: when the policy names a
    Kubernetes API server, the snapshot's manifest scan must have read that
    server, and when it names a telemetry cluster filter, the snapshot's
    telemetry query must have applied exactly that filter. A snapshot that
    does not record its scan context (written before it was recorded)
    cannot show what it scanned, so it fails too.
    """
    expected = f"{policy.environment}/{policy.cluster}"
    if scan is None:
        detail = (
            f"Baseline snapshot {snapshot_id} does not record the environment, "
            f"cluster or revisions it scanned, so it cannot be shown to describe "
            f"{expected}. Re-run discovery."
        )
    elif (scan.environment, scan.cluster) != (policy.environment, policy.cluster):
        detail = (
            f"Baseline snapshot {snapshot_id} scanned {scan.environment}/"
            f"{scan.cluster}, but the project policy releases to {expected}."
        )
    elif (
        policy.kubernetes_api_server
        and scan.kubernetes_api_server != policy.kubernetes_api_server
    ):
        detail = (
            f"Baseline snapshot {snapshot_id} read Kubernetes API server "
            f"{scan.kubernetes_api_server or 'none (or not recorded)'}, but the "
            f"project policy's cluster {policy.cluster} is "
            f"{policy.kubernetes_api_server}. Re-run discovery against that cluster."
        )
    elif policy.telemetry_scope and scan.telemetry_scope != policy.telemetry_scope:
        recorded = (
            scan.telemetry_scope.describe()
            if scan.telemetry_scope
            else "no telemetry scope (or none recorded)"
        )
        detail = (
            f"Baseline snapshot {snapshot_id} read telemetry scoped to {recorded}, "
            f"but the project policy requires {policy.telemetry_scope.describe()}. "
            "Re-run discovery."
        )
    elif (
        scan.telemetry_workspace_id
        and scan.telemetry_additional_workspaces != policy.telemetry_additional_workspaces
    ):
        detail = (
            f"Baseline snapshot {snapshot_id} read telemetry from additional workspaces "
            f"{', '.join(scan.telemetry_additional_workspaces) or 'none'}, but the "
            f"project policy lists "
            f"{', '.join(policy.telemetry_additional_workspaces) or 'none'}. Re-run discovery."
        )
    else:
        return None
    return EvidenceIssue(code=ReasonCode.BASELINE_SCOPE_MISMATCH, detail=detail)


def baseline_freshness_issue(
    policy: ProjectPolicy,
    snapshot_id: str,
    created_at: datetime | None,
    now: datetime,
) -> EvidenceIssue | None:
    """Return a BASELINE_STALE issue unless the snapshot is fresh enough.

    A snapshot exactly ``max_baseline_age_hours`` old is still fresh. A
    snapshot of unknown age is stale.
    """
    limit = timedelta(hours=policy.max_baseline_age_hours)
    if created_at is None:
        detail = (
            f"Baseline snapshot {snapshot_id} has no recorded creation time, so "
            f"it cannot be shown to be under {policy.max_baseline_age_hours}h old."
        )
    elif now - created_at > limit:
        hours = (now - created_at).total_seconds() / 3600
        detail = (
            f"Baseline snapshot {snapshot_id} is {hours:.1f}h old; the project "
            f"policy allows at most {policy.max_baseline_age_hours}h. Re-run "
            "discovery."
        )
    else:
        return None
    return EvidenceIssue(code=ReasonCode.BASELINE_STALE, detail=detail)


def required_source_issues(
    policy: ProjectPolicy,
    snapshot_id: str,
    reports: list[DiscoveryStageReport] | None,
) -> list[EvidenceIssue]:
    """Return one REQUIRED_SOURCE_MISSING issue per required source that did
    not SUCCEED in the snapshot (SKIPPED, missing, or incomplete)."""
    by_source = {report.source: report for report in reports or []}
    issues: list[EvidenceIssue] = []
    for source in policy.required_sources:
        report = by_source.get(source)
        status = report.status.value if report else "not reported"
        if report is None or report.status is not DiscoveryStatus.SUCCESS:
            issues.append(
                EvidenceIssue(
                    code=ReasonCode.REQUIRED_SOURCE_MISSING,
                    detail=(
                        f"The project policy requires {source.value} evidence, "
                        f"but that stage is {status} in baseline snapshot "
                        f"{snapshot_id}."
                    ),
                )
            )
    return issues


def baseline_runtime_state(
    reports: list[DiscoveryStageReport] | None,
) -> RuntimeEvidenceState:
    """Classify the runtime evidence a baseline alone provides."""
    for report in reports or []:
        if (
            report.source is DiscoveryMethod.RUNTIME_TELEMETRY
            and report.status is DiscoveryStatus.SUCCESS
        ):
            return RuntimeEvidenceState.BASELINE
    return RuntimeEvidenceState.NONE


def runtime_evidence_issue(
    policy: ProjectPolicy, state: RuntimeEvidenceState, why: str = ""
) -> EvidenceIssue | None:
    """Return a REQUIRED_SOURCE_MISSING issue if ``state`` is weaker than the
    policy's runtime evidence requirement; ``why`` explains the state."""
    if state in _ACCEPTED_RUNTIME[policy.runtime_evidence]:
        return None
    if state is RuntimeEvidenceState.LIVE_UNOBSERVABLE:
        return EvidenceIssue(
            code=ReasonCode.RUNTIME_UNOBSERVABLE,
            detail=(
                f"The project policy requires {policy.runtime_evidence.value} runtime "
                "evidence, but some radius services talk only protocols the telemetry "
                "can't see." + (f" {why}" if why else "")
            ),
        )
    return EvidenceIssue(
        code=ReasonCode.REQUIRED_SOURCE_MISSING,
        detail=(
            f"The project policy requires {policy.runtime_evidence.value} runtime "
            f"evidence, but this assessment has {state.value} runtime evidence."
            + (f" {why}" if why else "")
        ),
    )


def _utc(moment: datetime) -> datetime:
    return moment if moment.utcoffset() is not None else moment.replace(tzinfo=timezone.utc)


def _namespaced_name(service_id: str) -> str:
    try:
        identity = ServiceIdentity.parse(service_id)
    except ValueError:
        return service_id
    return f"{identity.namespace}/{identity.service_name}"


def regulated_impact(
    policy: ProjectPolicy | None,
    changed_services: Iterable[str],
    added: Iterable[DependencyChange],
    removed: Iterable[DependencyChange],
) -> list[RegulatedImpact]:
    """Regulated services a pull request touches, and how.

    A service is touched when the PR changes it, or when the head adds or
    removes a code dependency to or from it. Matched by
    ``<namespace>/<name>`` against the policy's ``regulated_services``.
    """
    if policy is None or not policy.regulated_services:
        return []
    reasons: dict[str, list[str]] = {}

    def note(service: str, reason: str) -> None:
        if policy.regulated_service(_namespaced_name(service)) is not None:
            reasons.setdefault(service, []).append(reason)

    for service in changed_services:
        note(service, "this pull request changes it")
    for verb, changes in (("adds", added), ("removes", removed)):
        for change in changes:
            source, target = _namespaced_name(change.source_id), _namespaced_name(change.target_id)
            note(change.target_id, f"this pull request {verb} a dependency on it from {source}")
            note(change.source_id, f"this pull request {verb} its dependency on {target}")
    impacts = []
    for service, found in sorted(reasons.items()):
        entry = policy.regulated_service(_namespaced_name(service))
        assert entry is not None
        impacts.append(
            RegulatedImpact(
                service=service, classification=entry.classification, reasons=list(dict.fromkeys(found))
            )
        )
    return impacts


def _live_window(policy: ProjectPolicy | None) -> timedelta:
    return timedelta(
        minutes=policy.max_telemetry_age_minutes
        if policy
        else config.DEFAULT_MAX_TELEMETRY_AGE_MINUTES
    )


def unobservable_protocols(
    edges: Iterable[DependencyEdge],
    services: Collection[str],
    capability: TelemetryCapability = BEYLA,
    service_capabilities: Mapping[str, TelemetryCapability] | None = None,
) -> dict[str, list[str]]:
    """Radius services the telemetry can't see, with the protocols why.

    A service is unobservable when it has dependencies (as caller or
    callee) and every one is provably outside the telemetry's capability:
    its address names a protocol the telemetry can't parse, or one whose
    version is unknown (``amqp://``), and telemetry never observed it. An
    edge whose protocol is unknown because no address scheme was seen (a
    bare host) proves nothing, so it keeps the service observable: it
    stays uncovered if unseen.

    Without ``service_capabilities`` one ``capability`` judges every edge.
    With it (per service, from the instrumentation stacks its telemetry
    shows), an edge is observable when either end's capability sees its
    protocol; an end with no telemetry of its own sees nothing.
    """
    def sees(edge: DependencyEdge) -> bool:
        if service_capabilities is None:
            return capability.can_observe(edge.protocol)
        return any(
            end in service_capabilities and service_capabilities[end].can_observe(edge.protocol)
            for end in (edge.source_service_id, edge.target_service_id)
        )

    protocols: dict[str, set[str]] = {}
    observable: set[str] = set()
    for edge in edges:
        for service in (edge.source_service_id, edge.target_service_id):
            if service not in services:
                continue
            seen = DiscoveryMethod.RUNTIME_TELEMETRY in edge.discovery_methods
            if seen or edge.protocol is None or sees(edge):
                observable.add(service)
            else:
                protocols.setdefault(service, set()).add(edge.protocol)
    return {
        service: sorted(found)
        for service, found in sorted(protocols.items())
        if service not in observable
    }


def live_coverage(
    policy: ProjectPolicy | None,
    observed: Collection[ObservedEdge],
    radius: Collection[str],
    now: datetime,
    blind: Mapping[str, list[str]] | None = None,
) -> LiveCoverage:
    """Describe, service by service, how live telemetry covers the radius.

    A radius service's last sighting is the newest resolved live edge it is
    the source or target of. It is observed when that is within
    ``max_telemetry_age_minutes`` of ``now``. Otherwise an active
    silent-service entry may allow it: always without ``max_quiet_days``,
    or when it was seen within that many days. Anything else is uncovered,
    so one busy service can't make a quiet one look fresh.

    A service in ``blind`` (see ``unobservable_protocols``) that wasn't seen
    is unobservable instead: no silent allowance applies to it, only an
    active ``exemptions.unobservable`` entry for each of its protocols.
    """
    blind = blind or {}
    last_seen: dict[str, datetime] = {}
    for edge in observed:
        edge_seen = _utc(edge.last_observed_at)
        for node in (edge.source_id, edge.target_id):
            if node in radius and (node not in last_seen or edge_seen > last_seen[node]):
                last_seen[node] = edge_seen
    window = _live_window(policy)
    today = now.date()
    observed_services: list[str] = []
    silent: list[str] = []
    uncovered: list[str] = []
    expired: list[str] = []
    unobservable: dict[str, list[str]] = {}
    accepted: dict[str, list[str]] = {}
    for service in sorted(radius):
        seen_at = last_seen.get(service)
        if seen_at is not None and now - seen_at <= window:
            observed_services.append(service)
            continue
        if service in blind:
            covered = all(_unobservable_allowed(policy, p, today, expired) for p in blind[service])
            (accepted if covered else unobservable)[service] = blind[service]
            continue
        rule = policy.silent_service_rule(_namespaced_name(service)) if policy else None
        if rule is not None and not rule.is_active(today):
            expired.append(service)
            rule = None
        allowed = rule is not None and (
            rule.max_quiet_days is None
            or (seen_at is not None and now - seen_at <= timedelta(days=rule.max_quiet_days))
        )
        (silent if allowed else uncovered).append(service)
    return LiveCoverage(
        edge_count=len(observed),
        newest_observed_at=max((_utc(e.last_observed_at) for e in observed), default=None),
        observed_services=observed_services,
        uncovered_services=uncovered,
        silent_services=silent,
        service_last_seen=last_seen,
        expired_exemptions=sorted(set(expired)),
        unobservable_services=unobservable,
        accepted_unobservable=accepted,
    )


def _unobservable_allowed(
    policy: ProjectPolicy | None, protocol: str, today: date, expired: list[str]
) -> bool:
    rule = policy.unobservable_rule(protocol) if policy else None
    if rule is not None and not rule.is_active(today):
        expired.append(f"unobservable protocol {protocol}")
        return False
    return rule is not None


def _unscoped_name_matches(source: str, service_name: str) -> bool:
    """Whether a telemetry source name looks like ``service_name``.

    Exporters may prefix the name with ``service.namespace`` (``shop.orders``
    or ``shop/orders``).
    """
    name = source.strip().lower()
    return name == service_name or name.endswith(("." + service_name, "/" + service_name))


def with_unscoped_sources(coverage: LiveCoverage, unscoped: dict[str, int] | None) -> LiveCoverage:
    """Attach the unscoped-telemetry diagnostic to ``coverage``.

    Each uncovered radius service is matched by name to the unscoped
    sources; matches stay uncovered (the cluster scope is never relaxed)
    but explain why.
    """
    if unscoped is None:
        return coverage
    matches: dict[str, list[str]] = {}
    for service in coverage.uncovered_services:
        try:
            name = ServiceIdentity.parse(service).service_name
        except ValueError:
            continue
        found = sorted(source for source in unscoped if _unscoped_name_matches(source, name))
        if found:
            matches[service] = found
    return coverage.model_copy(
        update={"unscoped_sources": dict(unscoped), "unscoped_services": matches}
    )


def _ago(now: datetime, moment: datetime | None) -> str:
    if moment is None:
        return "not seen in the lookback window"
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 120:
        return f"last seen {minutes} minutes ago"
    hours = minutes // 60
    return f"last seen {hours} hours ago" if hours < 48 else f"last seen {hours // 24} days ago"


def live_runtime_state(
    policy: ProjectPolicy | None, coverage: LiveCoverage, now: datetime
) -> tuple[RuntimeEvidenceState, str]:
    """Classify a completed, real live query and explain a weak result.

    LIVE_EMPTY: no usable rows. LIVE_STALE: even the newest row is older
    than the live window, so telemetry isn't arriving at all.
    LIVE_UNCOVERED: some radius service wasn't seen recently enough (see
    ``live_coverage``). Each later check needs the earlier one to pass.
    """
    if coverage.edge_count == 0 or coverage.newest_observed_at is None:
        return RuntimeEvidenceState.LIVE_EMPTY, (
            "The live telemetry query returned no usable rows for this cluster."
        )
    window = _live_window(policy)
    minutes_allowed = int(window.total_seconds() // 60)
    if now - coverage.newest_observed_at > window:
        return RuntimeEvidenceState.LIVE_STALE, (
            f"The newest live telemetry row is "
            f"{int((now - coverage.newest_observed_at).total_seconds() // 60)} minutes "
            f"old; at most {minutes_allowed} minutes is allowed, so telemetry is not "
            "arriving."
        )
    if coverage.uncovered_services:
        details = "; ".join(
            f"{service} ({_ago(now, coverage.service_last_seen.get(service))})"
            for service in coverage.uncovered_services
        )
        expired = (
            " Expired silent-service entries were ignored for: "
            + ", ".join(coverage.expired_exemptions)
            + "."
            if coverage.expired_exemptions
            else ""
        )
        unscoped = "".join(
            f" {service} sends telemetry without the cluster attribute ("
            + ", ".join(
                f"{source}: {(coverage.unscoped_sources or {}).get(source, 0)} rows"
                for source in sources
            )
            + "), which is never counted as this cluster's; add the k8s.cluster.name "
            "and k8s.namespace.name resource attributes (see docs/access-requirements.md)."
            for service, sources in sorted(coverage.unscoped_services.items())
        )
        return RuntimeEvidenceState.LIVE_UNCOVERED, (
            f"Every radius service must be seen within {minutes_allowed} minutes; not "
            f"seen recently enough: {details}.{expired}{unscoped}{_sampling_note(coverage)} "
            "Allow intentionally quiet services under exemptions.silent_services."
            + (f" {_unobservable_detail(coverage)}" if coverage.unobservable_services else "")
        )
    if coverage.unobservable_services:
        return RuntimeEvidenceState.LIVE_UNOBSERVABLE, _unobservable_detail(coverage)
    return RuntimeEvidenceState.LIVE, ""


# How many sampled sources an explanation names before summarizing.
_SAMPLED_SOURCES_SHOWN = 5


def _sampling_note(coverage: LiveCoverage) -> str:
    """Say that sampled telemetry can miss a rarely called service, if it is sampled."""
    if not coverage.sampled_sources:
        return ""
    ranked = sorted(coverage.sampled_sources.items(), key=lambda item: (-item[1], item[0]))
    shown = ", ".join(f"{source} (1 row per up to {count} calls)" for source, count in ranked[:_SAMPLED_SOURCES_SHOWN])
    more = len(ranked) - _SAMPLED_SOURCES_SHOWN
    return (
        f" This telemetry is sampled ({shown}{f', and {more} more' if more > 0 else ''}),"
        " so a service called rarely can have no row in the window while working."
    )


def _unobservable_detail(coverage: LiveCoverage) -> str:
    services = "; ".join(
        f"{service} ({', '.join(protocols)})"
        for service, protocols in sorted(coverage.unobservable_services.items())
    )
    profiles = "".join(
        f" {service} is instrumented with {', '.join(names)}."
        for service, names in sorted(coverage.telemetry_profiles.items())
        if service in coverage.unobservable_services and names
    )
    return (
        "The configured telemetry can't see these services' protocols, so not seeing "
        f"them proves nothing: {services}.{profiles} Instrument them (OpenTelemetry or "
        "Application Insights auto-instrumentation), or accept the blind spot per protocol "
        "under exemptions.unobservable after checking their dependencies by hand."
    )


def is_normalized_relative_path(path: str) -> bool:
    """Whether ``path`` is a plain repository-relative POSIX path.

    Absolute paths, backslashes, and empty, ``.`` or ``..`` segments are
    rejected, so ``docs/../src/app.py`` cannot match an exemption for
    ``docs/*``.
    """
    if not path or path.startswith("/") or "\\" in path:
        return False
    return all(segment not in ("", ".", "..") for segment in path.split("/"))


def partition_exempt_files(
    policy: ProjectPolicy | None, files: Iterable[str]
) -> tuple[list[str], list[Exemption]]:
    """Split ``files`` into those still unmapped and those the policy exempts.

    The first matching file exemption is recorded. Only normalized relative
    paths can be exempted (see ``is_normalized_relative_path``). Without a
    policy nothing is exempt.
    """
    rules = policy.exemptions.files if policy else []
    remaining: list[str] = []
    exempted: list[Exemption] = []
    for path in files:
        candidates = rules if is_normalized_relative_path(path) else []
        rule = next((r for r in candidates if r.matches(path)), None)
        if rule is None:
            remaining.append(path)
        else:
            exempted.append(Exemption(subject=path, rule=rule.pattern, reason=rule.reason))
    return remaining, exempted


def dependency_exemption(
    policy: ProjectPolicy | None, entity_id: str
) -> Exemption | None:
    """Return the policy's exemption for unresolved ``entity_id``, if any.

    An exact dependency exemption wins; otherwise an egress rule may allow
    it (see ``egress_exemption``).
    """
    for rule in policy.exemptions.dependencies if policy else []:
        if rule.entity_id == entity_id:
            return Exemption(subject=entity_id, rule=rule.entity_id, reason=rule.reason)
    return egress_exemption(policy, entity_id)


def egress_exemption(policy: ProjectPolicy | None, entity_id: str) -> Exemption | None:
    """Return the egress rule allowing unresolved ``entity_id``, if any.

    Host rules allow only external or private DNS names, CIDR rules only IP
    addresses. Service-shaped names, environment variables and ambiguous
    services are never egress.
    """
    if policy is None or not entity_id.startswith(UNRESOLVED_ID_PREFIX):
        return None
    reference = entity_id[len(UNRESOLVED_ID_PREFIX) :]
    kind = classify_unresolved(reference)
    host = host_of(reference)
    for rule in policy.egress:
        if kind in (UnresolvedEntityKind.EXTERNAL_DNS, UnresolvedEntityKind.PRIVATE_DNS):
            allowed = rule.allows_host(host)
        elif kind is UnresolvedEntityKind.IP_ADDRESS:
            allowed = rule.allows_ip(host)
        else:
            allowed = False
        if allowed:
            return Exemption(subject=entity_id, rule=rule.label, reason=rule.reason)
    return None
