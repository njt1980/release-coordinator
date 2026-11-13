"""Per-project release policies loaded from checked-in YAML files.

Each project's policy lives at ``{root}/{tenant}/{project_id}.yaml``. The
file is validated strictly (unknown keys are rejected) and must name the
tenant and project its path says it belongs to. There is no default
policy: a missing or invalid file is reported as such, and the release
coordinator blocks the release.
"""

import hashlib
import re
from enum import Enum
from pathlib import Path
from typing import Final, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src.core import config
from src.core.models import (
    IDENTITY_SEGMENT_PATTERN,
    UNKNOWN_LABEL,
    UNRESOLVED_ID_PREFIX,
    DiscoveryMethod,
    TelemetryClusterFilter,
    normalize_api_server,
)
from src.tools.baseline_store import BaselineScope

_POLICY_SUFFIX: Final[str] = ".yaml"


class RuntimeEvidenceRequirement(str, Enum):
    """The weakest runtime evidence a project accepts.

    LIVE: a completed live telemetry query for this assessment.
    BASELINE: live evidence, or runtime edges from a baseline whose
        RUNTIME_TELEMETRY stage succeeded.
    """

    LIVE = "live"
    BASELINE = "baseline"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_GLOB_FORBIDDEN: Final[frozenset[str]] = frozenset("[]\\")


def compile_path_glob(pattern: str) -> re.Pattern[str]:
    """Compile a repository path glob into a full-match regular expression.

    Matching is per path segment and case-sensitive: ``*`` matches any run
    of characters within one segment and ``?`` one character, neither ever
    matching ``/``. A segment that is exactly ``**`` matches any number of
    whole segments (zero or more in the middle or at the start, one or more
    at the end). So ``docs/*`` matches ``docs/a.md`` but not
    ``docs/a/b.md``, which ``docs/**`` matches.

    Raises:
        ValueError: if the pattern is absolute, has empty, ``.`` or ``..``
            segments, uses ``[``, ``]`` or ``\\``, or mixes ``**`` with other
            characters in one segment.
    """
    if not pattern or pattern.startswith("/"):
        raise ValueError(f"pattern must be a relative path: {pattern!r}")
    if _GLOB_FORBIDDEN.intersection(pattern):
        raise ValueError(f"pattern must not use '[', ']' or '\\': {pattern!r}")
    segments = pattern.split("/")
    parts: list[str] = []
    for position, segment in enumerate(segments):
        last = position == len(segments) - 1
        if segment in ("", ".", ".."):
            raise ValueError(f"pattern has an empty, '.' or '..' segment: {pattern!r}")
        if segment == "**":
            parts.append("(?:[^/]+/)*[^/]+" if last else "(?:[^/]+/)*")
            continue
        if "**" in segment:
            raise ValueError(f"'**' must be a whole segment: {pattern!r}")
        body = "".join(
            "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
            for char in segment
        )
        parts.append(body if last else f"{body}/")
    return re.compile("".join(parts))


class FileExemption(_StrictModel):
    """Changed files matching ``pattern`` need not be owned by a service.

    ``pattern`` is a path glob (see ``compile_path_glob``) matched against
    the repository-relative path: ``*`` and ``?`` stay within one segment,
    and a ``**`` segment spans directories.
    """

    pattern: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def _valid_glob(cls, value: str) -> str:
        compile_path_glob(value)
        return value

    def matches(self, path: str) -> bool:
        """Whether ``path`` matches this exemption's pattern."""
        return compile_path_glob(self.pattern).fullmatch(path) is not None


