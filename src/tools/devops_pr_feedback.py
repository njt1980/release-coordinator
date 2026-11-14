"""Write an assessment's outcome back to an Azure DevOps pull request.

The only module that writes to Azure DevOps; everything else reads. Each
write is opted into by the project policy (``ado_connection.comment`` and
``ado_connection.status``) and needs "Contribute to pull requests" for the
server's identity on that project:

- a status on the assessed iteration, genre ``release-coordinator``, name
  ``assessment``: ``succeeded`` (approved), ``pending`` (a person decides),
  ``failed`` (evidence incomplete) or ``error`` (the run failed). It is
  advisory: it blocks a merge only if a branch policy requires it, which
  this server never creates.
- the release briefing as one comment thread, posted closed so a
  "comments must be resolved" branch policy is never held up by it.

The builders are pure; ``post_feedback`` takes the git client, so tests
use a fake. Failures are returned, never raised.
"""

import logging
import threading
import time
from typing import Any, Final, Literal

from azure.devops.connection import Connection  # type: ignore[import-untyped]
from azure.devops.released.git import (  # type: ignore[import-untyped]
    Comment,
    GitPullRequestCommentThread,
    GitPullRequestStatus,
    GitStatusContext,
)
from azure.identity import DefaultAzureCredential
from msrest.authentication import BasicTokenAuthentication
from pydantic import BaseModel, Field

from src.core import config

logger = logging.getLogger(__name__)

STATUS_GENRE: Final[str] = "release-coordinator"
STATUS_NAME: Final[str] = "assessment"
# Azure DevOps keeps a status description short and a comment under 150k.
MAX_STATUS_DESCRIPTION: Final[int] = 400
MAX_COMMENT_CHARS: Final[int] = 100_000
_AZURE_DEVOPS_SCOPE: Final[str] = "499b84ac-1321-427f-aa17-267ca6975798/.default"
_TOKEN_REFRESH_MARGIN_SECONDS: Final[int] = 300
_THREAD_CLOSED: Final[str] = "closed"

StatusState = Literal["succeeded", "pending", "failed", "error"]


class PullRequestTarget(BaseModel):
    """The pull request iteration an assessment was made of."""

    organization_url: str = Field(min_length=1)
    project: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    pr_id: int = Field(gt=0)
    iteration_id: int = Field(default=0, ge=0)


class AssessmentOutcome(BaseModel):
    """What the feedback says about one assessment."""

    job_id: str = Field(min_length=1)
    failed: bool = False
    approved: bool = False
    actionable: bool = False
    headline: str = ""
    briefing_markdown: str = ""


class FeedbackResult(BaseModel):
    """What was written (or why not) for one assessment."""

    status_state: StatusState | None = None
    status_posted: bool = False
    comment_posted: bool = False
    errors: list[str] = Field(default_factory=list)


def status_state(outcome: AssessmentOutcome) -> StatusState:
    """The status that says, without blocking, what the assessment found."""
    if outcome.failed:
        return "error"
    if outcome.approved:
        return "succeeded"
    return "pending" if outcome.actionable else "failed"


def _description(outcome: AssessmentOutcome, state: StatusState) -> str:
    if state == "error":
        text = "Assessment failed to run (advisory)."
    else:
        text = f"{outcome.headline or 'Assessed'} (advisory)"
    return text[:MAX_STATUS_DESCRIPTION]


def assessment_link(link_base_url: str, job_id: str) -> str | None:
    """The assessment's print view, when the server's address is known."""
    if not link_base_url:
        return None
    return f"{link_base_url}/api/v1/webhooks/pr-assessment/{job_id}/record?format=html"


def build_status(outcome: AssessmentOutcome, target_url: str | None) -> Any:
    """The advisory pull request status for ``outcome``."""
    state = status_state(outcome)
    return GitPullRequestStatus(
        context=GitStatusContext(genre=STATUS_GENRE, name=STATUS_NAME),
        state=state,
        description=_description(outcome, state),
        target_url=target_url,
    )


