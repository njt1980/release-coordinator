"""Service inventory and dependency-map API.

``GET /api/v1/projects/{tenant}/{project_id}/inventory`` returns the
newest baseline's service inventory, most critical first
(``orchestration.inventory``); ``?format=csv`` returns it as a CSV file.
``GET .../inventory/graph?service=<id>&depth=2`` returns one service's
dependency neighbourhood (``orchestration.ego_graph``), and
``GET /api/v1/webhooks/pr-assessment/{job_id}/graph`` a pull request's,
from the baseline that assessment used. Read-only.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from fastapi.responses import Response

from src.coordinator import nodes as coordinator_nodes
from src.orchestration.ego_graph import MAX_DEPTH, EgoGraph, build_ego_graph
from src.orchestration.inventory import InventoryReport, build_inventory, inventory_csv
from src.tools.baseline_store import PATH_SEGMENT_PATTERN, BaselineScope, StoredBaseline
from src.webhooks import find_assessment

router = APIRouter(prefix="/api/v1/projects/{tenant}/{project_id}/inventory", tags=["inventory"])
assessment_router = APIRouter(prefix="/api/v1/webhooks/pr-assessment", tags=["inventory"])
Depth = Annotated[int, Query(ge=1, le=MAX_DEPTH)]

Tenant = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]
ProjectId = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]


def _latest(tenant: str, project_id: str) -> StoredBaseline:
    baseline = coordinator_nodes.BASELINE_STORE.latest(BaselineScope(tenant=tenant, project_id=project_id))
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No complete discovery baseline for {tenant}/{project_id}; run onboarding first.",
        )
    return baseline


def _ego(baseline: StoredBaseline, centers: list[str], depth: int) -> EgoGraph:
    try:
        return build_ego_graph(baseline.graph, centers, depth)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get("", response_model=InventoryReport)
def get_inventory(
    tenant: Tenant,
    project_id: ProjectId,
    format: Annotated[Literal["json", "csv"], Query()] = "json",
) -> InventoryReport | Response:
    """The newest baseline's inventory, as JSON or CSV."""
    baseline = _latest(tenant, project_id)
    report = build_inventory(baseline.graph, baseline.snapshot_id)
    if format == "csv":
        return Response(
            content=inventory_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="inventory-{tenant}-{project_id}-{baseline.snapshot_id}.csv"'
            },
        )
    return report


@router.get("/graph", response_model=EgoGraph)
def get_service_graph(
    tenant: Tenant, project_id: ProjectId, service: Annotated[str, Query(min_length=1)], depth: Depth = 2
) -> EgoGraph:
    """One service's dependency neighbourhood in the newest baseline."""
    return _ego(_latest(tenant, project_id), [service], depth)


@assessment_router.get("/{job_id}/graph", response_model=EgoGraph)
def get_assessment_graph(job_id: str, depth: Depth = 2) -> EgoGraph:
    """A pull request's changed services and their neighbourhood, in the baseline it was assessed against."""
    job = find_assessment(job_id)
    if job is None or job.assessment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No completed assessment {job_id}.")
    snapshot_id = job.assessment.evidence.baseline_snapshot_id
    if not snapshot_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The assessment used no baseline.")
    scope = BaselineScope(tenant=job.tenant, project_id=job.project_id)
    try:
        baseline = coordinator_nodes.BASELINE_STORE.read(scope, snapshot_id)
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=f"Baseline {snapshot_id} can't be read any more: {error}"
        ) from error
    return _ego(baseline, list(job.assessment.services_to_deploy), depth)
