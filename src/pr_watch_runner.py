"""Runs the pull request poller inside the API server, when a policy asks.

At startup the server looks for any project policy with
``ado_connection.poll: true``; without one the poller never starts (the
default, and the showcase's state). While it runs it wakes every
``PR_WATCH_TICK_SECONDS``, rereads the policies and polls each project
whose ``interval_minutes`` have passed. Turning polling on for the first
project therefore needs a server restart; changing or turning it off for a
project doesn't.

Each pass reads Azure DevOps read-only (see ``orchestration.pr_watch``);
the assessments it schedules run one after another once the pass is done.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from fastapi import BackgroundTasks, HTTPException, status
from starlette.concurrency import run_in_threadpool

from src.core import config
from src.core.models import ContractChange, DiscoveryResult
from src.orchestration.pr_watch import IntakeAttempt, PollReport, poll_project
from src.tools.baseline_store import BaselineScope
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.devops_pull_requests import ActivePullRequest
from src.tools.policy_store import (
    PolicyInvalidError,
    PolicyMissingError,
    PolicyStore,
    ProjectPolicy,
    default_policy_store,
)
from src.tools.pr_watch_store import PollOutcome, PrWatchStore, default_pr_watch_store
from src.webhooks import PrIntakeRequest, intake_pull_request

logger = logging.getLogger(__name__)

POLICY_STORE: PolicyStore = default_policy_store()
WATCH_STORE: PrWatchStore = default_pr_watch_store()

# Intake responses that mean "this iteration can't be assessed as it is"
# (it changes no file, or none a service owns): not retried until it changes.
_SKIPPED_STATUSES = frozenset({status.HTTP_422_UNPROCESSABLE_CONTENT})


def polling_policies(store: PolicyStore) -> list[tuple[BaselineScope, ProjectPolicy]]:
    """Every valid policy that turns polling on."""
    found: list[tuple[BaselineScope, ProjectPolicy]] = []
    for scope in store.list_scopes():
        try:
            policy = store.load(scope).policy
        except (PolicyMissingError, PolicyInvalidError):
            continue
        if policy.ado_connection.poll and policy.source_control is not None:
            found.append((scope, policy))
    return found


def _lister(policy: ProjectPolicy) -> Callable[[list[str]], DiscoveryResult[ActivePullRequest]]:
    source = policy.source_control
    assert source is not None

    def list_active(repositories: list[str]) -> DiscoveryResult[ActivePullRequest]:
        try:
            extractor = DevOpsExtractor(source.organization_url, source.project, repositories)
        except ValueError as error:
            return DiscoveryResult[ActivePullRequest].failed(str(error))
        return extractor.list_active_pull_requests(repositories)

    return list_active


def _intake(
    scope: BaselineScope, tasks: BackgroundTasks
) -> Callable[[ActivePullRequest, ContractChange], IntakeAttempt]:
    def intake(pull_request: ActivePullRequest, change_type: ContractChange) -> IntakeAttempt:
        request = PrIntakeRequest(
            pr_id=pull_request.pr_id,
            change_type=change_type,
            tenant=scope.tenant,
            project_id=scope.project_id,
        )
        try:
            accepted = intake_pull_request(request, tasks)
        except HTTPException as error:
            outcome: PollOutcome = "skipped" if error.status_code in _SKIPPED_STATUSES else "failed"
            return IntakeAttempt(outcome=outcome, detail=str(error.detail))
        except Exception as error:
            logger.warning("Intake of pull request %s failed: %s", pull_request.pr_id, error)
            return IntakeAttempt(outcome="failed", detail=str(error) or repr(error))
        return IntakeAttempt(outcome="scheduled", job_id=accepted.job_id)

    return intake


async def poll_once(scope: BaselineScope, policy: ProjectPolicy) -> PollReport:
    """One pass for one project; then run the assessments it scheduled."""
    tasks = BackgroundTasks()
    report = await run_in_threadpool(
        poll_project, scope, policy.ado_connection, _lister(policy), _intake(scope, tasks), WATCH_STORE
    )
    for error in report.errors:
        logger.warning("Pull request poll of %s/%s: %s", scope.tenant, scope.project_id, error)
    await tasks()
    return report


async def run_poller(stop: asyncio.Event) -> None:
    """Poll every due project until ``stop`` is set; a failed pass is logged."""
    last_poll: dict[tuple[str, str], float] = {}
    while not stop.is_set():
        for scope, policy in await run_in_threadpool(polling_policies, POLICY_STORE):
            key = (scope.tenant, scope.project_id)
            due = last_poll.get(key, 0.0) + policy.ado_connection.interval_minutes * 60
            if time.monotonic() < due and key in last_poll:
                continue
            last_poll[key] = time.monotonic()
            try:
                report = await poll_once(scope, policy)
                logger.info(
                    "Polled %s/%s: %d active, %d handled",
                    scope.tenant,
                    scope.project_id,
                    report.listed,
                    len(report.handled),
                )
            except Exception:
                logger.exception("Pull request poll of %s/%s failed", scope.tenant, scope.project_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.PR_WATCH_TICK_SECONDS)
        except TimeoutError:
            pass