class DependencyExemption(_StrictModel):
    """The unresolved dependency ``entity_id`` is not a discovery gap."""

    entity_id: str = Field(min_length=len(UNRESOLVED_ID_PREFIX) + 1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _is_unresolved_entity(self) -> Self:
        if not self.entity_id.startswith(UNRESOLVED_ID_PREFIX):
            raise ValueError(
                f"entity_id must start with {UNRESOLVED_ID_PREFIX!r}: {self.entity_id!r}"
            )
        return self


class Exemptions(_StrictModel):
    """Explicit allowlist of evidence gaps a project accepts, each justified."""

    files: list[FileExemption] = Field(default_factory=list)
    dependencies: list[DependencyExemption] = Field(default_factory=list)


class ProjectPolicy(_StrictModel):
    """Release eligibility rules for one tenant/project.

    ``required_sources`` must each have SUCCEEDED in the baseline snapshot
    (a SKIPPED stage does not count). ``max_baseline_age_hours`` bounds the
    snapshot's age at assessment time. ``environment`` and ``cluster`` name
    the one deployment target the project releases to; the baseline must
    have scanned exactly that target.

    ``environment`` and ``cluster`` are labels; two fields prove them.
    ``kubernetes_api_server`` is the API server the manifest scan must have
    read (required when K8S_MANIFEST is a required source).
    ``telemetry_cluster`` restricts every telemetry query to rows emitted
    from this cluster (required when RUNTIME_TELEMETRY is a required source
    or runtime evidence must be live).
    """

    policy_version: int = Field(ge=1)
    tenant: str
    project_id: str
    environment: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    cluster: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    max_baseline_age_hours: int = Field(
        default=config.MAX_BASELINE_AGE_HOURS,
        ge=1,
        le=config.MAX_BASELINE_AGE_HOURS,
    )
    required_sources: list[DiscoveryMethod] = Field(min_length=1)
    runtime_evidence: RuntimeEvidenceRequirement
    exemptions: Exemptions = Field(default_factory=Exemptions)
    kubernetes_api_server: str = ""
    telemetry_cluster: TelemetryClusterFilter | None = None

    @field_validator("kubernetes_api_server")
    @classmethod
    def _normalize_api_server(cls, value: str) -> str:
        return normalize_api_server(value) if value else ""

    @field_validator("environment", "cluster")
    @classmethod
    def _reject_unknown(cls, value: str) -> str:
        if value == UNKNOWN_LABEL:
            raise ValueError(f"must name a real deployment target, not {UNKNOWN_LABEL!r}")
        return value

    @model_validator(mode="after")
    def _sources_are_unique(self) -> Self:
        if len(set(self.required_sources)) != len(self.required_sources):
            raise ValueError("required_sources must not repeat a source")
        return self

    @model_validator(mode="after")
    def _cluster_is_provable(self) -> Self:
        if (
            DiscoveryMethod.K8S_MANIFEST in self.required_sources
            and not self.kubernetes_api_server
        ):
            raise ValueError(
                "kubernetes_api_server is required when K8S_MANIFEST is a "
                "required source"
            )
        needs_telemetry = (
            DiscoveryMethod.RUNTIME_TELEMETRY in self.required_sources
            or self.runtime_evidence is RuntimeEvidenceRequirement.LIVE
        )
        if needs_telemetry and self.telemetry_cluster is None:
            raise ValueError(
                "telemetry_cluster is required when RUNTIME_TELEMETRY is a "
                "required source or runtime_evidence is live"
            )
        return self


class LoadedPolicy(BaseModel):
    """A validated policy and the digest of the file it was read from."""

    policy: ProjectPolicy
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PolicyMissingError(LookupError):
    """Raised when a project has no policy file."""


class PolicyInvalidError(ValueError):
    """Raised when a policy file cannot be parsed or fails validation."""


def parse_policy(text: str, scope: BaselineScope) -> ProjectPolicy:
    """Parse and validate policy YAML for ``scope``.

    Raises:
        PolicyInvalidError: if the YAML is malformed, fails validation, or
            names a different tenant or project.
    """
    try:
        data = yaml.safe_load(text)
        policy = ProjectPolicy.model_validate(data)
    except (yaml.YAMLError, ValidationError) as error:
        raise PolicyInvalidError(str(error)) from error
    if (policy.tenant, policy.project_id) != (scope.tenant, scope.project_id):
        raise PolicyInvalidError(
            f"policy names {policy.tenant}/{policy.project_id}, "
            f"expected {scope.tenant}/{scope.project_id}"
        )
    return policy


class PolicyStore:
    """Filesystem store of per-project policies."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def policy_path(self, scope: BaselineScope) -> Path:
        """Return where ``scope``'s policy file lives."""
        return self._root / scope.tenant / f"{scope.project_id}{_POLICY_SUFFIX}"

    def load(self, scope: BaselineScope) -> LoadedPolicy:
        """Load and validate ``scope``'s policy.

        Raises:
            PolicyMissingError: if the policy file does not exist.
            PolicyInvalidError: if it cannot be read or is invalid.
        """
        path = self.policy_path(scope)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as error:
            raise PolicyMissingError(
                f"no release policy at {config.POLICIES_DIR}/{scope.tenant}/"
                f"{scope.project_id}{_POLICY_SUFFIX}"
            ) from error
        except OSError as error:
            raise PolicyInvalidError(f"cannot read policy: {error}") from error
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PolicyInvalidError(f"policy is not UTF-8: {error}") from error
        return LoadedPolicy(
            policy=parse_policy(text, scope),
            sha256=hashlib.sha256(raw).hexdigest(),
        )


DEFAULT_POLICIES_ROOT: Final[Path] = (
    Path(__file__).resolve().parents[2] / config.POLICIES_DIR
)


def default_policy_store() -> PolicyStore:
    """Return the store rooted at the project's ``policies`` directory."""
    return PolicyStore(DEFAULT_POLICIES_ROOT)
