"""Release decision API.

``POST /api/v1/webhooks/pr-assessment/{job_id}/decisions`` records a
person's Approve / Decline / Further analysis on one completed assessment;
``GET`` on the same path lists that assessment's decisions, newest last,
each with its signature manifestation and whether the assessment is still
the one that was signed. ``GET /api/v1/decisions/verify`` walks the whole
hash chain.

The client says only what was decided and why. The server sets the signer,
the time, the assessment hash and everything the decision is about, from
the stored assessment. Behind the platform's sign-in
(``RC_TRUST_PRINCIPAL_HEADER``) the signer is the signed-in Entra user,
who needs the ``RC_DECISION_ROLE`` app role when one is configured; a
typed name is refused there. Locally (no sign-in) the typed name is
recorded as such: not an electronic signature.
"""

import re
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from src.core import config
from src.core.decision import (
    GIT_SHA_PATTERN,
    DecisionDraft,
    DecisionRecord,
    DecisionVerdict,
    Signer,
    SignerSource,
    assessment_digest,
    clean_name,
)
from src.core.models import ReleaseAssessment
from src.tools.baseline_store import BaselineScope
from src.tools.decision_store import (
    ChainReport,
    DecisionStoreCorruptError,
    UnknownCorrectionError,
    default_decision_store,
)
from src.tools.principal import CLAIMS_HEADER, ID_HEADER, NAME_HEADER, Principal, parse_principal
from src.webhooks import PrAssessmentJob, find_assessment

router = APIRouter(tags=["decisions"])
DECISION_STORE = default_decision_store()


class DecisionRequest(BaseModel):
    """What a person decided; ``signer_name`` counts only without platform sign-in."""

    verdict: DecisionVerdict
    reason: str = Field(default="", max_length=4000)
    signer_name: str = Field(default="", max_length=200)
    corrects: str | None = None


class DecisionView(BaseModel):
    """A stored decision as every view shows it.

    ``manifestation`` is the signature line (meaning, printed name, UTC
    time); ``assessment_unchanged`` whether the assessment stored now is
    the one signed; ``superseded`` whether a later decision on the same
    assessment exists.
    """

    record: DecisionRecord
    meaning: str
    manifestation: str
    assessment_unchanged: bool
    superseded: bool


def _job(job_id: str) -> tuple[PrAssessmentJob, ReleaseAssessment]:
    job = find_assessment(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown assessment {job_id}.")
    if job.status != "completed" or job.assessment is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Assessment {job_id} is {job.status}; only a completed assessment can be decided.",
        )
    return job, job.assessment


def _signer(request: DecisionRequest, principal: Principal | None) -> Signer:
    if config.TRUST_PRINCIPAL_HEADER:
        if principal is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to record a decision.")
        if config.DECISION_ROLE and config.DECISION_ROLE not in principal.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Recording a release decision needs the {config.DECISION_ROLE!r} app role.",
            )
        return Signer(
            name=clean_name(principal.display_name),
            identifier=principal.user_name,
            object_id=principal.object_id,
            source=SignerSource.ENTRA,
        )
    name = clean_name(request.signer_name)
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="A signer name is required.")
    return Signer(name=name, identifier=name, source=SignerSource.TYPED)


def _views(records: list[DecisionRecord], assessment: ReleaseAssessment) -> list[DecisionView]:
    digest = assessment_digest(assessment.model_dump(mode="json"))
    latest = records[-1].record_id if records else None
    return [
        DecisionView(
            record=record,
            meaning=record.meaning,
            manifestation=record.manifestation,
            assessment_unchanged=record.assessment_sha256 == digest,
            superseded=record.record_id != latest,
        )
        for record in records
    ]


def _store_unavailable(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"The decision records can't be read: {error}",
    )


@router.get("/api/v1/webhooks/pr-assessment/{job_id}/decisions", response_model=list[DecisionView])
def list_decisions(job_id: str) -> list[DecisionView]:
    """Every decision on one assessment, oldest first."""
    job, assessment = _job(job_id)
    try:
        records = DECISION_STORE.records(BaselineScope(tenant=job.tenant, project_id=job.project_id), job_id)
    except (DecisionStoreCorruptError, OSError) as error:
        raise _store_unavailable(error) from error
    return _views(records, assessment)


@router.post(
    "/api/v1/webhooks/pr-assessment/{job_id}/decisions",
    response_model=list[DecisionView],
    status_code=status.HTTP_201_CREATED,
)
def record_decision(
    job_id: str,
    request: DecisionRequest,
    user_name: Annotated[str | None, Header(alias=NAME_HEADER)] = None,
    object_id: Annotated[str | None, Header(alias=ID_HEADER)] = None,
    claims: Annotated[str | None, Header(alias=CLAIMS_HEADER)] = None,
) -> list[DecisionView]:
    """Record one decision and return every decision on the assessment."""
    job, assessment = _job(job_id)
    evidence = assessment.evidence
    head_sha = (evidence.head_sha or "").lower()
    if not re.fullmatch(GIT_SHA_PATTERN, head_sha):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The assessment names no head commit, so a decision can't be tied to what was reviewed.",
        )
    signer = _signer(request, parse_principal(user_name, object_id, claims))
    try:
        draft = DecisionDraft(
            record_id=uuid4().hex,
            tenant=job.tenant,
            project_id=job.project_id,
            job_id=job_id,
            pr_id=evidence.pr_id,
            repository=evidence.repository,
            head_sha=head_sha,
            baseline_snapshot_id=evidence.baseline_snapshot_id,
            policy_version=evidence.policy_version,
            policy_sha256=evidence.policy_sha256,
            assessment_sha256=assessment_digest(assessment.model_dump(mode="json")),
            tool_approved=assessment.approved,
            tool_actionable=assessment.actionable,
            verdict=request.verdict,
            reason=request.reason.strip(),
            signer=signer,
            signed_at=datetime.now(timezone.utc),
            corrects=request.corrects,
        )
    except ValidationError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    try:
        DECISION_STORE.append(draft)
        records = DECISION_STORE.records(BaselineScope(tenant=job.tenant, project_id=job.project_id), job_id)
    except UnknownCorrectionError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    except (DecisionStoreCorruptError, OSError) as error:
        raise _store_unavailable(error) from error
    return _views(records, assessment)


@router.get("/api/v1/decisions/verify", response_model=ChainReport)
def verify_decisions() -> ChainReport:
    """Recompute every decision record's hash and link."""
    try:
        return DECISION_STORE.verify()
    except (DecisionStoreCorruptError, OSError) as error:
        raise _store_unavailable(error) from error
