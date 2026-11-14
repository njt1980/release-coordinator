"""PR webhook: map a pull request's changed files to services and assess it.

``POST /api/v1/webhooks/pr-assessment`` maps the changed files to registered
services using the project's newest complete discovery baseline, then
schedules a release coordinator run for those services, pinned to that same
snapshot, and returns at once.
``GET /api/v1/webhooks/pr-assessment/{job_id}`` reports the run's outcome.
"""

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from src.coordinator.file_mapping import ChangedFileMapping, map_changed_files
from src.coordinator.graph import app as coordinator_app
from src.coordinator.project_policy import partition_exempt_files
from src.coordinator.state import ReleaseGraphState
from src.core import config
from src.core.models import (
    ContractChange,
    Exemption,
    PullRequestPayload,
    ReleaseAssessment,
)
from src.orchestration.narration import narrate_assessment
from src.orchestration.run_log import DEFAULT_RUN_LOGS, run_graph
from src.tools.baseline_store import PATH_SEGMENT_PATTERN, BaselineScope, default_store
from src.tools.policy_store import (
    PolicyInvalidError,
    PolicyMissingError,
    ProjectPolicy,
    default_policy_store,
)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

BASELINE_STORE = default_store()
POLICY_STORE = default_policy_store()

# Full SHA-1 or SHA-256 commit IDs only: source control verifies them, and
# Azure DevOps rejects abbreviated IDs.
_GIT_SHA_PATTERN = r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$"


class PullRequestWebhook(BaseModel):
    """Generic pull-request webhook payload.

    ``changed_files`` are paths relative to the root of ``repository``.
    ``change_type`` (for example taken from a PR label) decides the
    deterministic deployment order. ``diff`` is what the risk reasoner
    reads. ``tenant`` and ``project_id`` select the discovery baseline.
    """

    pr_id: str = Field(min_length=1)
    base_sha: str = Field(pattern=_GIT_SHA_PATTERN)
    head_sha: str = Field(pattern=_GIT_SHA_PATTERN)
    repository: str = Field(min_length=1)
    changed_files: list[str] = Field(min_length=1)
    change_type: ContractChange
    diff: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    source_branch: str = ""
    target_branch: str = "main"
    tenant: str = Field(default=config.DEFAULT_TENANT, pattern=PATH_SEGMENT_PATTERN)
    project_id: str = Field(
        default=config.DEFAULT_PROJECT_ID, pattern=PATH_SEGMENT_PATTERN
    )
    workspace_id: str = ""

    @field_validator("changed_files")
    @classmethod
    def _drop_blank_files(cls, values: list[str]) -> list[str]:
        files = [value.strip() for value in values if value.strip()]
        if not files:
            raise ValueError("changed_files must list at least one path")
        return files


class PrAssessmentAccepted(BaseModel):
    """Acknowledgement for a scheduled PR assessment.

    ``unmapped_files`` are changed files no registered service owns and the
    project policy does not exempt; ``exempted_files`` are those it exempts.
    Without a usable policy nothing is exempt.
    """

    job_id: str
    pr_id: str
    baseline_snapshot_id: str
    target_services: list[str] = Field(min_length=1)
    files_by_service: dict[str, list[str]]
    unmapped_files: list[str]
    exempted_files: list[Exemption] = Field(default_factory=list)


AssessmentJobStatus = Literal["queued", "running", "completed", "failed"]


class PrAssessmentJob(BaseModel):
    """Current state of one scheduled PR assessment."""

    job_id: str
    pr_id: str
    status: AssessmentJobStatus
    error_message: str | None = None
    assessment: ReleaseAssessment | None = None
    evidence_report: str | None = None


# In-memory job registry; records are lost when the server restarts. Only
# mutated from the event loop, so no locking is needed.
_assessment_jobs: dict[str, PrAssessmentJob] = {}
RUN_LOGS = DEFAULT_RUN_LOGS


def find_assessment(job_id: str) -> PrAssessmentJob | None:
    """Return a PR assessment job's current record, or None if unknown."""
    return _assessment_jobs.get(job_id)


def _load_policy(scope: BaselineScope) -> ProjectPolicy | None:
    """Return the project's policy, or None when it is missing or invalid.

    The assessment itself reports why (POLICY_MISSING or POLICY_INVALID);
    here a missing policy only means nothing is exempt.
    """
    try:
        return POLICY_STORE.load(scope).policy
    except (PolicyMissingError, PolicyInvalidError):
        return None


