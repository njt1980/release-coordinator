"""FastAPI boundary that lets a frontend trigger the Day-Zero discovery engine."""

from uuid import uuid4

from fastapi import FastAPI
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
    """Acknowledgement returned once the discovery graph has run."""

    status: str
    message: str
    job_id: str
    workspace_id: str


def build_discovery_state(payload: OnboardingPayload) -> DiscoveryGraphState:
    """Map an onboarding payload onto the initial discovery graph state."""
    return {
        "target_repositories": list(payload.target_repositories),
        "target_namespaces": list(payload.target_namespaces),
        "discovered_services": [],
        "static_edges": [],
        "runtime_edges": [],
        "reconciled_graph": {},
    }


@app.post("/api/onboard", response_model=OnboardingResponse)
def onboard(payload: OnboardingPayload) -> OnboardingResponse:
    """Run the discovery graph for the requested scope and return a mock job ID."""
    discovery_app.invoke(build_discovery_state(payload))
    return OnboardingResponse(
        status="success",
        message="Discovery engine run completed.",
        job_id=f"job-{uuid4()}",
        workspace_id=payload.workspace_id,
    )
