"""PR webhook: map a pull request's changed files to services and assess it.

``POST /api/v1/webhooks/pr-assessment`` maps the changed files to registered
services using the project's newest complete discovery baseline, then
schedules a release coordinator run for those services, pinned to that same
snapshot, and returns at once.
``GET /api/v1/webhooks/pr-assessment/{job_id}`` reports the run's outcome.
``POST /api/v1/webhooks/pr-intake`` does the same for a pull request named
only by its Azure DevOps ID, read (read-only) from Azure DevOps.
"""

from collections.abc import Callable, Mapping
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
import logging

from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from src.coordinator.file_mapping import ChangedFileMapping, map_changed_files
from src.coordinator.graph import app as coordinator_app
from src.coordinator.project_policy import partition_exempt_files
from src.coordinator.sequencing import ReleaseOrderFacts, order_facts
from src.coordinator.state import ReleaseGraphState
from src.core import config
from src.core.models import (
    ContractChange,
    Exemption,
    PullRequestPayload,
    ReleaseAssessment,
)
from src.orchestration.briefing import ReleaseBriefing, build_briefing, render_markdown
from src.orchestration.narration import narrate_assessment
from src.orchestration.run_log import DEFAULT_RUN_LOGS, run_graph
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.devops_pr_feedback import (
    AssessmentOutcome,
    DevOpsPullRequestWriter,
    FeedbackResult,
    PullRequestTarget,
    post_feedback,
)
from src.tools.assessment_archive import default_archive
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
    """Current state of one scheduled PR assessment.

    ``tenant`` and ``project_id`` are the project it was assessed for
    (records archived before they were kept read as the defaults).
    ``order`` is what the deployment order was computed from, so the
    assessment can join a release train (None for older records).
    ``source`` is the Azure DevOps pull request iteration it was read from
    (None for webhook payloads), and ``ado_feedback`` what was written back
    to it (None when the policy asks for nothing).
    """

    job_id: str
    pr_id: str
    status: AssessmentJobStatus
    tenant: str = config.DEFAULT_TENANT
    project_id: str = config.DEFAULT_PROJECT_ID
    error_message: str | None = None
    assessment: ReleaseAssessment | None = None
    evidence_report: str | None = None
    briefing: ReleaseBriefing | None = None
    order: ReleaseOrderFacts | None = None
    source: PullRequestTarget | None = None
    ado_feedback: FeedbackResult | None = None


class PrAssessmentSummary(BaseModel):
    """One PR assessment in a list: enough to choose which to open.

    ``tenant`` and ``project_id`` are the project it was assessed for, so a
    page can link to that project's other views (e.g. the telemetry gaps).
    """

    job_id: str
    pr_id: str
    tenant: str = config.DEFAULT_TENANT
    project_id: str = config.DEFAULT_PROJECT_ID
    status: AssessmentJobStatus
    headline: str = ""
    decisions_needed: int = 0
    evidence_fixes: int = 0


# In-memory job registry; records are lost when the server restarts. Only
# mutated from the event loop, so no locking is needed.
_assessment_jobs: dict[str, PrAssessmentJob] = {}
RUN_LOGS = DEFAULT_RUN_LOGS
# Finished assessments on disk (None: kept in memory only; see config).
ASSESSMENT_ARCHIVE = default_archive()
_archive_restored = False
logger = logging.getLogger(__name__)


def _restore_archive() -> None:
    """Load archived assessments once, so they survive a restart."""
    global _archive_restored
    if _archive_restored or ASSESSMENT_ARCHIVE is None:
        return
    _archive_restored = True
    for job_id, text in ASSESSMENT_ARCHIVE.load_all():
        if job_id in _assessment_jobs:
            continue
        try:
            _assessment_jobs[job_id] = PrAssessmentJob.model_validate_json(text)
        except ValidationError as error:
            logger.warning("Archived assessment %s is invalid: %s", job_id, error)


def _finish(record: PrAssessmentJob) -> None:
    """Record a finished job, and archive it when an archive is configured."""
    _assessment_jobs[record.job_id] = record
    if ASSESSMENT_ARCHIVE is None:
        return
    try:
        ASSESSMENT_ARCHIVE.save(record.job_id, record.model_dump_json())
    except (OSError, ValueError) as error:
        logger.warning("Assessment %s not archived: %s", record.job_id, error)


