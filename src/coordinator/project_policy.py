"""Per-project release eligibility checks.

Pure functions that compare an assessment's evidence with the project's
``ProjectPolicy``: the baseline's environment and cluster, baseline
freshness, required evidence sources, the runtime evidence requirement,
and the exemption allowlist. Each failed
check is an ``EvidenceIssue`` whose reason code limits actionability, so
the advisory model is not consulted and the release is not approved.
"""

from collections.abc import Collection, Iterable
from datetime import datetime, timedelta, timezone

from src.core import config
from src.core.models import (
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    UNRESOLVED_ID_PREFIX,
    EvidenceIssue,
    Exemption,
    LiveCoverage,
    ObservedEdge,
    ReasonCode,
    RuntimeEvidenceState,
    ScanContext,
    ServiceIdentity,
    TelemetryClusterFilter,
    UnresolvedEntityKind,
)
from src.core.service_ids import classify_unresolved, host_of
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
    elif policy.telemetry_cluster and scan.telemetry_cluster != policy.telemetry_cluster:
        recorded = (
            _describe_filter(scan.telemetry_cluster)
            if scan.telemetry_cluster
            else "no cluster filter (or none recorded)"
        )
        detail = (
            f"Baseline snapshot {snapshot_id} read telemetry with {recorded}, but "
            f"the project policy requires "
            f"{_describe_filter(policy.telemetry_cluster)}. Re-run discovery."
        )
    else:
        return None
    return EvidenceIssue(code=ReasonCode.BASELINE_SCOPE_MISMATCH, detail=detail)


def _describe_filter(cluster_filter: TelemetryClusterFilter) -> str:
    return f"{cluster_filter.attribute} == {cluster_filter.value!r}"


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


def live_coverage(
    policy: ProjectPolicy | None,
    observed: Collection[ObservedEdge],
    radius: Collection[str],
) -> LiveCoverage:
    """Describe how a live query's resolved edges cover the release radius.

    A radius service is observed when it is the source or target of any
    resolved live edge. The policy's ``silent_services`` (by
    ``<namespace>/<name>``) are reported apart from the uncovered ones.
    """
    seen = {edge.source_id for edge in observed} | {edge.target_id for edge in observed}
    silent_names = {rule.service for rule in policy.exemptions.silent_services} if policy else set()
    unseen = sorted(service for service in radius if service not in seen)
    return LiveCoverage(
        edge_count=len(observed),
        newest_observed_at=max((_utc(e.last_observed_at) for e in observed), default=None),
        observed_services=sorted(service for service in radius if service in seen),
        uncovered_services=[s for s in unseen if _namespaced_name(s) not in silent_names],
        silent_services=[s for s in unseen if _namespaced_name(s) in silent_names],
    )


def live_runtime_state(
    policy: ProjectPolicy | None, coverage: LiveCoverage, now: datetime
) -> tuple[RuntimeEvidenceState, str]:
    """Classify a completed, real live query and explain a weak result.

    Empty beats stale beats uncovered: each later check needs the earlier
    one to pass. Without a policy the default telemetry age applies.
    """
    if coverage.edge_count == 0 or coverage.newest_observed_at is None:
        return RuntimeEvidenceState.LIVE_EMPTY, (
            "The live telemetry query returned no usable rows for this cluster."
        )
    max_age = timedelta(
        minutes=policy.max_telemetry_age_minutes
        if policy
        else config.DEFAULT_MAX_TELEMETRY_AGE_MINUTES
    )
    age = now - coverage.newest_observed_at
    if age > max_age:
        minutes = int(age.total_seconds() // 60)
        return RuntimeEvidenceState.LIVE_STALE, (
            f"The newest live telemetry row is {minutes} minutes old; at most "
            f"{int(max_age.total_seconds() // 60)} minutes is allowed."
        )
    if coverage.uncovered_services:
        return RuntimeEvidenceState.LIVE_UNCOVERED, (
            "Live telemetry did not observe these radius services: "
            + ", ".join(coverage.uncovered_services)
            + ". Allowlist intentionally silent ones under exemptions.silent_services."
        )
    return RuntimeEvidenceState.LIVE, ""


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
