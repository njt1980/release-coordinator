"""What a validation bundle quotes besides the decision record: the tool
version, the stored discovery benchmark results, and the intended-use
statement (read by ``tools.validation_sources``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ToolVersion(BaseModel):
    """Which build of the tool made the bundle (``source`` says how that is known)."""

    version: str = Field(min_length=1)
    source: Literal["environment", "git", "unknown"]


class BenchmarkScores(BaseModel):
    """One row of the stored discovery benchmark (see tests/benchmarks/discovery)."""

    inventory_precision: float | None = None
    inventory_recall: float | None = None
    static_precision: float | None = None
    static_recall: float | None = None
    manifest_precision: float | None = None
    manifest_recall: float | None = None
    missed_silent: int | None = None
    missed_flagged: int | None = None
    services_flagged: int | None = None
    flag_precision: float | None = None
    cause_precision: float | None = None


class BenchmarkApp(BaseModel):
    """One benchmark app's scores and split (``held_out`` apps were never tuned on)."""

    name: str = Field(min_length=1)
    split: str = "unknown"
    scores: BenchmarkScores


class BenchmarkResults(BaseModel):
    """The stored benchmark baseline; ``available`` is false (with ``problem``) when unreadable."""

    available: bool
    source_file: str
    sha256: str = ""
    pooled: BenchmarkScores | None = None
    pooled_held_out: BenchmarkScores | None = None
    apps: list[BenchmarkApp] = Field(default_factory=list)
    problem: str = ""


class IntendedUse(BaseModel):
    """The intended-use statement as shipped; ``available`` false (with ``problem``) when missing."""

    available: bool
    source_file: str
    sha256: str = ""
    text: str = ""
    problem: str = ""
