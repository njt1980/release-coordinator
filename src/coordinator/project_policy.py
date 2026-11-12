"""Per-project release eligibility checks.

Pure functions that compare an assessment's evidence with the project's
``ProjectPolicy``: baseline freshness, required evidence sources, the
runtime evidence requirement, and the exemption allowlist. Each failed
check is an ``EvidenceIssue`` whose reason code limits actionability, so
the advisory model is not consulted and the release is not approved.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from fnmatch import fnmatchcase

from src.core.models import (
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    EvidenceIssue,
    Exemption,
    ReasonCode,
    RuntimeEvidenceState,
)
from src.tools.policy_store import ProjectPolicy, RuntimeEvidenceRequirement

_ACCEPTED_RUNTIME: dict[RuntimeEvidenceRequirement, frozenset[RuntimeEvidenceState]] = {
    RuntimeEvidenceRequirement.LIVE: frozenset({RuntimeEvidenceState.LIVE}),
    RuntimeEvidenceRequirement.BASELINE: frozenset(
        {RuntimeEvidenceState.LIVE, RuntimeEvidenceState.BASELINE}
    ),
}


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
    policy: ProjectPolicy, state: RuntimeEvidenceState
) -> EvidenceIssue | None:
    """Return a REQUIRED_SOURCE_MISSING issue if ``state`` is weaker than the
    policy's runtime evidence requirement."""
    if state in _ACCEPTED_RUNTIME[policy.runtime_evidence]:
        return None
    return EvidenceIssue(
        code=ReasonCode.REQUIRED_SOURCE_MISSING,
        detail=(
            f"The project policy requires {policy.runtime_evidence.value} runtime "
            f"evidence, but this assessment has {state.value} runtime evidence."
        ),
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
        rule = next((r for r in candidates if fnmatchcase(path, r.pattern)), None)
        if rule is None:
            remaining.append(path)
        else:
            exempted.append(Exemption(subject=path, rule=rule.pattern, reason=rule.reason))
    return remaining, exempted


def dependency_exemption(
    policy: ProjectPolicy | None, entity_id: str
) -> Exemption | None:
    """Return the policy's exemption for unresolved ``entity_id``, if any."""
    for rule in policy.exemptions.dependencies if policy else []:
        if rule.entity_id == entity_id:
            return Exemption(subject=entity_id, rule=rule.entity_id, reason=rule.reason)
    return None
