"""FastAPI boundary that lets a frontend trigger the Day-Zero discovery engine."""

from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, status
from pydantic import BaseModel, Field

from src.discovery_engine.graph import discovery_app
from src.discovery_engine.state import DiscoveryGraphState
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor


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


class SystemVerification(BaseModel):
    """Outcome of a read-only access check against one integration."""

    status: bool
    message: str


class KubernetesVerificationRequest(BaseModel):
    """Namespaces whose read access should be checked."""

    target_namespaces: list[str] = Field(min_length=1)


def _to_verification(result: tuple[bool, str]) -> SystemVerification:
    status_ok, message = result
    return SystemVerification(status=status_ok, message=message)


def verify_devops() -> SystemVerification:
    """Run the Azure DevOps read-only access check."""
    try:
        return _to_verification(DevOpsExtractor().verify_access())
    except Exception as error:
        return SystemVerification(status=False, message=str(error))


def verify_kubernetes(namespaces: list[str]) -> SystemVerification:
    """Run the Kubernetes access check in every requested namespace."""
    try:
        extractor = KubernetesExtractor()
        failures: list[str] = []
        for namespace in namespaces:
            ok, message = extractor.verify_access(namespace)
            if not ok:
                failures.append(f"{namespace}: {message}")
        if failures:
            return SystemVerification(status=False, message="; ".join(failures))
        return SystemVerification(status=True, message="Success")
    except Exception as error:
        return SystemVerification(status=False, message=str(error))


def verify_telemetry() -> SystemVerification:
    """Run the telemetry read-only query check."""
    try:
        return _to_verification(TelemetryExtractor().verify_access())
    except Exception as error:
        return SystemVerification(status=False, message=str(error))


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


@app.post("/api/verify/devops", response_model=SystemVerification)
def verify_devops_endpoint() -> SystemVerification:
    """Run the pre-flight read-only access check for Azure DevOps."""
    return verify_devops()


@app.post("/api/verify/kubernetes", response_model=SystemVerification)
def verify_kubernetes_endpoint(
    payload: KubernetesVerificationRequest,
) -> SystemVerification:
    """Run the pre-flight read-only access check for Kubernetes namespaces."""
    return verify_kubernetes(list(payload.target_namespaces))


@app.post("/api/verify/telemetry", response_model=SystemVerification)
def verify_telemetry_endpoint() -> SystemVerification:
    """Run the pre-flight read-only access check for telemetry."""
    return verify_telemetry()
