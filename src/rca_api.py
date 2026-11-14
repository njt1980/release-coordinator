"""Advisory root-cause view API.

``GET /api/v1/projects/{tenant}/{project_id}/rca?service=<id or name>&depth=3&baselines=5``
returns where to look first when that service fails (``orchestration.rca``):
its dependencies by hop and evidence, what changed in them across the last
``baselines`` complete baselines, and recent PR assessments that changed
them. The newest baseline is walked with the dependency review applied.
Read-only and advisory.
"""

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from src.coordinator import nodes as coordinator_nodes
from src.core.models import DiscoveryIncompleteError, KnowledgeGraph
from src.orchestration.rca import MAX_BASELINES, MAX_DEPTH, BaselineView, PullRequestTouch, RcaReport, build_rca
from src.orchestration.review import confirmed_map
from src.tools.baseline_store import PATH_SEGMENT_PATTERN, BaselineScope, StoredBaseline
from src.tools.policy_store import PolicyInvalidError, PolicyMissingError, ProjectPolicy
from src.tools.review_store import ReviewStoreCorruptError
from src.webhooks import find_assessment, list_pr_assessments

router = APIRouter(prefix="/api/v1/projects/{tenant}/{project_id}/rca", tags=["rca"])
logger = logging.getLogger(__name__)

Tenant = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]
ProjectId = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]


def _baselines(scope: BaselineScope, count: int) -> list[StoredBaseline]:
    """The newest ``count`` readable complete baselines, newest first."""
    store = coordinator_nodes.BASELINE_STORE
    found: list[StoredBaseline] = []
    for snapshot_id in store.snapshot_ids(scope):
        if len(found) == count:
            break
        try:
            found.append(store.read(scope, snapshot_id))
        except (OSError, ValueError, DiscoveryIncompleteError) as error:
            logger.warning("RCA skips baseline %s: %s", snapshot_id, error)
    return found


def _reviewed(
    scope: BaselineScope, baseline: StoredBaseline
) -> tuple[KnowledgeGraph, dict[str, list[str]], str | None]:
    """The baseline's graph with the dependency review applied, older IDs per merged service, and a note."""
    try:
        policy: ProjectPolicy | None = coordinator_nodes.POLICY_STORE.load(scope).policy
    except (PolicyMissingError, PolicyInvalidError):
        policy = None
    try:
        in_force = coordinator_nodes.review_in_force(scope, baseline, policy, baseline.graph)
    except (ReviewStoreCorruptError, sqlite3.Error, OSError) as error:
        return baseline.graph, {}, f"The dependency review couldn't be read, so it isn't applied: {error}"
    if in_force is None:
        return baseline.graph, {}, None
    applied = confirmed_map(baseline.graph, in_force)
    aliases: dict[str, list[str]] = {}
    for old, new in applied.renamed.items():
        aliases.setdefault(new, []).append(old)
    return applied.graph, aliases, None


def _pull_requests(scope: BaselineScope) -> list[PullRequestTouch]:
    """This project's completed PR assessments, newest first."""
    touches = []
    for summary in list_pr_assessments():
        if summary.status != "completed" or (summary.tenant, summary.project_id) != (scope.tenant, scope.project_id):
            continue
        job = find_assessment(summary.job_id)
        if job is None or job.assessment is None:
            continue
        touches.append(
            PullRequestTouch(
                job_id=job.job_id,
                pr_id=job.pr_id,
                headline=summary.headline,
                services=list(job.assessment.services_to_deploy),
            )
        )
    return touches


@router.get("", response_model=RcaReport)
def get_rca(
    tenant: Tenant,
    project_id: ProjectId,
    service: Annotated[str, Query(min_length=1)],
    depth: Annotated[int, Query(ge=1, le=MAX_DEPTH)] = 3,
    baselines: Annotated[int, Query(ge=1, le=MAX_BASELINES)] = 5,
) -> RcaReport:
    """Where to look first when ``service`` fails (advisory)."""
    scope = BaselineScope(tenant=tenant, project_id=project_id)
    stored = _baselines(scope, baselines)
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No complete discovery baseline for {tenant}/{project_id}; run onboarding first.",
        )
    graph, aliases, note = _reviewed(scope, stored[0])
    views = [BaselineView(snapshot_id=b.snapshot_id, graph=b.graph, created_at=b.created_at) for b in stored]
    try:
        report = build_rca(graph, service, views, depth, _pull_requests(scope), aliases)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if note:
        report.notes.append(note)
    return report
