"""State schema for release orchestration."""

from typing import Required, TypedDict

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
    """

    trigger_event: Required[dict[str, object]]
    workspace_id: Required[str]
    pull_request: PullRequestPayload
    target_services: list[str]
    upstream_services: list[str]
    downstream_services: list[str]
    matched_edges: list[DependencyEdge]
    telemetry_status: TelemetryStatus
    risk_score: float
    approved: bool
    deployment_sequence: list[str]
    justification: str
    evidence_report: str
    release_assessment: ReleaseAssessment
