"""FastAPI boundary that lets a frontend trigger the Day-Zero discovery engine."""

from typing import Literal, Self
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from src.core import config
from src.core.models import (
    DependencyEdge,
    DiscoveryStageReport,
    ServiceNode,
    UnresolvedEntity,
)
from src.discovery_engine.graph import discovery_app
from src.discovery_engine.state import DiscoveryGraphState
from src.tools.baseline_store import (
    PATH_SEGMENT_PATTERN,
    BaselineScope,
    StoredBaseline,
    default_store,
)
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor
from src.webhooks import router as webhooks_router


app = FastAPI(title="Release Coordinator API")
app.include_router(webhooks_router)

BASELINE_STORE = default_store()


class OnboardingPayload(BaseModel):
    """Frontend request to onboard a workspace into the discovery engine.

    Every integration is optional, but at least one must be configured.
    Azure DevOps is configured when both the organization URL and project are
    given, Kubernetes when at least one namespace is given, and telemetry when
    a workspace ID is given. ``tenant`` and ``project_id`` select where the
    run's baseline snapshot is stored.
    """

    tenant: str = Field(default=config.DEFAULT_TENANT, pattern=PATH_SEGMENT_PATTERN)
    project_id: str = Field(
        default=config.DEFAULT_PROJECT_ID, pattern=PATH_SEGMENT_PATTERN
    )
    devops_org_url: str = ""
    devops_project: str = ""
    target_repositories: list[str] = Field(default_factory=list)
    target_namespaces: list[str] = Field(default_factory=list)
    workspace_id: str = ""

    @field_validator("devops_org_url", "devops_project", "workspace_id")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("target_repositories", "target_namespaces")
    @classmethod
    def _drop_blank_entries(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]

    @property
    def has_devops(self) -> bool:
        """Whether a complete Azure DevOps scope was supplied."""
        return bool(self.devops_org_url and self.devops_project)

    @model_validator(mode="after")
    def _require_coherent_scope(self) -> Self:
        if bool(self.devops_org_url) != bool(self.devops_project):
            raise ValueError(
                "Azure DevOps requires both an organization URL and a project"
            )
        if self.target_repositories and not self.has_devops:
            raise ValueError(
                "Repositories require an Azure DevOps organization URL and project"
            )
        if not (self.has_devops or self.target_namespaces or self.workspace_id):
            raise ValueError(
                "Configure at least one integration: Azure DevOps, "
                "Kubernetes, or Telemetry"
            )
        return self


class OnboardingResponse(BaseModel):
    """Acknowledgement returned once a discovery run has been scheduled."""

    status: str
    message: str
    job_id: str
    workspace_id: str


JobStatus = Literal["queued", "running", "completed", "failed"]


class CrawlResults(BaseModel):
    """One stored baseline snapshot, summarized for the frontend.

    Carries the discovered services and dependency edges alongside their
    counts so the frontend can show what was extracted, the outcome of each
    discovery stage, and which snapshot this is. Service IDs are FQURIs;
    ``unresolved_entities`` lists endpoints that matched no registered
    service. ``demo_mode`` is true when
    the baseline relies on mock fixture data rather than real evidence.
    """

    tenant: str
    project_id: str
    snapshot_id: str
    demo_mode: bool
    service_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    services: list[ServiceNode]
    edges: list[DependencyEdge]
    unresolved_entities: list[UnresolvedEntity]
    discovery: list[DiscoveryStageReport]


class JobRecord(BaseModel):
    """Current state of one scheduled discovery run."""

    job_id: str
    status: JobStatus
    error_message: str | None = None
    results: CrawlResults | None = None


# In-memory job registry. Records are lost when the server restarts. Only
# mutated from the event loop, so no locking is needed.
_jobs: dict[str, JobRecord] = {}


class SystemVerification(BaseModel):
    """Outcome of a read-only access check against one integration."""

    status: bool
    message: str


class DevOpsVerificationRequest(BaseModel):
    """Azure DevOps scope whose read access should be checked."""

    devops_org_url: str = Field(min_length=1)
    devops_project: str = Field(min_length=1)
    target_repositories: list[str] = Field(default_factory=list)


class KubernetesVerificationRequest(BaseModel):
    """Namespaces whose read access should be checked."""

    target_namespaces: list[str] = Field(min_length=1)


class TelemetryVerificationRequest(BaseModel):
    """Log Analytics workspace whose query access should be checked."""

    workspace_id: str = Field(min_length=1)

    @field_validator("workspace_id")
    @classmethod
    def _strip_workspace_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("workspace_id must not be blank")
        return stripped


def _to_verification(result: tuple[bool, str]) -> SystemVerification:
    status_ok, message = result
    return SystemVerification(status=status_ok, message=message)


def verify_devops(payload: DevOpsVerificationRequest) -> SystemVerification:
    """Run the Azure DevOps read-only access check for the requested scope."""
    try:
        extractor = DevOpsExtractor(
            org_url=payload.devops_org_url,
            project=payload.devops_project,
            repositories=list(payload.target_repositories),
        )
        return _to_verification(extractor.verify_access())
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


