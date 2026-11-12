"""FastAPI boundary that lets a frontend trigger the Day-Zero discovery engine."""

import json
from typing import Literal, Self
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from src.core.models import (
    DependencyEdge,
    DiscoveryStageReport,
    KnowledgeGraph,
    ServiceNode,
    baseline_problems,
)
from src.discovery_engine.graph import discovery_app
from src.discovery_engine.nodes import KNOWLEDGE_GRAPH_PATH, DiscoveryIncompleteError
from src.discovery_engine.state import DiscoveryGraphState
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor


app = FastAPI(title="Release Coordinator API")


class OnboardingPayload(BaseModel):
    """Frontend request to onboard a workspace into the discovery engine.

    Every integration is optional, but at least one must be configured.
    Azure DevOps is configured when both the organization URL and project are
    given, Kubernetes when at least one namespace is given, and telemetry when
    a workspace ID is given.
    """

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
    """Knowledge graph produced by a completed discovery run.

    Carries the discovered services and dependency edges alongside their
    counts so the frontend can show what was extracted, plus the outcome of
    each discovery stage.
    """

    service_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    services: list[ServiceNode]
    edges: list[DependencyEdge]
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
    }


def summarize_knowledge_graph() -> CrawlResults:
    """Load the services and dependency edges from the persisted knowledge graph.

    Raises if the file is missing, does not contain valid ``services`` and
    ``edges`` lists, or does not record a complete discovery, so a run without
    a trustworthy baseline is not reported as completed.
    """
    raw_graph = json.loads(KNOWLEDGE_GRAPH_PATH.read_text(encoding="utf-8"))
    graph = KnowledgeGraph.model_validate(raw_graph)
    if graph.discovery is None:
        raise DiscoveryIncompleteError(
            ["knowledge graph does not record discovery outcomes"]
        )
    problems = baseline_problems(graph.discovery)
    if problems:
        raise DiscoveryIncompleteError(problems)
    return CrawlResults(
        service_count=len(graph.services),
        edge_count=len(graph.edges),
        services=graph.services,
        edges=graph.edges,
        discovery=graph.discovery,
    )


def execute_discovery(state: DiscoveryGraphState) -> CrawlResults:
    """Run the discovery graph to completion and summarize what it persisted."""
    discovery_app.invoke(state)
    return summarize_knowledge_graph()


async def run_discovery_job(job_id: str, state: DiscoveryGraphState) -> None:
    """Run the discovery graph for one job and record its outcome.

    The synchronous graph and the graph file read run in the thread pool so
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
