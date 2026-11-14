"""Observability gap report API.

``GET /api/v1/projects/{tenant}/{project_id}/observability`` probes the
project's telemetry (the workspace the newest baseline was built from, plus
the policy's additional workspaces, within the policy's telemetry scope)
and reports, per telemetry role, which instrumentation writes it, whether
it is sampled and whether it carries what the platform needs, and the
observability gaps of the whole estate (see ``coordinator.observability``).
Read-only; it never changes a baseline or an assessment.
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from src.coordinator import nodes as coordinator_nodes
from src.coordinator.observability import gap_stacks, observability_gaps
from src.coordinator.runtime_drift import baseline_identity_context
from src.core import config
from src.core.models import ObservabilityGap, TelemetryRoleProfile
from src.tools.baseline_store import PATH_SEGMENT_PATTERN, BaselineScope
from src.tools.policy_store import PolicyInvalidError, PolicyMissingError, ProjectPolicy
from src.tools.telemetry_extractor import TelemetryExtractor

router = APIRouter(prefix="/api/v1/projects/{tenant}/{project_id}/observability", tags=["observability"])

Tenant = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]
ProjectId = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]


class ObservabilityReport(BaseModel):
    """What the project's telemetry holds, and where it falls short.

    ``stacks`` counts named roles per instrumentation stack; ``scope`` is
    the policy's telemetry scope as text ("" when unscoped).
    """

    snapshot_id: str
    generated_at: datetime
    workspace_id: str
    additional_workspaces: list[str] = Field(default_factory=list)
    scope: str = ""
    lookback_days: int = Field(ge=1)
    stacks: dict[str, int] = Field(default_factory=dict)
    roles: list[TelemetryRoleProfile] = Field(default_factory=list)
    gaps: list[ObservabilityGap] = Field(default_factory=list)


def _policy(scope: BaselineScope) -> ProjectPolicy | None:
    try:
        return coordinator_nodes.POLICY_STORE.load(scope).policy
    except (PolicyMissingError, PolicyInvalidError):
        return None


@router.get("", response_model=ObservabilityReport)
def get_observability(tenant: Tenant, project_id: ProjectId) -> ObservabilityReport:
    """Probe the project's telemetry and report its observability gaps."""
    scope = BaselineScope(tenant=tenant, project_id=project_id)
    baseline = coordinator_nodes.BASELINE_STORE.latest(scope)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No complete discovery baseline for {tenant}/{project_id}; run onboarding first.",
        )
    workspace_id = baseline.graph.scan.telemetry_workspace_id if baseline.graph.scan else ""
    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The newest baseline read no telemetry workspace; onboard with one first.",
        )
    policy = _policy(scope)
    telemetry_scope = policy.telemetry_scope if policy else None
    days = policy.runtime_lookback_days if policy else config.DEFAULT_RUNTIME_LOOKBACK_DAYS
    extractor = TelemetryExtractor(
        workspace_id, policy.telemetry_additional_workspaces if policy else ()
    )
    if extractor.is_mock:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Telemetry is the demo fixture (TELEMETRY_SOURCE=mock); there is nothing to probe.",
        )
    profiles = extractor.get_role_profiles(days, telemetry_scope)
    edges = extractor.get_active_dependencies(days, telemetry_scope)
    if profiles is None or edges.status.is_incomplete:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The telemetry workspace could not be fully read; see the server log.",
        )
    now = datetime.now(timezone.utc)
    graph = baseline.graph
    return ObservabilityReport(
        snapshot_id=baseline.snapshot_id,
        generated_at=now,
        workspace_id=workspace_id,
        additional_workspaces=list(policy.telemetry_additional_workspaces) if policy else [],
        scope=telemetry_scope.describe() if telemetry_scope else "",
        lookback_days=days,
        stacks={stack.value: count for stack, count in gap_stacks(profiles).items()},
        roles=profiles,
        gaps=observability_gaps(
            graph, baseline_identity_context(tenant, graph), profiles, edges.items, now
        ),
    )
