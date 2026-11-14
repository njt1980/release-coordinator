"""Release decision records: a person's Approve / Decline / Further analysis.

The assessment is advice; the decision is the record a regulated change
process keeps. Each record is built for 21 CFR Part 11 (it does not claim
compliance, which only the customer's validation can establish):

- signature manifestation (11.50): the signer's printed name, the UTC time
  and the meaning of the signature are part of the record and of every view
  of it (``manifestation``);
- signature/record linking (11.70): the record carries the SHA-256 of the
  exact assessment it was signed on, the PR, head commit, baseline and
  policy version, and is itself hashed into a chain, so it can't be moved to
  another assessment or altered unnoticed;
- audit trail (11.10(e)): records are only appended; a correction is a new
  record that names the one it corrects, which stays readable.

``SignerSource.ENTRA`` records come from the platform's sign-in;
``TYPED`` records (local pilot) carry a typed name and are not electronic
signatures.
"""

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Final, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from src.core.hashing import sha256_hex

GENESIS_HASH: Final[str] = "0" * 64
_HEX64: Final[str] = r"^[0-9a-f]{64}$"
GIT_SHA_PATTERN: Final[str] = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"


class DecisionVerdict(str, Enum):
    """What the person decided about one assessed PR iteration."""

    APPROVE = "approve"
    DECLINE = "decline"
    FURTHER_ANALYSIS = "further_analysis"


# The meaning of the signature (11.50(a)(3)), shown with every record.
MEANINGS: Final[dict[DecisionVerdict, str]] = {
    DecisionVerdict.APPROVE: "Approved for release",
    DecisionVerdict.DECLINE: "Declined for release",
    DecisionVerdict.FURTHER_ANALYSIS: "Further analysis requested before release",
}


class SignerSource(str, Enum):
    """Where the signer's identity came from.

    ENTRA: the platform's sign-in (Easy Auth); the identity is verified.
    TYPED: a name typed in the local pilot; not an electronic signature.
    """

    ENTRA = "entra"
    TYPED = "typed"


class Signer(BaseModel):
    """Who signed: printed name, stable ID and, from Entra, the object ID."""

    model_config = {"frozen": True, "extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    identifier: str = Field(min_length=1, max_length=320)
    object_id: str | None = Field(default=None, max_length=64)
    source: SignerSource


class DecisionDraft(BaseModel):
    """A decision before the store places it in the chain.

    ``assessment_sha256`` is ``assessment_digest`` of the assessment shown;
    ``tool_approved`` / ``tool_actionable`` are its verdict at signing time.
    Approving an assessment the tool did not approve, declining, and asking
    for further analysis all need a reason. ``corrects`` names an earlier
    record of the same assessment that this one corrects.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    record_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    tenant: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    pr_id: str = Field(min_length=1)
    repository: str = ""
    head_sha: str = Field(pattern=GIT_SHA_PATTERN)
    baseline_snapshot_id: str | None = None
    policy_version: int | None = None
    policy_sha256: str | None = None
    assessment_sha256: str = Field(pattern=_HEX64)
    tool_approved: bool
    tool_actionable: bool
    verdict: DecisionVerdict
    reason: str = Field(default="", max_length=4000)
    signer: Signer
    signed_at: datetime
    corrects: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_validator("head_sha")
    @classmethod
    def _lower_sha(cls, value: str) -> str:
        return value.lower()

    @field_validator("signed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("signed_at must carry a time zone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _reason_when_needed(self) -> Self:
        overrides = self.verdict is DecisionVerdict.APPROVE and not self.tool_approved
        if (overrides or self.verdict is not DecisionVerdict.APPROVE) and not self.reason.strip():
            raise ValueError(
                "a reason is required to decline, to ask for further analysis, "
                "or to approve what the assessment did not approve"
            )
        return self

    @property
    def meaning(self) -> str:
        """The meaning of this signature."""
        return MEANINGS[self.verdict]


class DecisionRecord(DecisionDraft):
    """A decision placed in the store's hash chain.

    ``sequence`` is its position in the store (1 first), ``previous_hash``
    the ``record_hash`` before it (``GENESIS_HASH`` for the first), and
    ``record_hash`` the SHA-256 of every other field (``content_hash``).
    """

    sequence: int = Field(ge=1)
    previous_hash: str = Field(pattern=_HEX64)
    record_hash: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _hash_matches(self) -> Self:
        if self.record_hash != content_hash(self):
            raise ValueError(f"record {self.record_id}: record_hash does not match its content")
        return self

    @property
    def manifestation(self) -> str:
        """The signature as every human-readable view must show it (11.50)."""
        note = "" if self.signer.source is SignerSource.ENTRA else " (typed name, not an electronic signature)"
        identifier = "" if self.signer.identifier == self.signer.name else f" <{self.signer.identifier}>"
        return (
            f"{self.meaning}: {self.signer.name}{identifier}{note}, "
            f"{self.signed_at:%Y-%m-%d %H:%M:%S} UTC"
        )


def content_hash(record: DecisionDraft | DecisionRecord, previous_hash: str | None = None, sequence: int | None = None) -> str:
    """SHA-256 over a record's content: every field but ``record_hash``.

    For a draft, ``previous_hash`` and ``sequence`` are the chain position
    the store is about to give it.
    """
    data = record.model_dump(mode="json", exclude={"record_hash"})
    if previous_hash is not None:
        data["previous_hash"] = previous_hash
    if sequence is not None:
        data["sequence"] = sequence
    return sha256_hex(data)


def seal(draft: DecisionDraft, sequence: int, previous_hash: str) -> DecisionRecord:
    """Place ``draft`` in the chain after ``previous_hash``."""
    return DecisionRecord(
        **draft.model_dump(),
        sequence=sequence,
        previous_hash=previous_hash,
        record_hash=content_hash(draft, previous_hash, sequence),
    )


def assessment_digest(assessment_json: dict[str, object]) -> str:
    """SHA-256 of an assessment's JSON form (``model_dump(mode="json")``)."""
    return sha256_hex(assessment_json)


_UNSAFE_NAME: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")


def clean_name(value: str) -> str:
    """A signer name without control characters or surrounding spaces."""
    return _UNSAFE_NAME.sub("", value).strip()
