"""Typed contracts for the discovery-accuracy benchmark.

An ``AppTruth`` file pins the public repositories of one application and
records its documented dependency graph (every edge cites its primary
source). A ``BenchReport`` is what one run measured; it is compared with the
committed baseline by ``ratchet``.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Mapping targets with a special meaning (see NameMapping).
NOISE = "__noise__"
OPTIONAL = "__optional__"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepoPin(_Strict):
    """One public repository checked out at an exact commit."""

    name: str = Field(min_length=1, description="Repository name the scanner sees.")
    url: str = Field(pattern=r"^https://")
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class EnvSourceKind(str, Enum):
    K8S = "k8s"
    COMPOSE = "compose"


class EnvSource(_Strict):
    """Deployment files whose env values emulate the Kubernetes source.

    Paths start with a pinned repository name (``eShop/deploy``) or with
    ``@project/`` for files in this repository (``@project/deploy/lab/eshop``).
    """

    type: EnvSourceKind
    paths: list[str] = Field(min_length=1)
    dotenv: str | None = None


class NodeKind(str, Enum):
    SERVICE = "service"
    INFRA = "infra"


class TruthNode(_Strict):
    kind: NodeKind
    lang: str | None = None
    what: str | None = None
    source: bool = Field(True, description="False when the app ships no source for it.")
    optional: bool = False


class EdgeStatus(str, Enum):
    CORE = "core"
    OPTIONAL = "optional"
    AMBIGUOUS = "ambiguous"


class TruthEdge(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    caller: str = Field(alias="from")
    callee: str = Field(alias="to")
    proto: str
    kind: Literal["svc", "infra"]
    status: EdgeStatus = EdgeStatus.CORE
    mechanism: str
    source: str = Field(min_length=1, description="Primary source: file:line or doc URL.")


class NameMapping(_Strict):
    """Hand-made links from scanner names to ground-truth names.

    ``boundaries`` maps a scanner service (``name``, ``repo:name``, or
    ``root:repo:<root>`` when two services share a name) to a
    ground-truth service, to ``None`` (a bogus boundary) or to ``OPTIONAL``
    (not scored). ``hosts`` maps a reference host to a node or to ``NOISE``.
    ``workloads`` maps a deployment-file workload name to a node.
    """

    boundaries: dict[str, str | None] = Field(default_factory=dict)
    hosts: dict[str, str] = Field(default_factory=dict)
    workloads: dict[str, str] = Field(default_factory=dict)


class Split(str, Enum):
    """TUNED: the scanner was changed while looking at this app's misses, so
    its numbers flatter the scanner. HELD_OUT: never used to tune; its
    ground truth was built without seeing scanner output. Acceptance is
    judged on held-out apps."""

    TUNED = "tuned"
    HELD_OUT = "held_out"


class AppTruth(_Strict):
    app: str
    split: Split = Split.TUNED
    upstream: str
    gt_source: str
    repos: list[RepoPin] = Field(min_length=1)
    env_sources: list[EnvSource] = Field(default_factory=list)
    observability: list[str] = Field(default_factory=list)
    services: dict[str, TruthNode]
    edges: list[TruthEdge]
    mapping: NameMapping = Field(default_factory=NameMapping)


class Score(BaseModel):
    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None


class EdgeMode(str, Enum):
    STATIC = "static"
    MANIFEST = "manifest"


class EdgeScores(BaseModel):
    svc: Score
    infra: Score
    all: Score
    true_positives: list[dict[str, object]]
    false_positives: list[dict[str, object]]
    unresolved_targets: list[dict[str, str]]
    missed: list[dict[str, object]]


class MissAccounting(BaseModel):
    """How the missed edges (manifest mode) present to a coordinator, and
    how many flags point at a real miss.

    A service is flagged when its coverage is incomplete. A flag is useful
    when the service has a missed edge (manifest mode); otherwise it is a
    false alarm (the code wasn't fully understood, but nothing was missed).
    A cause is a set of services sharing the same unread files (what a
    briefing shows as one question); it is useful when any of them has a
    missed edge.
    """

    services_scored: int
    services_not_understood: int
    services_unsupported_language: int
    services_incomplete_any: int
    missed_edges: int
    missed_flagged: int
    missed_silent: int
    missed_caller_has_no_boundary: int
    services_flagged_with_miss: int = 0
    causes_flagged: int = 0
    causes_flagged_with_miss: int = 0


class InventoryScore(Score):
    missing: list[str]
    bogus: list[dict[str, str]]
    nontrivial_mappings: list[str]


class AppResult(BaseModel):
    app: str
    inventory: InventoryScore
    edges: dict[EdgeMode, EdgeScores]
    misses: MissAccounting


class Headline(BaseModel):
    """The pooled numbers the ratchet guards."""

    inventory_precision: float | None
    inventory_recall: float | None
    static_precision: float | None
    static_recall: float | None
    manifest_precision: float | None
    manifest_recall: float | None
    missed_silent: int
    missed_flagged: int
    services_flagged: int = 0
    flag_precision: float | None = None
    cause_precision: float | None = None


class BenchReport(BaseModel):
    apps: dict[str, Headline]
    pooled: Headline
    pooled_held_out: Headline | None = None