def verify_telemetry(workspace_id: str) -> SystemVerification:
    """Run the telemetry read-only query check against one workspace."""
    try:
        extractor = TelemetryExtractor(workspace_id=workspace_id)
        return _to_verification(extractor.verify_access())
    except Exception as error:
        return SystemVerification(status=False, message=str(error))


def build_discovery_state(payload: OnboardingPayload) -> DiscoveryGraphState:
    """Map an onboarding payload onto the initial discovery graph state."""
    return {
        "tenant": payload.tenant,
        "project_id": payload.project_id,
        "workspace_id": payload.workspace_id,
        "devops_org_url": payload.devops_org_url,
        "devops_project": payload.devops_project,
        "target_repositories": list(payload.target_repositories),
        "target_namespaces": list(payload.target_namespaces),
        "discovered_services": [],
        "static_edges": [],
        "runtime_edges": [],
        "discovery_reports": [],
        "reconciled_graph": {},
        "baseline_snapshot_id": "",
    }


def summarize_baseline(baseline: StoredBaseline) -> CrawlResults:
    """Summarize a stored baseline snapshot for the frontend."""
    graph = baseline.graph
    return CrawlResults(
        tenant=baseline.scope.tenant,
        project_id=baseline.scope.project_id,
        snapshot_id=baseline.snapshot_id,
        demo_mode=baseline.demo_mode,
        service_count=len(graph.services),
        edge_count=len(graph.edges),
        unresolved_count=len(graph.unresolved_entities),
        services=graph.services,
        edges=graph.edges,
        unresolved_entities=graph.unresolved_entities,
        discovery=graph.discovery or [],
    )


def execute_discovery(state: DiscoveryGraphState) -> CrawlResults:
    """Run the discovery graph and summarize the snapshot this run wrote.

    The run's own snapshot is read back by ID, not the project's latest, so
    a concurrent run for the same project cannot be reported in its place.

    Raises:
        RuntimeError: if the run finished without reporting a snapshot.
        DiscoveryIncompleteError, OSError, ValueError: if the snapshot is
            incomplete, missing or invalid.
    """
    final_state = discovery_app.invoke(state)
    snapshot_id = (final_state or {}).get("baseline_snapshot_id")
    if not snapshot_id:
        raise RuntimeError("Discovery finished without writing a baseline snapshot")
    scope = BaselineScope(tenant=state["tenant"], project_id=state["project_id"])
    return summarize_baseline(BASELINE_STORE.read(scope, snapshot_id))


async def run_discovery_job(job_id: str, state: DiscoveryGraphState) -> None:
    """Run the discovery graph for one job and record its outcome.

    The synchronous graph and the snapshot read run in the thread pool so
    the event loop stays free to answer status polls while the crawl is in
    progress.
    """
    _jobs[job_id] = JobRecord(job_id=job_id, status="running")
    try:
        results = await run_in_threadpool(execute_discovery, state)
    except Exception as error:
        _jobs[job_id] = JobRecord(
            job_id=job_id, status="failed", error_message=str(error) or repr(error)
        )
    else:
        _jobs[job_id] = JobRecord(job_id=job_id, status="completed", results=results)


@app.post(
    "/api/onboard",
    response_model=OnboardingResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def onboard(
    payload: OnboardingPayload, background_tasks: BackgroundTasks
) -> OnboardingResponse:
    """Schedule a discovery graph run for the requested scope and return at once."""
    job_id = str(uuid4())
    _jobs[job_id] = JobRecord(job_id=job_id, status="queued")
    background_tasks.add_task(
        run_discovery_job, job_id, build_discovery_state(payload)
    )
    return OnboardingResponse(
        status="accepted",
        message="Discovery engine run scheduled.",
        job_id=job_id,
        workspace_id=payload.workspace_id,
    )


@app.get("/api/onboard/{job_id}", response_model=JobRecord)
def get_onboarding_job(job_id: str) -> JobRecord:
    """Return the current status of a scheduled discovery run."""
    record = _jobs.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job ID"
        )
    return record


@app.get(
    "/api/baselines/{tenant}/{project_id}/latest", response_model=CrawlResults
)
def get_latest_baseline(tenant: str, project_id: str) -> CrawlResults:
    """Return the project's newest complete baseline, including demo mode."""
    try:
        scope = BaselineScope(tenant=tenant, project_id=project_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    baseline = BASELINE_STORE.latest(scope)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No complete baseline for {tenant}/{project_id}",
        )
    return summarize_baseline(baseline)


@app.post("/api/verify/devops", response_model=SystemVerification)
def verify_devops_endpoint(payload: DevOpsVerificationRequest) -> SystemVerification:
    """Run the pre-flight read-only access check for Azure DevOps."""
    return verify_devops(payload)


@app.post("/api/verify/kubernetes", response_model=SystemVerification)
def verify_kubernetes_endpoint(
    payload: KubernetesVerificationRequest,
) -> SystemVerification:
    """Run the pre-flight read-only access check for Kubernetes namespaces."""
    return verify_kubernetes(list(payload.target_namespaces))


@app.post("/api/verify/telemetry", response_model=SystemVerification)
def verify_telemetry_endpoint(
    payload: TelemetryVerificationRequest,
) -> SystemVerification:
    """Run the pre-flight read-only access check for telemetry."""
    return verify_telemetry(payload.workspace_id)