def comment_text(outcome: AssessmentOutcome, iteration_id: int) -> str:
    """The briefing as a comment, marked advisory, within the size limit."""
    head = f"_Release Coordinator, advisory: assessment {outcome.job_id}"
    head += f" of iteration {iteration_id}._\n\n" if iteration_id else "._\n\n"
    if outcome.failed:
        body = "The assessment failed to run; see the Release Coordinator for the error."
    else:
        body = outcome.briefing_markdown or outcome.headline or "Assessed."
    text = head + body
    if len(text) > MAX_COMMENT_CHARS:
        text = text[: MAX_COMMENT_CHARS - 40] + "\n\n_(briefing truncated)_"
    return text


def build_thread(outcome: AssessmentOutcome, iteration_id: int) -> Any:
    """One closed thread holding the briefing."""
    return GitPullRequestCommentThread(
        comments=[Comment(content=comment_text(outcome, iteration_id), comment_type="text")],
        status=_THREAD_CLOSED,
    )


def post_feedback(
    git_client: Any,
    target: PullRequestTarget,
    outcome: AssessmentOutcome,
    *,
    status: bool,
    comment: bool,
    link_base_url: str = "",
) -> FeedbackResult:
    """Post the status and/or comment ``status``/``comment`` ask for.

    The status goes on the assessed iteration when it is known, else on
    the pull request. Never raises; each failed write is in ``errors``.
    """
    result = FeedbackResult()
    if status:
        result.status_state = status_state(outcome)
        payload = build_status(outcome, assessment_link(link_base_url, outcome.job_id))
        try:
            if target.iteration_id:
                git_client.create_pull_request_iteration_status(
                    payload, target.repository_id, target.pr_id, target.iteration_id, project=target.project
                )
            else:
                git_client.create_pull_request_status(
                    payload, target.repository_id, target.pr_id, project=target.project
                )
            result.status_posted = True
        except Exception as error:
            logger.warning("Status on pull request %s not posted: %s", target.pr_id, error)
            result.errors.append(f"status: {error}")
    if comment:
        try:
            git_client.create_thread(
                build_thread(outcome, target.iteration_id),
                target.repository_id,
                target.pr_id,
                project=target.project,
            )
            result.comment_posted = True
        except Exception as error:
            logger.warning("Comment on pull request %s not posted: %s", target.pr_id, error)
            result.errors.append(f"comment: {error}")
    return result


class DevOpsPullRequestWriter:
    """Azure DevOps git client for the feedback writes, with the server's
    identity (``DefaultAzureCredential``)."""

    def __init__(self, org_url: str) -> None:
        """Raises:
            ValueError: if ``org_url`` is blank or not in the deployment's
                ``RC_ADO_ORG_ALLOWLIST`` (when one is set).
        """
        url = org_url.strip().rstrip("/")
        if not url:
            raise ValueError("org_url is required")
        if config.ADO_ORG_ALLOWLIST and url.lower() not in config.ADO_ORG_ALLOWLIST:
            raise ValueError(f"{url} is not an Azure DevOps organization this server may use")
        self._organization_url = url
        self._credential = DefaultAzureCredential()
        self._lock = threading.Lock()
        self._cached: tuple[Any, float] | None = None

    def git_client(self) -> Any:
        """A git client whose token is fresh for at least a few minutes."""
        with self._lock:
            if self._cached is not None and time.time() < self._cached[1] - _TOKEN_REFRESH_MARGIN_SECONDS:
                return self._cached[0]
            token = self._credential.get_token(_AZURE_DEVOPS_SCOPE)
            connection = Connection(
                base_url=self._organization_url,
                creds=BasicTokenAuthentication({"access_token": token.token}),
            )
            client = connection.clients.get_git_client()
            self._cached = (client, float(token.expires_on))
            return client
