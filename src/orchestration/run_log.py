"""Live run logs: record what each onboarding or assessment step checked.

``run_graph`` executes a compiled LangGraph workflow one node at a time
(``stream_mode="updates"``) and hands each node's update to a narrator,
which turns it into plain-language ``EventDraft``s. The ``RunLogStore``
numbers and timestamps them so clients can poll for what is new. The
graphs, their nodes and the tool adapters are not changed: narration only
reads node updates.

Logs live in memory, like the job registries, and are lost on restart
(durable runs are gap G17). Messages carry identifiers, counts and
findings, never credentials.
"""

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Final

from langgraph.graph.state import CompiledStateGraph

from pydantic import BaseModel, Field

from src.core.models import (
    RunEvent,
    RunEventStatus,
    RunKind,
    RunState,
    RunSummary,
)

# Oldest runs are dropped beyond this many, so memory stays bounded.
MAX_RUNS: Final[int] = 200


class EventDraft(BaseModel):
    """An event before the store gives it a sequence number and time."""

    stage: str = Field(min_length=1)
    status: RunEventStatus
    message: str = Field(min_length=1)
    details: list[str] = Field(default_factory=list)


Narrator = Callable[[str, Mapping[str, Any], Mapping[str, Any]], Iterable[EventDraft]]
"""(node name, state before the node, the node's update) -> events."""


class _Run:
    def __init__(self, summary: RunSummary) -> None:
        self.summary = summary
        self.events: list[RunEvent] = []


class RunLogStore:
    """Thread-safe, in-memory store of run summaries and their events."""

    def __init__(self, max_runs: int = MAX_RUNS) -> None:
        self._runs: OrderedDict[str, _Run] = OrderedDict()
        self._lock = threading.Lock()
        self._max_runs = max_runs

    def start(
        self, run_id: str, kind: RunKind, title: str, tenant: str, project_id: str
    ) -> RunSummary:
        """Register a new run (replacing any earlier run with the same ID)."""
        summary = RunSummary(
            run_id=run_id,
            kind=kind,
            title=title,
            tenant=tenant,
            project_id=project_id,
            started_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._runs.pop(run_id, None)
            self._runs[run_id] = _Run(summary)
            while len(self._runs) > self._max_runs:
                self._runs.popitem(last=False)
        return summary

    def add(self, run_id: str, draft: EventDraft) -> RunEvent | None:
        """Append an event to a run; None if the run is unknown."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            event = RunEvent(
                seq=len(run.events) + 1,
                at=datetime.now(timezone.utc),
                **draft.model_dump(),
            )
            run.events.append(event)
            run.summary = run.summary.model_copy(update={"event_count": len(run.events)})
            return event

    def finish(self, run_id: str, state: RunState, outcome: str) -> None:
        """Mark a run completed or failed with a one-line outcome."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.summary = run.summary.model_copy(
                    update={
                        "state": state,
                        "outcome": outcome,
                        "finished_at": datetime.now(timezone.utc),
                    }
                )

    def get(self, run_id: str, after: int = 0) -> tuple[RunSummary, list[RunEvent]] | None:
        """Return a run's summary and its events with ``seq`` > ``after``."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            return run.summary, [event for event in run.events if event.seq > after]

    def summaries(self) -> list[RunSummary]:
        """Return every run's summary, newest first."""
        with self._lock:
            return [run.summary for run in reversed(self._runs.values())]


def run_graph(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    state: Mapping[str, Any],
    narrate: Narrator,
    store: RunLogStore,
    run_id: str,
) -> dict[str, Any]:
    """Run ``graph`` node by node, logging each step, and return the final state.

    The final state is the input with every node's update applied in order,
    which is what ``invoke`` returns for these graphs (no reducers). An
    exception from a node propagates after a FAILED event is logged.
    """
    current: dict[str, Any] = dict(state)
    node = "start"
    try:
        for chunk in graph.stream(dict(state), stream_mode="updates"):
            for node, update in chunk.items():
                update = dict(update or {})
                for draft in narrate(node, current, update):
                    store.add(run_id, draft)
                current = {**current, **update}
    except Exception as error:
        store.add(
            run_id,
            EventDraft(
                stage="Run",
                status=RunEventStatus.FAILED,
                message=f"Run stopped with an error after step '{node}': {error}",
            ),
        )
        raise
    return current


DEFAULT_RUN_LOGS: Final[RunLogStore] = RunLogStore()
