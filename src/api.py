"""FastAPI boundary that lets a frontend trigger the Day-Zero discovery engine."""

from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, status
from pydantic import BaseModel, Field

from src.discovery_engine.graph import discovery_app
from src.discovery_engine.state import DiscoveryGraphState


app = FastAPI(title="Release Coordinator API")


class OnboardingPayload(BaseModel):
    """Frontend request to onboard a workspace into the discovery engine."""

    target_repositories: list[str] = Field(min_length=1)
    target_namespaces: list[str] = Field(min_length=1)
    workspace_id: str = Field(min_length=1)


class OnboardingResponse(BaseModel):
    """Acknowledgement returned once a discovery run has been scheduled."""

    status: str
    message: str
    job_id: str
    workspace_id: str


def build_discovery_state(payload: OnboardingPayload) -> DiscoveryGraphState:
    """Map an onboarding payload onto the initial discovery graph state."""
    return {
        "workspace_id": payload.workspace_id,
        "target_repositories": list(payload.target_repositories),
        "target_namespaces": list(payload.target_namespaces),
        "discovered_services": [],
        "static_edges": [],
        "runtime_edges": [],
        "reconciled_graph": {},
    }


@app.post(
    "/api/onboard",
    response_model=OnboardingResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def onboard(
    payload: OnboardingPayload, background_tasks: BackgroundTasks
) -> OnboardingResponse:
    """Schedule a discovery graph run for the requested scope and return at once."""
    background_tasks.add_task(discovery_app.invoke, build_discovery_state(payload))
    return OnboardingResponse(
        status="accepted",
        message="Discovery engine run scheduled.",
        job_id=str(uuid4()),
        workspace_id=payload.workspace_id,
    )
