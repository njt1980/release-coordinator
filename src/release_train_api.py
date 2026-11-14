"""Release train API: assess several completed PR assessments as one release.

``POST /api/v1/release-trains`` combines completed assessments of one
project (see ``orchestration.release_train``); ``GET /api/v1/release-trains``
lists trains and ``GET /api/v1/release-trains/{train_id}`` rebuilds one from
its members' current records. Single-PR assessment is unchanged.

Trains are kept in memory only: after a restart, create the train again
from the same assessments (which are archived when an archive is set).
"""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from src.orchestration.release_train import (
    ReleaseTrain,
    TrainMemberInput,
    build_release_train,
)
from src.webhooks import PrAssessmentJob, find_assessment

router = APIRouter(prefix="/api/v1/release-trains", tags=["release-trains"])


class ReleaseTrainRequest(BaseModel):
    """The completed PR assessments to combine, by job ID."""

    job_ids: list[str] = Field(min_length=2)
    title: str = Field(default="", max_length=200)

    @field_validator("job_ids")
    @classmethod
    def _distinct(cls, values: list[str]) -> list[str]:
        ids = [value.strip() for value in values if value.strip()]
        if len(set(ids)) != len(ids):
            raise ValueError("each assessment can be in a train only once")
        if len(ids) < 2:
            raise ValueError("a release train needs at least two assessments")
        return ids


class _StoredTrain(BaseModel):
    train_id: str
    request: ReleaseTrainRequest
    created_at: datetime


# In-memory, like the job registry; only mutated from the event loop.
_trains: dict[str, _StoredTrain] = {}


def _completed(job_id: str) -> PrAssessmentJob:
    record = find_assessment(job_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown job ID {job_id}")
    if record.status != "completed" or record.assessment is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Assessment {job_id} is {record.status}; only completed ones join a train.",
        )
    return record


def _build(stored: _StoredTrain) -> ReleaseTrain:
    records = [_completed(job_id) for job_id in stored.request.job_ids]
    projects = sorted({(r.tenant, r.project_id) for r in records})
    if len(projects) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A release train combines assessments of one project; got "
            + ", ".join(f"{t}/{p}" for t, p in projects),
        )
    tenant, project_id = projects[0]
    members = []
    for record in records:
        assert record.assessment is not None  # _completed checked it
        members.append(
            TrainMemberInput(
                job_id=record.job_id,
                pr_id=record.pr_id,
                headline=record.briefing.headline if record.briefing else "",
                assessment=record.assessment,
                order=record.order,
            )
        )
    return build_release_train(
        stored.train_id,
        stored.request.title,
        tenant,
        project_id,
        stored.created_at,
        members,
    )


@router.post("", response_model=ReleaseTrain, status_code=status.HTTP_201_CREATED)
def create_release_train(request: ReleaseTrainRequest) -> ReleaseTrain:
    """Combine completed assessments of one project into a release train.

    404 for an unknown job, 409 for one not completed, 422 when the jobs
    belong to different projects.
    """
    stored = _StoredTrain(
        train_id=str(uuid4()), request=request, created_at=datetime.now(timezone.utc)
    )
    train = _build(stored)
    _trains[stored.train_id] = stored
    return train


@router.get("", response_model=list[ReleaseTrain])
def list_release_trains() -> list[ReleaseTrain]:
    """This server session's trains, newest first (unbuildable ones skipped)."""
    trains: list[ReleaseTrain] = []
    for stored in reversed(list(_trains.values())):
        try:
            trains.append(_build(stored))
        except HTTPException:
            continue
    return trains


@router.get("/{train_id}", response_model=ReleaseTrain)
def get_release_train(train_id: str) -> ReleaseTrain:
    """Rebuild one train from its members' current records."""
    stored = _trains.get(train_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown release train ID")
    return _build(stored)
