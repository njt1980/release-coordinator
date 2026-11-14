"""State schema for release orchestration."""

from datetime import datetime
from typing import Required, TypedDict

from src.coordinator.policy import ReleaseVerdict
from src.coordinator.revision import HeadTopology, RevisionLag
from src.coordinator.sequencing import DeploymentPlan
from src.core.models import (
    AssessmentReview,
    ChangeFlags,
    ContractCheck,
    DependencyEdge,
    EvidenceIssue,
    Exemption,
    KnowledgeGraph,
    MessagingCheck,
    PullRequestPayload,
    RepositoryCoverage,
    ReleaseAssessment,
    RiskAssessment,
    RuntimeEvidenceState,
    ShapeFinding,
    TelemetryStatus,
)
from src.orchestration.review import ReviewInForce
from src.tools.policy_store import LoadedPolicy


class ReleaseGraphState(TypedDict, total=False):
    """Data passed between nodes in the release coordination graph.

    ``trigger_event`` is the raw pull-request payload; it is validated into
    ``pull_request`` by the first node. A blank ``workspace_id`` means no live
    telemetry is queried and runtime evidence comes from the knowledge graph.
    ``tenant`` and ``project_id`` select the baseline's project (both default
    to ``default``). ``baseline_snapshot_id`` pins the snapshot to assess
    against; when it is not supplied, the newest complete snapshot is used
    and recorded here. ``baseline_demo_mode`` records whether that snapshot
    relies on mock data, ``baseline_created_at`` when it was written, and
    ``baseline_runtime_state`` the runtime evidence it provides on its own.
    ``baseline_graph`` is the loaded snapshot's graph (None when none could
    be loaded); live telemetry is compared with it to detect drift.
    ``project_policy`` is the project's release policy (None when it is
    missing or invalid, which is itself a discovery issue).
    ``unmapped_files`` are changed files no registered service owns and the
    policy does not exempt; ``exempted_files`` and ``exempted_dependencies``
    record what the policy exempted.
    ``target_services`` are FQURIs; the radius node replaces them with the
    registered FQURIs they resolve to. ``upstream_services`` and
    ``downstream_services`` hold the full transitive radius, nearest first.
    ``discovery_issues`` lists every reason the evidence is insufficient,
    including unresolved dependencies in the radius, unmapped files and
    demo data; any entry rejects the release without calling the LLM.
    ``deployment_plan`` is the deterministic topological order computed
    before the LLM runs; ``deployment_sequence`` is its sequence, which the
    LLM cannot change. ``advisory_assessment`` is the model's opinion (None
    when it was not consulted), ``advisory_redactions`` what was redacted
    from what it was sent, and ``verdict`` the deterministic policy
    decision (see ``decide_release``). ``assessed_graph`` is the baseline
    with the changed services' code dependencies read at the pull
    request's head (None when the head couldn't be verified and scanned);
    the radius is walked over it. ``dependency_changes`` records what the
    head added and removed; ``revision_lag`` what changed between the
    baseline's scanned revision and the base. ``head_coverage`` is how
    completely the head scan read the changed services;
    ``exempted_unscanned_files`` the unscanned files the policy accepts, and
    ``shape_findings`` the out-of-shape Kubernetes objects in the radius.
    ``review_in_force`` are the project's dependency review answers that
    count for this release (None without answers), and
    ``assessment_review`` how they shaped it. ``contract_check`` is the
    contract compatibility evidence the plan's compatibility rests on;
    ``change_flags`` the pull request's advisory migration, configuration
    and feature-flag changes; ``messaging_check`` the message channels the
    release's services produce to or consume from (None for a baseline
    predating channels).
    """

    trigger_event: Required[dict[str, object]]
    workspace_id: Required[str]
    tenant: str
    project_id: str
    baseline_snapshot_id: str
    baseline_created_at: datetime | None
    baseline_demo_mode: bool
    baseline_runtime_state: RuntimeEvidenceState
    baseline_graph: KnowledgeGraph | None
    project_policy: LoadedPolicy | None
    unmapped_files: list[str]
    exempted_files: list[Exemption]
    exempted_dependencies: list[Exemption]
    assessed_graph: KnowledgeGraph | None
    revision_lag: RevisionLag | None
    dependency_changes: HeadTopology | None
    head_coverage: list[RepositoryCoverage]
    exempted_unscanned_files: list[Exemption]
    shape_findings: list[ShapeFinding]
    review_in_force: ReviewInForce | None
    assessment_review: AssessmentReview | None
    pull_request: PullRequestPayload
    target_services: list[str]
    upstream_services: list[str]
    upstream_hops: dict[str, int]
    downstream_hops: dict[str, int]
    downstream_services: list[str]
    matched_edges: list[DependencyEdge]
    telemetry_status: TelemetryStatus
    discovery_issues: list[EvidenceIssue]
    contract_check: ContractCheck
    change_flags: ChangeFlags
    messaging_check: MessagingCheck | None
    deployment_plan: DeploymentPlan
    deployment_sequence: list[str]
    advisory_assessment: RiskAssessment | None
    advisory_redactions: dict[str, int]
    verdict: ReleaseVerdict
    evidence_report: str
    release_assessment: ReleaseAssessment
