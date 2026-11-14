"""Which pull request iterations the poller has already handled, per project.

One JSON file per project, ``{root}/{tenant}/{project_id}.json``, written to
a hidden temporary file and renamed into place (readers never see half a
file). A pull request is keyed by repository ID and pull request ID; its
entry says which source commit and target branch were last handled, so a
new iteration (a push, or a retarget) is told apart from one already seen.
The file is advisory bookkeeping: losing it only means the active pull
requests are assessed once more.
"""

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field, ValidationError

from src.core import config
from src.tools.baseline_store import BaselineScope

logger = logging.getLogger(__name__)

_SUFFIX: Final[str] = ".json"

PollOutcome = Literal["scheduled", "skipped", "failed"]


class SeenPullRequest(BaseModel):
    """The last iteration of one pull request the poller handled."""

    repository_id: str = Field(min_length=1)
    pr_id: int = Field(gt=0)
    source_commit: str = Field(min_length=1)
    target_branch: str = ""
    handled_at: datetime
    outcome: PollOutcome
    job_id: str | None = None
    detail: str = ""

    @property
    def key(self) -> str:
        return pull_request_key(self.repository_id, self.pr_id)


class SeenPullRequests(BaseModel):
    """Everything the poller remembers about one project."""

    entries: dict[str, SeenPullRequest] = Field(default_factory=dict)


def pull_request_key(repository_id: str, pr_id: int) -> str:
    return f"{repository_id.lower()}/{pr_id}"


class PrWatchStore:
    """Directory of per-project poller state."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, scope: BaselineScope) -> Path:
        return self.root / scope.tenant / f"{scope.project_id}{_SUFFIX}"

    def load(self, scope: BaselineScope) -> SeenPullRequests:
        """What was handled for ``scope``; empty when nothing was, or the
        file is unreadable (logged)."""
        path = self._path(scope)
        try:
            return SeenPullRequests.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return SeenPullRequests()
        except (OSError, ValidationError) as error:
            logger.warning("Poller state %s unreadable, starting over: %s", path, error)
            return SeenPullRequests()

    def save(self, scope: BaselineScope, seen: SeenPullRequests) -> None:
        """Replace ``scope``'s state.

        Raises:
            OSError: if the file can't be written.
        """
        path = self._path(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".", suffix=".tmp", delete=False
        ) as handle:
            handle.write(seen.model_dump_json(indent=1))
            temp_path = Path(handle.name)
        os.replace(temp_path, path)


def default_pr_watch_store() -> PrWatchStore:
    """The store under the data root (``pr-watch``)."""
    return PrWatchStore(config.DATA_ROOT / config.PR_WATCH_DIR)