def find_assessment(job_id: str) -> PrAssessmentJob | None:
    """Return a PR assessment job's current record, or None if unknown."""
    _restore_archive()
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
    job_id: str,
    pr_id: str,
    state: ReleaseGraphState,
    source: PullRequestTarget | None = None,
) -> None:
    """Run the release coordinator for one job and record its outcome.

    The synchronous graph runs in the thread pool so status polls are
    answered while the LLM call is in progress. For a pull request read
    from Azure DevOps (``source``), the outcome is then written back to it
    when the project policy asks for that (see ``publish_feedback``).
    """
    tenant = state.get("tenant") or config.DEFAULT_TENANT
    project_id = state.get("project_id") or config.DEFAULT_PROJECT_ID
    _assessment_jobs[job_id] = PrAssessmentJob(
        job_id=job_id,
        pr_id=pr_id,
        status="running",
        tenant=tenant,
        project_id=project_id,
        source=source,
    )
    try:
        final_state = await run_in_threadpool(
            run_graph, coordinator_app, state, narrate_assessment, RUN_LOGS, job_id
        )
        assessment = final_state["release_assessment"]
    except Exception as error:
        message = str(error) or repr(error)
        _finish(
            PrAssessmentJob(
                job_id=job_id,
                pr_id=pr_id,
                status="failed",
                error_message=message,
                tenant=tenant,
                project_id=project_id,
                source=source,
            )
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
        _finish(
            PrAssessmentJob(
                job_id=job_id,
                pr_id=pr_id,
                status="completed",
                assessment=assessment,
                evidence_report=final_state.get("evidence_report"),
                briefing=build_briefing(assessment),
                tenant=tenant,
                project_id=project_id,
                order=_order_facts(final_state),
                source=source,
            )
        )
    if source is not None:
        await run_in_threadpool(publish_feedback, job_id)


# Makes the git client feedback is written with, per organization URL
# (replaced in tests).
FeedbackClientFactory = Callable[[str], Any]


def _writer_client(organization_url: str) -> Any:
    return DevOpsPullRequestWriter(organization_url).git_client()


FEEDBACK_CLIENT: FeedbackClientFactory = _writer_client


def _outcome(record: PrAssessmentJob) -> AssessmentOutcome:
    briefing = record.briefing
    return AssessmentOutcome(
        job_id=record.job_id,
        failed=record.status == "failed",
        approved=bool(record.assessment and record.assessment.approved),
        actionable=bool(record.assessment and record.assessment.actionable),
        headline=briefing.headline if briefing else "",
        briefing_markdown=render_markdown(briefing) if briefing else "",
    )


def publish_feedback(job_id: str) -> FeedbackResult | None:
    """Write a finished assessment back to its Azure DevOps pull request,
    as far as the project policy's ``ado_connection`` asks (nothing by
    default), and record what was written on the job.

    The pull request must be in the policy's ``source_control`` project:
    nothing is written anywhere else. Never raises.
    """
    record = _assessment_jobs.get(job_id)
    if record is None or record.source is None or record.status not in ("completed", "failed"):
        return None
    policy = _load_policy(BaselineScope(tenant=record.tenant, project_id=record.project_id))
    if policy is None or policy.source_control is None or not policy.ado_connection.writes:
        return None
    source, control = record.source, policy.source_control
    if (source.organization_url.rstrip("/").lower(), source.project) != (
        control.organization_url.lower(),
        control.project,
    ):
        result = FeedbackResult(errors=["pull request is outside the policy's source_control project"])
    else:
        connection = policy.ado_connection
        try:
            client = FEEDBACK_CLIENT(control.organization_url)
        except Exception as error:
            logger.warning("No Azure DevOps client for feedback on %s: %s", job_id, error)
            result = FeedbackResult(errors=[f"client: {error}"])
        else:
            result = post_feedback(
                client,
                source,
                _outcome(record),
                status=connection.status,
                comment=connection.comment,
                link_base_url=connection.link_base_url,
            )
    _finish(record.model_copy(update={"ado_feedback": result}))
    return result


def _order_facts(final_state: Mapping[str, Any]) -> ReleaseOrderFacts | None:
    """What the run's deployment order was computed from (None without one)."""
    plan = final_state.get("deployment_plan")
    if plan is None or not plan.services_to_deploy:
        return None
    return order_facts(
        plan.change_type,
        plan.services_to_deploy,
        list(final_state.get("upstream_services", [])),
        list(final_state.get("downstream_services", [])),
        final_state.get("matched_edges", []),
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
    baseline snapshot the files were mapped against. Responds 403 where the
    deployment disables it (``RC_DISABLE_RAW_WEBHOOK``): use PR intake.
    """
    if config.DISABLE_RAW_WEBHOOK:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This server reads pull requests from Azure DevOps only; use PR intake.",
        )
    return schedule_assessment(payload, background_tasks)


def schedule_assessment(
    payload: PullRequestWebhook,
    background_tasks: BackgroundTasks,
    source: PullRequestTarget | None = None,
) -> PrAssessmentAccepted:
    """Map a pull request's changed files and schedule its assessment
    (see ``pr_assessment_webhook`` for the responses)."""
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
    _restore_archive()  # Archived jobs list before new ones.
    _assessment_jobs[job_id] = PrAssessmentJob(
        job_id=job_id,
        pr_id=payload.pr_id,
        status="queued",
        tenant=payload.tenant,
        project_id=payload.project_id,
        source=source,
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
        source,
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


class PrIntakeRequest(BaseModel):
    """Assess a pull request by its Azure DevOps ID.

    Everything else (repository, commits, changed files, diff) is read
    from Azure DevOps through the project policy's ``source_control``.
    ``change_type`` is the coordinator's call, as in the webhook.
    """

    pr_id: int = Field(gt=0)
    change_type: ContractChange
    tenant: str = Field(default=config.DEFAULT_TENANT, pattern=PATH_SEGMENT_PATTERN)
    project_id: str = Field(default=config.DEFAULT_PROJECT_ID, pattern=PATH_SEGMENT_PATTERN)
    workspace_id: str = ""


class PrIntakeAccepted(PrAssessmentAccepted):
    """A scheduled assessment of a pull request read from Azure DevOps.

    ``skipped_files`` changed but are not in the diff the risk reasoner
    reads (binary, oversized, or past the size limits; ``diff_truncated``).
    """

    repository: str
    title: str = ""
    base_sha: str
    head_sha: str
    diff_truncated: bool = False
    skipped_files: list[str] = Field(default_factory=list)


_EMPTY_DIFF = "(no text changes: every changed file is binary or too large to diff)"


@router.post(
    "/pr-intake",
    response_model=PrIntakeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def pr_intake(request: PrIntakeRequest, background_tasks: BackgroundTasks) -> PrIntakeAccepted:
    """Read a pull request from Azure DevOps and schedule its assessment.

    Read-only: the PR's latest iteration is read with the project's
    source-control identity; nothing is written back. Responds 409 when the
    project policy names no ``source_control``, 502 when the pull request
    can't be read, 422 when it changes no file, and otherwise as
    ``pr_assessment_webhook``.
    """
    return intake_pull_request(request, background_tasks)


def intake_pull_request(request: PrIntakeRequest, background_tasks: BackgroundTasks) -> PrIntakeAccepted:
    """Schedule the assessment of a pull request read from Azure DevOps
    (``pr_intake``; the poller calls it too). Raises HTTPException with the
    responses ``pr_intake`` documents.
    """
    scope = BaselineScope(tenant=request.tenant, project_id=request.project_id)
    policy = _load_policy(scope)
    if policy is None or policy.source_control is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The project policy for {scope.tenant}/{scope.project_id} names no "
                "source_control, so pull requests can't be read from Azure DevOps."
            ),
        )
    source_control = policy.source_control
    extractor = DevOpsExtractor(
        org_url=source_control.organization_url, project=source_control.project, repositories=[]
    )
    result = extractor.read_pull_request(request.pr_id)
    if not result.items:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="; ".join(result.errors) or f"Pull request {request.pr_id} can't be read.",
        )
    details = result.items[0]
    if not details.files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Pull request {request.pr_id} changes no file.",
        )
    payload = PullRequestWebhook(
        pr_id=str(details.pr_id),
        base_sha=details.base_sha,
        head_sha=details.head_sha,
        repository=details.repository,
        changed_files=details.changed_paths,
        change_type=request.change_type,
        diff=details.diff or _EMPTY_DIFF,
        title=details.title,
        description=details.description,
        source_branch=details.source_branch,
        target_branch=details.target_branch or "main",
        tenant=request.tenant,
        project_id=request.project_id,
        workspace_id=request.workspace_id,
    )
    source = PullRequestTarget(
        organization_url=source_control.organization_url,
        project=source_control.project,
        repository_id=details.repository_id or details.repository,
        pr_id=details.pr_id,
        iteration_id=details.iteration_id,
    )
    accepted = schedule_assessment(payload, background_tasks, source)
    return PrIntakeAccepted(
        **accepted.model_dump(),
        repository=details.repository,
        title=details.title,
        base_sha=details.base_sha,
        head_sha=details.head_sha,
        diff_truncated=details.diff_truncated,
        skipped_files=details.skipped_files,
    )


@router.get("/pr-assessments", response_model=list[PrAssessmentSummary])
def list_pr_assessments() -> list[PrAssessmentSummary]:
    """PR assessments, newest first: this server session's, plus archived
    ones when an archive is configured."""
    _restore_archive()
    return [
        PrAssessmentSummary(
            job_id=record.job_id,
            pr_id=record.pr_id,
            tenant=record.tenant,
            project_id=record.project_id,
            status=record.status,
            headline=record.briefing.headline if record.briefing else "",
            decisions_needed=record.briefing.decisions_needed if record.briefing else 0,
            evidence_fixes=record.briefing.evidence_fixes if record.briefing else 0,
        )
        for record in reversed(list(_assessment_jobs.values()))
    ]


@router.get("/pr-assessment/{job_id}/briefing", response_model=ReleaseBriefing)
def get_pr_briefing(job_id: str) -> ReleaseBriefing:
    """The release briefing of a completed PR assessment."""
    record = find_assessment(job_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job ID")
    if record.briefing is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Assessment {job_id} is {record.status}; no briefing yet.",
        )
    return record.briefing


@router.get("/pr-assessment/{job_id}", response_model=PrAssessmentJob)
def get_pr_assessment(job_id: str) -> PrAssessmentJob:
    """Return the current status of a scheduled PR assessment."""
    record = find_assessment(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job ID"
        )
    return record
