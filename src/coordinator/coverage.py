"""Judge whether the static scan saw every service's source code.

Pure functions over a baseline's ``static_coverage`` and the pull request
head's coverage. A source file the scanner couldn't read (an unsupported
language, a read failure) can declare a dependency nobody sees, in any
service: an unread call into a target would make its caller upstream. So
every service's coverage counts, not only the radius's, and live traffic
doesn't substitute for it. A project accepts specific files with a reviewed
``exemptions.unscanned_files`` rule.
"""

import os
from collections import Counter
from collections.abc import Iterable

from src.core.models import (
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    EvidenceIssue,
    Exemption,
    KnowledgeGraph,
    ReasonCode,
    RepositoryCoverage,
    ServiceCoverage,
    ShapeFinding,
    UnscannedFile,
    UnscannedReason,
)
from src.tools.policy_store import ProjectPolicy

_LISTED = 5


def unscanned_file_exemption(policy: ProjectPolicy | None, path: str) -> Exemption | None:
    """Return the policy's exemption for unscanned file ``path``, if any."""
    for rule in policy.exemptions.unscanned_files if policy else []:
        if rule.matches(path):
            return Exemption(subject=path, rule=rule.pattern, reason=rule.reason)
    return None


def _with_head(
    baseline: list[RepositoryCoverage], head: Iterable[RepositoryCoverage]
) -> list[tuple[RepositoryCoverage, ServiceCoverage, bool]]:
    """Pair each service's coverage with its repository, the head's replacing
    the baseline's for the services the head scan read (flagged True)."""
    from_head = {
        (record.repository.lower(), service.root): (record, service)
        for record in head
        for service in record.services
    }
    pairs: list[tuple[RepositoryCoverage, ServiceCoverage, bool]] = []
    for record in baseline:
        for service in record.services:
            replaced = from_head.pop((record.repository.lower(), service.root), None)
            pairs.append((*replaced, True) if replaced else (record, service, False))
    pairs.extend((record, service, True) for record, service in from_head.values())
    return pairs


def _unaccounted_services(
    graph: KnowledgeGraph, pairs: list[tuple[RepositoryCoverage, ServiceCoverage, bool]]
) -> list[str]:
    """Return repository-scanned services with no coverage record."""
    recorded = {(record.repository.lower(), service.root) for record, service, _ in pairs}
    return sorted(
        service.service_id
        for service in graph.services
        if REPOSITORY_METADATA_KEY in service.metadata
        and (
            str(service.metadata[REPOSITORY_METADATA_KEY]).lower(),
            str(service.metadata.get(SERVICE_ROOT_METADATA_KEY, "")),
        )
        not in recorded
    )


def _describe(files: list[UnscannedFile]) -> str:
    """Summarize unscanned files: counts by reason and extension, a sample."""
    parts: list[str] = []
    unsupported = Counter(
        os.path.splitext(f.path)[1] or f.path
        for f in files
        if f.reason is UnscannedReason.UNSUPPORTED_LANGUAGE
    )
    if unsupported:
        kinds = ", ".join(f"{count} {ext}" for ext, count in sorted(unsupported.items()))
        parts.append(f"unsupported language ({kinds})")
    failed = sum(1 for f in files if f.reason is UnscannedReason.READ_FAILED)
    if failed:
        parts.append(f"{failed} unreadable")
    sample = ", ".join(f.path for f in files[:_LISTED])
    more = f" (+{len(files) - _LISTED} more)" if len(files) > _LISTED else ""
    return f"{'; '.join(parts)}: {sample}{more}"


def static_coverage_issues(
    policy: ProjectPolicy | None,
    graph: KnowledgeGraph,
    snapshot_id: str,
    head: Iterable[RepositoryCoverage] = (),
) -> tuple[list[EvidenceIssue], list[Exemption]]:
    """Return STATIC_COVERAGE_INCOMPLETE issues and the exempted files.

    One issue per service with unscanned source files the policy doesn't
    exempt, read at the head for the services the head scan covered. A
    baseline with repository-scanned services but no coverage records
    predates coverage accounting, and a service missing from them is
    unaccounted; either way the scan can't be shown complete.
    """
    if graph.static_coverage is None:
        if any(REPOSITORY_METADATA_KEY in s.metadata for s in graph.services):
            return [
                EvidenceIssue(
                    code=ReasonCode.STATIC_COVERAGE_INCOMPLETE,
                    detail=(
                        f"Baseline {snapshot_id} predates static coverage accounting, "
                        "so it can't show every service's source was scanned; re-run "
                        "discovery."
                    ),
                )
            ], []
        return [], []
    issues: list[EvidenceIssue] = []
    exempted: list[Exemption] = []
    pairs = _with_head(graph.static_coverage, head)
    unaccounted = _unaccounted_services(graph, pairs)
    if unaccounted:
        issues.append(
            EvidenceIssue(
                code=ReasonCode.STATIC_COVERAGE_INCOMPLETE,
                detail=(
                    f"Baseline {snapshot_id} has no coverage record for "
                    f"{', '.join(unaccounted[:_LISTED])}"
                    + (f" (+{len(unaccounted) - _LISTED} more)" if len(unaccounted) > _LISTED else "")
                    + "; re-run discovery."
                ),
            )
        )
    for record, service, at_head in pairs:
        open_files: list[UnscannedFile] = []
        for unscanned in service.unscanned:
            exemption = unscanned_file_exemption(policy, unscanned.path)
            if exemption is None:
                open_files.append(unscanned)
            else:
                exempted.append(exemption)
        if not open_files:
            continue
        where = "the pull request's head" if at_head else "the baseline"
        issues.append(
            EvidenceIssue(
                code=ReasonCode.STATIC_COVERAGE_INCOMPLETE,
                detail=(
                    f"{service.service_name} ({record.repository}/{service.root or '.'}) "
                    f"has {len(open_files)} source file(s) the scan couldn't read at "
                    f"{where} {record.commit_sha[:12]}, so dependencies they declare "
                    f"are invisible: {_describe(open_files)}. Exempt reviewed files in "
                    "the project policy (exemptions.unscanned_files)."
                ),
            )
        )
    return issues, exempted


def radius_shape_findings(
    graph: KnowledgeGraph, services: Iterable[str]
) -> list[ShapeFinding]:
    """Return the out-of-shape findings that concern ``services``."""
    wanted = set(services)
    return [
        finding
        for finding in graph.kubernetes_shape or []
        if finding.service_id is not None and finding.service_id in wanted
    ]
