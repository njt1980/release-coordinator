"""State schema for release orchestration."""

from typing import Required, TypedDict

from src.coordinator.sequencing import DeploymentPlan
from src.core.models import (
    DependencyEdge,
    PullRequestPayload,
    ReleaseAssessment,
    TelemetryStatus,
)


class ReleaseGraphState(TypedDict, total=False):
    """Data passed between nodes in the release coordination graph.

    ``trigger_event`` is the raw pull-request payload; it is validated into
    ``pull_request`` by the first node. A blank ``workspace_id`` means no live
    telemetry is queried and runtime evidence comes from the knowledge graph.
    ``tenant`` and ``project_id`` select the baseline to load (both default
    to ``default``); ``baseline_snapshot_id`` and ``baseline_demo_mode``
    record which snapshot was used and whether it relies on mock data.
    ``target_services`` are FQURIs; the radius node replaces them with the
    registered FQURIs they resolve to. ``upstream_services`` and
    ``downstream_services`` hold the full transitive radius, nearest first.
    ``discovery_issues`` lists every reason the dependency evidence may be
    incomplete, including unresolved dependencies in the radius; any entry
    rejects the release without calling the LLM. ``deployment_plan`` is the
    deterministic topological order computed before the LLM runs;
    ``deployment_sequence`` is its sequence, which the LLM cannot change.
    """

    trigger_event: Required[dict[str, object]]
    workspace_id: Required[str]
    tenant: str
    project_id: str
    baseline_snapshot_id: str
    baseline_demo_mode: bool
    pull_request: PullRequestPayload
    target_services: list[str]
    upstream_services: list[str]
    downstream_services: list[str]
    matched_edges: list[DependencyEdge]
    telemetry_status: TelemetryStatus
    discovery_issues: list[str]
    deployment_plan: DeploymentPlan
    risk_score: float
    approved: bool
    deployment_sequence: list[str]
    justification: str
    evidence_report: str
    release_assessment: ReleaseAssessment