def build_coordinator_state(
    payload: PullRequestWebhook,
    snapshot_id: str,
    mapping: ChangedFileMapping,
) -> ReleaseGraphState:
    """Map a webhook payload and its file mapping onto coordinator state.

    The run is pinned to ``snapshot_id``, the baseline the files were mapped
    against, so a discovery run finishing meanwhile cannot change the graph
    being assessed. Revisions, the full changed-file list and the unmapped
    files are carried through to the assessment.
    """
    pull_request = PullRequestPayload(
        pr_id=payload.pr_id,
        title=payload.title or f"PR {payload.pr_id}",
        target_service=mapping.target_services[0],
        change_type=payload.change_type,
        source_branch=payload.source_branch,
        target_branch=payload.target_branch,
        description=payload.description,
        diff=payload.diff,
        repository=payload.repository,
        base_sha=payload.base_sha,
        head_sha=payload.head_sha,
        changed_files=list(payload.changed_files),
    )
    return {
        "trigger_event": pull_request.model_dump(mode="json"),
        "target_services": list(mapping.target_services),
        "unmapped_files": list(mapping.unmapped_files),
        "baseline_snapshot_id": snapshot_id,
        "workspace_id": payload.workspace_id.strip(),
        "tenant": payload.tenant,
        "project_id": payload.project_id,
    }


async def run_assessment_job(
    job_id: str, pr_id: str, state: ReleaseGraphState
) -> None:
    """Run the release coordinator for one job and record its outcome.

    The synchronous graph runs in the thread pool so status polls are
    answered while the LLM call is in progress.
    """
    _assessment_jobs[job_id] = PrAssessmentJob(
        job_id=job_id, pr_id=pr_id, status="running"
    )
    try:
        final_state = await run_in_threadpool(
            run_graph, coordinator_app, state, narrate_assessment, RUN_LOGS, job_id
        )
        assessment = final_state["release_assessment"]
    except Exception as error:
        message = str(error) or repr(error)
        _assessment_jobs[job_id] = PrAssessmentJob(
            job_id=job_id,
            pr_id=pr_id,
            status="failed",
            error_message=message,
        )
        RUN_LOGS.finish(job_id, "failed", message)
    else:
        RUN_LOGS.finish(
            job_id,
            "completed",
            "Approved"
            if assessment.approved
            else "Not approved: "
            + (", ".join(code.value for code in assessment.reason_codes) or "no reason given"),
        )
        _assessment_jobs[job_id] = PrAssessmentJob(
            job_id=job_id,
            pr_id=pr_id,
            status="completed",
            assessment=assessment,
            evidence_report=final_state.get("evidence_report"),
        )


@router.post(
    "/pr-assessment",
    response_model=PrAssessmentAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def pr_assessment_webhook(
    payload: PullRequestWebhook, background_tasks: BackgroundTasks
) -> PrAssessmentAccepted:
    """Map the PR's changed files to services and schedule its assessment.

    Responds 409 when the project has no complete baseline and 422 when no
    changed file belongs to a registered service. When only some files map,
    the assessment is scheduled and the rest are reported as unmapped, which
    makes the verdict non-actionable. The assessment is pinned to the
    baseline snapshot the files were mapped against.
    """
    scope = BaselineScope(tenant=payload.tenant, project_id=payload.project_id)
    baseline = BASELINE_STORE.latest(scope)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No complete discovery baseline for {scope.tenant}/"
                f"{scope.project_id}; run discovery first."
            ),
        )
    mapping = map_changed_files(
        baseline.graph, payload.repository, payload.changed_files
    )
    if not mapping.target_services:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": (
                    f"No changed file maps to a registered service in "
                    f"repository {payload.repository!r} (baseline "
                    f"{baseline.snapshot_id})."
                ),
                "unmapped_files": mapping.unmapped_files,
            },
        )
    unmapped, exempted = partition_exempt_files(
        _load_policy(scope), mapping.unmapped_files
    )
    job_id = str(uuid4())
    _assessment_jobs[job_id] = PrAssessmentJob(
        job_id=job_id, pr_id=payload.pr_id, status="queued"
    )
    RUN_LOGS.start(
        job_id,
        "assessment",
        f"Assess {payload.pr_id}" + (f": {payload.title}" if payload.title else ""),
        payload.tenant,
        payload.project_id,
    )
    background_tasks.add_task(
        run_assessment_job,
        job_id,
        payload.pr_id,
        build_coordinator_state(payload, baseline.snapshot_id, mapping),
    )
    return PrAssessmentAccepted(
        job_id=job_id,
        pr_id=payload.pr_id,
        baseline_snapshot_id=baseline.snapshot_id,
        target_services=mapping.target_services,
        files_by_service=mapping.files_by_service,
        unmapped_files=unmapped,
        exempted_files=exempted,
    )


@router.get("/pr-assessment/{job_id}", response_model=PrAssessmentJob)
def get_pr_assessment(job_id: str) -> PrAssessmentJob:
    """Return the current status of a scheduled PR assessment."""
    record = _assessment_jobs.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job ID"
        )
    return record
