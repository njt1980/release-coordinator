"""FastAPI boundary that lets a frontend trigger the Day-Zero discovery engine."""

import logging
from datetime import datetime
from typing import Literal, Self
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from src.core import config
from src.core.models import (
    IDENTITY_SEGMENT_PATTERN,
    UNKNOWN_LABEL,
    DependencyEdge,
    DiscoveryStageReport,
    PipelineVariables,
    RepositoryCoverage,
    RunEvent,
    ScannedRevision,
    ServiceNode,
    ShapeFinding,
    UnresolvedEntity,
)
from src.discovery_engine.graph import discovery_app
from src.discovery_engine.state import DiscoveryGraphState
from src.orchestration.narration import narrate_discovery
from src.orchestration.run_log import DEFAULT_RUN_LOGS, run_graph
from src.tools.baseline_store import (
    PATH_SEGMENT_PATTERN,
    BaselineScope,
    StoredBaseline,
    default_store,
)
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor
from src.assistant_api import router as assistant_router
from src.review_api import router as review_router
from src.runs import router as runs_router
from src.webhooks import router as webhooks_router


app = FastAPI(title="Release Coordinator API")
app.include_router(webhooks_router)
app.include_router(runs_router)
app.include_router(assistant_router)
app.include_router(review_router)

BASELINE_STORE = default_store()
RUN_LOGS = DEFAULT_RUN_LOGS
logger = logging.getLogger(__name__)


class OnboardingPayload(BaseModel):
    """Frontend request to onboard a workspace into the discovery engine.

    Every integration is optional, but at least one must be configured.
    Azure DevOps is configured when both the organization URL and project are
    given, Kubernetes when at least one namespace is given, and telemetry when
    a workspace ID is given. ``tenant`` and ``project_id`` select where the
    run's baseline snapshot is stored. ``environment`` and ``cluster`` name
    the deployment target being scanned; they are required, recorded in the
    snapshot and checked against the project's release policy.
    """

    tenant: str = Field(default=config.DEFAULT_TENANT, pattern=PATH_SEGMENT_PATTERN)
    project_id: str = Field(
        default=config.DEFAULT_PROJECT_ID, pattern=PATH_SEGMENT_PATTERN
    )
    environment: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    cluster: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
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

    @field_validator("environment", "cluster")
    @classmethod
    def _reject_unknown(cls, value: str) -> str:
        if value == UNKNOWN_LABEL:
            raise ValueError(f"must name a real deployment target, not {UNKNOWN_LABEL!r}")
        return value

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
        if self.has_devops and not self.target_repositories:
            raise ValueError(
                "Azure DevOps requires at least one repository to scan"
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
    ``environment``, ``cluster`` and ``scanned_revisions`` are what the
    snapshot scanned (None and empty for snapshots that predate scan
    recording); ``created_at`` is when it was written, if known.
    ``static_coverage``, ``pipeline_variables`` and ``kubernetes_shape``
    are the snapshot's coverage, pipeline and out-of-shape records (None
    when it predates them).
    """

    tenant: str
    project_id: str
    snapshot_id: str
    created_at: datetime | None = None
    demo_mode: bool
    environment: str | None = None
    cluster: str | None = None
    scanned_revisions: list[ScannedRevision] = Field(default_factory=list)
    service_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    services: list[ServiceNode]
    edges: list[DependencyEdge]
    unresolved_entities: list[UnresolvedEntity]
    discovery: list[DiscoveryStageReport]
    static_coverage: list[RepositoryCoverage] | None = None
    pipeline_variables: list[PipelineVariables] | None = None
    kubernetes_shape: list[ShapeFinding] | None = None


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
        server = extractor.api_server or "an unidentified API server"
        return SystemVerification(status=True, message=f"Success: read from {server}")
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
        "environment": payload.environment,
        "cluster": payload.cluster,
        "workspace_id": payload.workspace_id,
        "devops_org_url": payload.devops_org_url,
        "devops_project": payload.devops_project,
        "target_repositories": list(payload.target_repositories),
        "target_namespaces": list(payload.target_namespaces),
        "discovered_services": [],
        "service_environments": [],
        "static_edges": [],
        "runtime_edges": [],
        "discovery_reports": [],
        "scanned_revisions": [],
        "kubernetes_api_server": "",
        "telemetry_cluster": None,
        "static_coverage": [],
        "pipeline_variables": [],
        "kubernetes_shape": [],
        "manifest_edges": [],
        "static_config": [],
        "reconciled_graph": {},
        "baseline_snapshot_id": "",
    }


def summarize_baseline(baseline: StoredBaseline) -> CrawlResults:
    """Summarize a stored baseline snapshot for the frontend."""
    graph = baseline.graph
    scan = graph.scan
    return CrawlResults(
        tenant=baseline.scope.tenant,
        project_id=baseline.scope.project_id,
        snapshot_id=baseline.snapshot_id,
        created_at=baseline.created_at,
        demo_mode=baseline.demo_mode,
        environment=scan.environment if scan else None,
        cluster=scan.cluster if scan else None,
        scanned_revisions=list(scan.scanned_revisions) if scan else [],
        service_count=len(graph.services),
        edge_count=len(graph.edges),
        unresolved_count=len(graph.unresolved_entities),
        services=graph.services,
        edges=graph.edges,
        unresolved_entities=graph.unresolved_entities,
        discovery=graph.discovery or [],
        static_coverage=graph.static_coverage,
        pipeline_variables=graph.pipeline_variables,
        kubernetes_shape=graph.kubernetes_shape,
    )


def execute_discovery(
    state: DiscoveryGraphState, run_id: str | None = None
) -> CrawlResults:
    """Run the discovery graph and summarize the snapshot this run wrote.

    With ``run_id``, every step is narrated into that run's live log and,
    once the snapshot is written, the log is kept beside it.

    The run's own snapshot is read back by ID, not the project's latest, so
    a concurrent run for the same project cannot be reported in its place.

    Raises:
        RuntimeError: if the run finished without reporting a snapshot.
        DiscoveryIncompleteError, OSError, ValueError: if the snapshot is
            incomplete, missing or invalid.
    """
    if run_id is None:
        final_state = discovery_app.invoke(state)
    else:
        final_state = run_graph(discovery_app, state, narrate_discovery, RUN_LOGS, run_id)
    snapshot_id = (final_state or {}).get("baseline_snapshot_id")
    if not snapshot_id:
        raise RuntimeError("Discovery finished without writing a baseline snapshot")
    scope = BaselineScope(tenant=state["tenant"], project_id=state["project_id"])
    results = summarize_baseline(BASELINE_STORE.read(scope, snapshot_id))
    if run_id is not None:
        _keep_run_log(scope, snapshot_id, run_id)
    return results


def _keep_run_log(scope: BaselineScope, snapshot_id: str, run_id: str) -> None:
    """Save the run's log beside its snapshot; a failure here is only logged."""
    found = RUN_LOGS.get(run_id)
    if found is None:
        return
    try:
        BASELINE_STORE.write_run_log(scope, snapshot_id, found[1])
    except OSError as error:
        logger.warning("Could not keep the run log for %s: %s", snapshot_id, error)


