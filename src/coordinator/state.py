"""State schema for release orchestration."""

from typing import Required, TypedDict

from src.coordinator.policy import ReleaseVerdict
from src.coordinator.sequencing import DeploymentPlan
from src.core.models import (
    DependencyEdge,
    EvidenceIssue,
    PullRequestPayload,
    ReleaseAssessment,
    RiskAssessment,
    TelemetryStatus,
)


class ReleaseGraphState(TypedDict, total=False):
    """Data passed between nodes in the release coordination graph.

    ``trigger_event`` is the raw pull-request payload; it is validated into
    ``pull_request`` by the first node. A blank ``workspace_id`` means no live
    telemetry is queried and runtime evidence comes from the knowledge graph.
    ``tenant`` and ``project_id`` select the baseline's project (both default
    to ``default``). ``baseline_snapshot_id`` pins the snapshot to assess
    against; when it is not supplied, the newest complete snapshot is used
    and recorded here. ``baseline_demo_mode`` records whether that snapshot
    relies on mock data. ``unmapped_files`` are changed files no registered
    service owns.
    ``target_services`` are FQURIs; the radius node replaces them with the
    registered FQURIs they resolve to. ``upstream_services`` and
    ``downstream_services`` hold the full transitive radius, nearest first.
    ``discovery_issues`` lists every reason the evidence is insufficient,
    including unresolved dependencies in the radius, unmapped files and
    demo data; any entry rejects the release without calling the LLM.
    ``deployment_plan`` is the deterministic topological order computed
    before the LLM runs; ``deployment_sequence`` is its sequence, which the
    LLM cannot change. ``advisory_assessment`` is the model's opinion (None
    when it was not consulted) and ``verdict`` the deterministic policy
    decision (see ``decide_release``).
    """

    trigger_event: Required[dict[str, object]]
    workspace_id: Required[str]
    tenant: str
    project_id: str
    baseline_snapshot_id: str
    baseline_demo_mode: bool
    unmapped_files: list[str]
    pull_request: PullRequestPayload
    target_services: list[str]
    upstream_services: list[str]
    downstream_services: list[str]
    matched_edges: list[DependencyEdge]
    telemetry_status: TelemetryStatus
    discovery_issues: list[EvidenceIssue]
    deployment_plan: DeploymentPlan
    deployment_sequence: list[str]
    advisory_assessment: RiskAssessment | None
    verdict: ReleaseVerdict
    evidence_report: str
    release_assessment: ReleaseAssessment
