"""Plain-language narration of discovery and assessment steps.

Pure functions: given a node's name, the state before it and the update it
returned, produce the ``EventDraft``s a person reads in the live log. They
read node updates only; they never call adapters or change state.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, Final

from src.core.models import (
    DiscoveryStageReport,
    DiscoveryStatus,
    EvidenceIssue,
    RunEventStatus,
    RuntimeEvidenceState,
    ServiceIdentity,
)
from src.orchestration.run_log import EventDraft

_MESSAGE_LIMIT: Final[int] = 280
State = Mapping[str, Any]


def short_name(node_id: str) -> str:
    """``namespace/name`` for a service FQURI; the ID itself otherwise."""
    try:
        identity = ServiceIdentity.parse(node_id)
    except ValueError:
        return node_id
    return f"{identity.namespace}/{identity.service_name}"


def _names(ids: Iterable[str]) -> str:
    listed = [short_name(i) for i in ids]
    return ", ".join(listed) if listed else "none"


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _event(
    stage: str, status: RunEventStatus, message: str, details: Iterable[str] = ()
) -> EventDraft:
    detail_list = [d for d in details if d]
    if len(message) > _MESSAGE_LIMIT:
        detail_list.insert(0, message)
        message = message[: _MESSAGE_LIMIT - 1].rstrip() + "…"
    return EventDraft(stage=stage, status=status, message=message, details=detail_list)


def _new_issues(before: State, update: State) -> list[EvidenceIssue]:
    if "discovery_issues" not in update:
        return []
    return list(update["discovery_issues"])[len(before.get("discovery_issues") or []) :]


def _issue_events(stage: str, before: State, update: State) -> list[EventDraft]:
    return [
        _event(stage, RunEventStatus.BLOCKED, f"{issue.code.value}: {issue.detail}")
        for issue in _new_issues(before, update)
    ]


def _new_report(before: State, update: State) -> DiscoveryStageReport | None:
    reports = list(update.get("discovery_reports") or [])
    previous = len(before.get("discovery_reports") or [])
    return reports[previous] if len(reports) > previous else None


def _stage_problem(stage: str, report: DiscoveryStageReport) -> EventDraft:
    status = (
        RunEventStatus.FAILED
        if report.status is DiscoveryStatus.FAILED
        else RunEventStatus.WARNING
    )
    return _event(
        stage,
        status,
        f"{stage} {report.status.value.lower()}: {_plural(len(report.errors), 'error')}. "
        "An incomplete stage can't become a baseline.",
        report.errors,
    )


# --- Discovery ------------------------------------------------------------


def _scope(before: State, update: State) -> list[EventDraft]:
    repositories = list(update.get("target_repositories") or [])
    namespaces = list(update.get("target_namespaces") or [])
    workspace = (before.get("workspace_id") or "").strip() or "none"
    return [
        _event(
            "Scope",
            RunEventStatus.INFO,
            f"Onboarding {before.get('tenant', 'default')}/{before.get('project_id', 'default')} "
            f"for {before.get('environment', '?')}/{before.get('cluster', '?')}: "
            f"{_plural(len(repositories), 'repository', 'repositories')}, "
            f"namespaces {', '.join(namespaces) or 'none'}, telemetry workspace {workspace}.",
        )
    ]


def _code_scan(before: State, update: State) -> list[EventDraft]:
    stage = "Code scan"
    report = _new_report(before, update)
    if report is None:
        return []
    if report.status is DiscoveryStatus.SKIPPED:
        return [_event(stage, RunEventStatus.INFO, "Skipped: no Azure DevOps project configured.")]
    if report.status is not DiscoveryStatus.SUCCESS:
        return [_stage_problem(stage, report)]
    revisions = list(update.get("scanned_revisions") or [])
    services = len(update.get("discovered_services") or []) - len(
        before.get("discovered_services") or []
    )
    return [
        _event(
            stage,
            RunEventStatus.PASSED,
            f"Read {_plural(len(revisions), 'repository', 'repositories')}: "
            f"{_plural(services, 'service directory', 'service directories')}, "
            f"{_plural(report.item_count, 'dependency reference')} in code.",
            [f"{r.repository} @ {r.commit_sha[:12]} ({r.branch})" for r in revisions],
        )
    ]


def _kubernetes(before: State, update: State) -> list[EventDraft]:
    stage = "Kubernetes scan"
    report = _new_report(before, update)
    if report is None:
        return []
    if report.status is DiscoveryStatus.SKIPPED:
        return [_event(stage, RunEventStatus.INFO, "Skipped: no namespaces configured.")]
    if report.status is not DiscoveryStatus.SUCCESS:
        return [_stage_problem(stage, report)]
    server = update.get("kubernetes_api_server") or ""
    namespaces = ", ".join(before.get("target_namespaces") or [])
    events = [
        _event(
            stage,
            RunEventStatus.PASSED,
            f"Read {_plural(report.item_count, 'service')} in {namespaces}"
            + (f" from {server}." if server else "."),
        )
    ]
    if not server:
        events.append(
            _event(
                stage,
                RunEventStatus.WARNING,
                "The API server could not be identified, so this baseline can't prove "
                "which cluster it read.",
            )
        )
    return events


def _telemetry(before: State, update: State) -> list[EventDraft]:
    stage = "Telemetry"
    report = _new_report(before, update)
    if report is None:
        return []
    if report.status is DiscoveryStatus.SKIPPED:
        return [_event(stage, RunEventStatus.INFO, "Skipped: no telemetry workspace configured.")]
    if report.status is not DiscoveryStatus.SUCCESS:
        return [_stage_problem(stage, report)]
    cluster_filter = update.get("telemetry_cluster")
    dropped = [f"Dropped as noise: {count} × {reason}" for reason, count in sorted(report.dropped_records.items())]
    events = [
        _event(
            stage,
            RunEventStatus.PASSED if report.item_count else RunEventStatus.WARNING,
            f"Found {_plural(report.item_count, 'observed dependency', 'observed dependencies')}"
            + (
                f" from cluster {cluster_filter.attribute} = {cluster_filter.value}."
                if cluster_filter
                else "."
            ),
            dropped,
        )
    ]
    if any(edge.is_mock_fixture for edge in update.get("runtime_edges") or []):
        events.append(
            _event(
                stage,
                RunEventStatus.WARNING,
                "These are demo fixture rows (TELEMETRY_SOURCE=mock), not observed traffic; "
                "nothing assessed against them can be approved.",
            )
        )
    if cluster_filter is None:
        events.append(
            _event(
                stage,
                RunEventStatus.WARNING,
                "Not filtered to a cluster (no valid project policy names one), so a policy "
                "requiring live evidence will reject this baseline.",
            )
        )
    return events


def _reconcile(before: State, update: State) -> list[EventDraft]:
    stage = "Reconcile"
    graph = update.get("reconciled_graph") or {}
    services = list(graph.get("services") or [])
    edges = list(graph.get("edges") or [])
    unresolved = list(graph.get("unresolved_entities") or [])
    events = [
        _event(
            stage,
            RunEventStatus.PASSED,
            f"Merged the evidence: {_plural(len(services), 'service')}, "
            f"{_plural(len(edges), 'dependency', 'dependencies')}.",
        )
    ]
    if unresolved:
        kinds = Counter(entity.kind.value for entity in unresolved)
        summary = ", ".join(
            f"{kind.lower().replace('_', ' ')}: {count}" for kind, count in sorted(kinds.items())
        )
        events.append(
            _event(
                stage,
                RunEventStatus.WARNING,
                f"{_plural(len(unresolved), 'endpoint')} matched no single service ({summary}). "
                "They block any release whose radius reaches them, unless the policy allows them.",
                [f"{entity.entity_id} ({entity.kind.value})" for entity in unresolved],
            )
        )
    return events


def _persist(before: State, update: State) -> list[EventDraft]:
    snapshot = update.get("baseline_snapshot_id")
    return [
        _event("Baseline", RunEventStatus.PASSED, f"Saved baseline snapshot {snapshot}.")
    ] if snapshot else []


_DISCOVERY = {
    "initialize_scan_scope": _scope,
    "extract_static_code_baseline": _code_scan,
    "extract_k8s_topology": _kubernetes,
    "extract_runtime_telemetry": _telemetry,
    "reconcile_knowledge_graph": _reconcile,
    "persist_to_graph_db": _persist,
}


def narrate_discovery(node: str, before: State, update: State) -> list[EventDraft]:
    """Events for one discovery node."""
    narrator = _DISCOVERY.get(node)
    return narrator(before, update) if narrator else []


# --- Assessment -----------------------------------------------------------


def _policy(before: State, update: State) -> list[EventDraft]:
    stage = "Policy"
    loaded = update.get("project_policy")
    events = []
    if loaded is not None:
        policy = loaded.policy
        events.append(
            _event(
                stage,
                RunEventStatus.PASSED,
                f"Loaded policy v{policy.policy_version} for {policy.tenant}/{policy.project_id}: "
                f"releases to {policy.environment}/{policy.cluster}, "
                f"requires {policy.runtime_evidence.value} runtime evidence.",
                [f"sha256 {loaded.sha256[:12]}"],
            )
        )
    return events + _issue_events(stage, before, update)


def _trigger(before: State, update: State) -> list[EventDraft]:
    stage = "Pull request"
    pull_request = update.get("pull_request")
    events = []
    if pull_request is not None:
        events.append(
            _event(
                stage,
                RunEventStatus.INFO,
                f"{pull_request.pr_id}: {pull_request.title} ({pull_request.change_type.value}), "
                f"{_plural(len(pull_request.changed_files), 'changed file')} mapped to "
                f"{_names(update.get('target_services') or [])}.",
                list(pull_request.changed_files),
            )
        )
    exempted = list(update.get("exempted_files") or [])
    if exempted:
        events.append(
            _event(
                stage,
                RunEventStatus.INFO,
                f"{_plural(len(exempted), 'file')} exempt by policy.",
                [f"{e.subject} ({e.rule}: {e.reason})" for e in exempted],
            )
        )
    return events + _issue_events(stage, before, update)


def _revision(before: State, update: State) -> list[EventDraft]:
    stage = "Pull request revisions"
    events: list[EventDraft] = []
    changes = update.get("dependency_changes")
    if changes is not None:
        added = [f"+ {short_name(c.source_id)} -> {short_name(c.target_id)}" for c in changes.added]
        removed = [f"- {short_name(c.source_id)} -> {short_name(c.target_id)}" for c in changes.removed]
        events.append(
            _event(
                stage,
                RunEventStatus.PASSED,
                "Revisions checked with source control; changed services read at the head: "
                f"{_plural(len(added), 'dependency', 'dependencies')} added, "
                f"{len(removed)} removed.",
                added + removed,
            )
        )
    return events + _issue_events(stage, before, update)


def _radius(before: State, update: State) -> list[EventDraft]:
    stage = "Release radius"
    events = []
    snapshot = update.get("baseline_snapshot_id")
    if snapshot:
        upstream = list(update.get("upstream_services") or [])
        downstream = list(update.get("downstream_services") or [])
        events.append(
            _event(
                stage,
                RunEventStatus.PASSED,
                f"Against baseline {snapshot}: {_names(update.get('target_services') or [])} "
                f"changes; {_plural(len(upstream), 'caller')} and "
                f"{_plural(len(downstream), 'dependency', 'dependencies')} could be affected.",
                [f"Callers: {_names(upstream)}", f"Dependencies: {_names(downstream)}"],
            )
        )
    exempted = list(update.get("exempted_dependencies") or [])[
        len(before.get("exempted_dependencies") or []) :
    ]
    if exempted:
        events.append(
            _event(
                stage,
                RunEventStatus.INFO,
                f"{_plural(len(exempted), 'unresolved dependency', 'unresolved dependencies')} allowed by policy.",
                [f"{e.subject} ({e.rule}: {e.reason})" for e in exempted],
            )
        )
    return events + _issue_events(stage, before, update)


def _live(before: State, update: State) -> list[EventDraft]:
    stage = "Live check"
    status = update.get("telemetry_status")
    events: list[EventDraft] = []
    if status is not None:
        state = status.runtime_evidence
        coverage = status.coverage
        details = []
        if coverage is not None:
            details = [
                f"Seen recently: {_names(coverage.observed_services)}",
                f"Not seen recently enough: {_names(coverage.uncovered_services)}",
                f"Allowed to be quiet: {_names(coverage.silent_services)}",
            ] + [f"Dropped as noise: {n} × {r}" for r, n in sorted(status.dropped_records.items())]
        if status.source == "knowledge_graph":
            message = (
                f"No live query (no workspace given); runtime evidence comes from the "
                f"baseline: {state.value}."
            )
            level = RunEventStatus.INFO
        elif state is RuntimeEvidenceState.LIVE:
            assert coverage is not None
            message = (
                f"Live telemetry covers the radius: {_plural(coverage.edge_count, 'live edge')}, "
                f"every service seen within the allowed window."
            )
            level = RunEventStatus.PASSED
        else:
            message = f"Live telemetry is {state.value}; see the reason below."
            level = RunEventStatus.WARNING
        events.append(_event(stage, level, message, details))
    return events + _issue_events(stage, before, update)


def _sequence(before: State, update: State) -> list[EventDraft]:
    stage = "Deployment order"
    plan = update.get("deployment_plan")
    if plan is None:
        return []
    order = " → ".join(short_name(s) for s in plan.sequence)
    events = [
        _event(
            stage,
            RunEventStatus.PASSED if plan.is_acyclic else RunEventStatus.BLOCKED,
            (
                f"{plan.change_type.value.capitalize()} change: deploy {order}."
                if plan.is_acyclic
                else f"No safe order: a dependency cycle involves {_names(plan.cyclic_services)}."
            ),
        )
    ]
    if plan.stale_consumers:
        events.append(
            _event(
                stage,
                RunEventStatus.WARNING,
                "Breaking change: these unchanged consumers must already handle the new "
                f"contract: {_names(plan.stale_consumers)}.",
            )
        )
    return events


def _risk(before: State, update: State) -> list[EventDraft]:
    stage = "Risk model"
    advisory = update.get("advisory_assessment")
    if advisory is None:
        return [
            _event(
                stage,
                RunEventStatus.INFO,
                "Not consulted: the deterministic checks already block this release.",
            )
        ]
    return [
        _event(
            stage,
            RunEventStatus.PASSED if advisory.approved else RunEventStatus.BLOCKED,
            f"Advisory review {'found no objection' if advisory.approved else 'rejected the release'} "
            f"(risk {advisory.risk_score:.2f}). It can block, never approve.",
            [advisory.justification],
        )
    ]


def _verdict(before: State, update: State) -> list[EventDraft]:
    assessment = update.get("release_assessment")
    if assessment is None:
        return []
    if assessment.approved:
        return [_event("Verdict", RunEventStatus.PASSED, "Approved: no blocking reason applies.")]
    codes = ", ".join(code.value for code in assessment.reason_codes) or "none"
    return [_event("Verdict", RunEventStatus.BLOCKED, f"Not approved: {codes}.")]


_ASSESSMENT = {
    "load_policy_node": _policy,
    "extract_trigger_node": _trigger,
    "verify_revision_node": _revision,
    "calculate_static_radius_node": _radius,
    "validate_runtime_telemetry_node": _live,
    "sequence_deployment_node": _sequence,
    "assess_risk_and_sequence_node": _risk,
    "compile_evidence_node": _verdict,
}


def narrate_assessment(node: str, before: State, update: State) -> list[EventDraft]:
    """Events for one release-assessment node."""
    narrator = _ASSESSMENT.get(node)
    return narrator(before, update) if narrator else []