async def run_discovery_job(job_id: str, state: DiscoveryGraphState) -> None:
    """Run the discovery graph for one job and record its outcome.

    The synchronous graph and the snapshot read run in the thread pool so
    the event loop stays free to answer status polls while the crawl is in
    progress.
    """
    _jobs[job_id] = JobRecord(job_id=job_id, status="running")
    try:
        results = await run_in_threadpool(execute_discovery, state, job_id)
    except Exception as error:
        message = str(error) or repr(error)
        _jobs[job_id] = JobRecord(job_id=job_id, status="failed", error_message=message)
        RUN_LOGS.finish(job_id, "failed", message)
    else:
        _jobs[job_id] = JobRecord(job_id=job_id, status="completed", results=results)
        RUN_LOGS.finish(
            job_id,
            "completed",
            f"Baseline {results.snapshot_id}: {results.service_count} services, "
            f"{results.edge_count} dependencies, {results.unresolved_count} unresolved",
        )


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
    RUN_LOGS.start(
        job_id,
        "onboarding",
        f"Onboard {payload.tenant}/{payload.project_id} "
        f"({payload.environment}/{payload.cluster})",
        payload.tenant,
        payload.project_id,
    )
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


@app.get(
    "/api/baselines/{tenant}/{project_id}/{snapshot_id}/run-log",
    response_model=list[RunEvent],
)
def get_baseline_run_log(tenant: str, project_id: str, snapshot_id: str) -> list[RunEvent]:
    """Return the log of the onboarding run that built a snapshot.

    404 when the snapshot predates run logs or its log wasn't kept.
    """
    try:
        scope = BaselineScope(tenant=tenant, project_id=project_id)
        events = BASELINE_STORE.read_run_log(scope, snapshot_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    if events is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No run log kept for snapshot {snapshot_id}",
        )
    return events


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
