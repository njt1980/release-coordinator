"""One pass of the pull request poller for one project.

Lists the active pull requests of the policy's repositories, picks those
whose iteration hasn't been handled yet (a new source commit or a new
target branch), hands each to PR intake, and records what happened, so
the same iteration is never assessed twice. The Azure DevOps read and the
intake are passed in, so this module holds only the decisions.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, Field

from src.core.models import ContractChange, DiscoveryResult, DiscoveryStatus
from src.tools.baseline_store import BaselineScope
from src.tools.devops_pull_requests import ActivePullRequest
from src.tools.policy_store import AdoConnection
from src.tools.pr_watch_store import (
    PollOutcome,
    PrWatchStore,
    SeenPullRequest,
    pull_request_key,
)

BREAKING_LABEL: Final[str] = "rc:breaking"
ADDITIVE_LABEL: Final[str] = "rc:additive"


class IntakeAttempt(BaseModel):
    """What handing one pull request iteration to intake did."""

    outcome: PollOutcome
    job_id: str | None = None
    detail: str = ""


class PolledPullRequest(BaseModel):
    """One pull request iteration this pass handed to intake."""

    repository: str
    pr_id: int
    source_commit: str
    change_type: ContractChange
    attempt: IntakeAttempt


class PollReport(BaseModel):
    """What one pass did for one project."""

    tenant: str
    project_id: str
    listed: int = Field(default=0, ge=0)
    handled: list[PolledPullRequest] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


ListActive = Callable[[list[str]], DiscoveryResult[ActivePullRequest]]
Intake = Callable[[ActivePullRequest, ContractChange], IntakeAttempt]


def change_type_for(pull_request: ActivePullRequest, default: ContractChange) -> ContractChange:
    """The pull request's label decides; ``rc:breaking`` wins over ``rc:additive``."""
    labels = {label.strip().lower() for label in pull_request.labels}
    if BREAKING_LABEL in labels:
        return ContractChange.BREAKING
    if ADDITIVE_LABEL in labels:
        return ContractChange.ADDITIVE
    return default


def _targets(pull_request: ActivePullRequest, branches: list[str]) -> bool:
    return not branches or pull_request.target_branch.lower() in {branch.lower() for branch in branches}


def due_pull_requests(
    active: list[ActivePullRequest], seen: dict[str, SeenPullRequest], branches: list[str]
) -> list[ActivePullRequest]:
    """The active pull requests whose current iteration wasn't handled yet
    (or whose last attempt failed, e.g. no baseline or a read error), by
    repository and pull request ID."""
    due: list[ActivePullRequest] = []
    for pull_request in active:
        if not _targets(pull_request, branches):
            continue
        last = seen.get(pull_request_key(pull_request.repository_id, pull_request.pr_id))
        if (
            last is not None
            and last.outcome != "failed"
            and last.source_commit == pull_request.source_commit
            and last.target_branch == pull_request.target_branch
        ):
            continue
        due.append(pull_request)
    return sorted(due, key=lambda pull_request: (pull_request.repository.lower(), pull_request.pr_id))


def poll_project(
    scope: BaselineScope,
    connection: AdoConnection,
    list_active: ListActive,
    intake: Intake,
    store: PrWatchStore,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PollReport:
    """Assess every new iteration of the project's active pull requests.

    Each iteration is recorded with what intake made of it. A scheduled or
    skipped one (it changes no file, or none a service owns) isn't tried
    again until it changes; a failed one (no baseline yet, a read error)
    is tried again on the next pass.
    The state of pull requests that are no longer active is dropped.
    """
    report = PollReport(tenant=scope.tenant, project_id=scope.project_id)
    listed = list_active(list(connection.repositories))
    report.errors.extend(listed.errors)
    if listed.status is DiscoveryStatus.FAILED:
        return report
    report.listed = len(listed.items)
    state = store.load(scope)
    for pull_request in due_pull_requests(listed.items, state.entries, connection.target_branches):
        change_type = change_type_for(pull_request, connection.change_type)
        attempt = intake(pull_request, change_type)
        report.handled.append(
            PolledPullRequest(
                repository=pull_request.repository,
                pr_id=pull_request.pr_id,
                source_commit=pull_request.source_commit,
                change_type=change_type,
                attempt=attempt,
            )
        )
        entry = SeenPullRequest(
            repository_id=pull_request.repository_id,
            pr_id=pull_request.pr_id,
            source_commit=pull_request.source_commit,
            target_branch=pull_request.target_branch,
            handled_at=now(),
            outcome=attempt.outcome,
            job_id=attempt.job_id,
            detail=attempt.detail,
        )
        state.entries[entry.key] = entry
    if listed.status is DiscoveryStatus.SUCCESS:
        active = {pull_request_key(item.repository_id, item.pr_id) for item in listed.items}
        state.entries = {key: entry for key, entry in state.entries.items() if key in active}
    store.save(scope, state)
    return report
