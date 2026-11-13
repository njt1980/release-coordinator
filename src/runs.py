"""Live run logs over HTTP: what each onboarding or assessment checked.

Clients poll ``GET /api/runs/{run_id}/events?after=<last seq>`` about once a
second while ``run.state`` is ``running``; each call returns only the
events they haven't seen. Logs are kept in memory (see
``src.orchestration.run_log``).
"""

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from src.core.models import RunEvent, RunSummary
from src.orchestration.run_log import DEFAULT_RUN_LOGS

router = APIRouter(prefix="/api/runs", tags=["runs"])
RUN_LOGS = DEFAULT_RUN_LOGS


class RunEventsPage(BaseModel):
    """A run's summary and the events after the requested sequence number."""

    run: RunSummary
    events: list[RunEvent]


@router.get("", response_model=list[RunSummary])
def list_runs() -> list[RunSummary]:
    """Return recent onboarding and assessment runs, newest first."""
    return RUN_LOGS.summaries()


@router.get("/{run_id}/events", response_model=RunEventsPage)
def run_events(run_id: str, after: int = Query(default=0, ge=0)) -> RunEventsPage:
    """Return the run's summary and its events with ``seq`` greater than ``after``."""
    found = RUN_LOGS.get(run_id, after)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown run ID")
    summary, events = found
    return RunEventsPage(run=summary, events=events)
