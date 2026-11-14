"""Service inventory API.

``GET /api/v1/projects/{tenant}/{project_id}/inventory`` returns the
newest baseline's service inventory, most critical first
(``orchestration.inventory``); ``?format=csv`` returns it as a CSV file.
Read-only.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from fastapi.responses import Response

from src.coordinator import nodes as coordinator_nodes
from src.orchestration.inventory import InventoryReport, build_inventory, inventory_csv
from src.tools.baseline_store import PATH_SEGMENT_PATTERN, BaselineScope

router = APIRouter(prefix="/api/v1/projects/{tenant}/{project_id}/inventory", tags=["inventory"])

Tenant = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]
ProjectId = Annotated[str, Path(pattern=PATH_SEGMENT_PATTERN)]


@router.get("", response_model=InventoryReport)
def get_inventory(
    tenant: Tenant,
    project_id: ProjectId,
    format: Annotated[Literal["json", "csv"], Query()] = "json",
) -> InventoryReport | Response:
    """The newest baseline's inventory, as JSON or CSV."""
    baseline = coordinator_nodes.BASELINE_STORE.latest(BaselineScope(tenant=tenant, project_id=project_id))
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No complete discovery baseline for {tenant}/{project_id}; run onboarding first.",
        )
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
