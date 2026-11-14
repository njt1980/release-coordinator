"""Per-project release policies loaded from checked-in YAML files.

Each project's policy lives at ``{root}/{tenant}/{project_id}.yaml``. The
file is validated strictly (unknown keys are rejected) and must name the
tenant and project its path says it belongs to. There is no default
policy: a missing or invalid file is reported as such, and the release
coordinator blocks the release.
"""

import hashlib
import ipaddress
import re
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Final, Literal, Self

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
    TelemetryScope,
    normalize_api_server,
    normalize_component_ids,
    normalize_workspace_ids,
    telemetry_scope_of,
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


_DNS_NAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Broadest network an egress rule may allow; anything wider is a mistake.
_MIN_PREFIX = {4: 8, 6: 16}


class SilentServiceExemption(_StrictModel):
    """A radius service allowed to be quiet longer than the live window.

    ``service`` is ``<namespace>/<name>``: for example a report job that
    runs weekly, or an admin tool used a few times a week. Every radius
    service must otherwise have been seen within
    ``max_telemetry_age_minutes``. With ``max_quiet_days`` it must have
    been seen within that many days instead; without it, it may go unseen
    for the whole lookback. ``expires_on`` is the last day (UTC) the entry
    applies, so every exception is re-reviewed; an expired entry is
    ignored and reported.
    """

    service: str
    reason: str = Field(min_length=1)
    expires_on: date
    max_quiet_days: int | None = Field(default=None, ge=1, le=config.MAX_RUNTIME_LOOKBACK_DAYS)

    def is_active(self, today: date) -> bool:
        """Whether the entry still applies on ``today``."""
        return today <= self.expires_on

    @field_validator("service")
    @classmethod
    def _namespaced_name(cls, value: str) -> str:
        namespace, separator, name = value.strip().lower().partition("/")
        if not (separator and _DNS_LABEL.match(namespace) and _DNS_LABEL.match(name)):
            raise ValueError(f"service must be '<namespace>/<name>': {value!r}")
        return f"{namespace}/{name}"


class RegulatedService(_StrictModel):
    """A service whose changes may need regulatory review.

    ``service`` is ``<namespace>/<name>``; ``classification`` says why it is
    regulated (for example ``IEC 62304 class B``, ``SaMD``), as the
    manufacturer's own records state it. A pull request that changes such a
    service, or adds or removes a dependency to or from it, is flagged in the
    briefing as a possible significant change: whether it is one is the
    manufacturer's regulatory decision, never the tool's.
    """

    service: str
    classification: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=1000)

    @field_validator("service")
    @classmethod
    def _namespaced_name(cls, value: str) -> str:
        return SilentServiceExemption._namespaced_name(value)


class UnobservableProtocolExemption(_StrictModel):
    """A protocol the configured telemetry can't see, accepted for review.

    A radius service talking only protocols outside the telemetry's
    capability is never observed, so live coverage can't vouch for it
    (RUNTIME_UNOBSERVABLE). An entry accepts that blind spot for one
    ``protocol`` (as named in assessments: ``amqp``, ``amqps``,
    ``unknown``...) until ``expires_on`` (UTC, inclusive), with a reason:
    for example that the service's consumers were checked by hand. It never
    makes the protocol observable, and a silent-service allowance never
    replaces it.
    """

    protocol: str = Field(pattern=r"^[a-z][a-z0-9+.-]*$")
    reason: str = Field(min_length=1)
    expires_on: date

    def is_active(self, today: date) -> bool:
        """Whether the entry still applies on ``today``."""
        return today <= self.expires_on


class EgressRule(_StrictModel):
    """An external destination the project calls on purpose (gap G2).

    Exactly one of: ``host`` (one DNS name), ``host_suffix`` (a leading-dot
    zone such as ``.blob.core.windows.net`` matching every name in it) or
    ``cidr`` (an IP network, at most /8 for IPv4 and /16 for IPv6). Host
    rules match only unresolved DNS names and CIDR rules only IP addresses:
    an IP-only sighting of an allowed host still blocks.
    """

    host: str | None = None
    host_suffix: str | None = None
    cidr: str | None = None
    reason: str = Field(min_length=1)

    @field_validator("host")
    @classmethod
    def _valid_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        host = value.strip().lower().rstrip(".")
        if not _DNS_NAME.match(host):
            raise ValueError(f"host must be a DNS name without port or wildcard: {value!r}")
        return host

    @field_validator("host_suffix")
    @classmethod
    def _valid_suffix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        suffix = value.strip().lower().rstrip(".")
        if not suffix.startswith(".") or not _DNS_NAME.match(suffix[1:]):
            raise ValueError(
                f"host_suffix must be a zone with a leading dot, e.g. '.example.com': {value!r}"
            )
        return suffix

    @field_validator("cidr")
    @classmethod
    def _valid_cidr(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            network = ipaddress.ip_network(value.strip(), strict=True)
        except ValueError as error:
            raise ValueError(f"cidr must be an IP network: {error}") from error
        if network.prefixlen < _MIN_PREFIX[network.version]:
            raise ValueError(f"cidr {value!r} is too broad")
        return str(network)

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        given = [v for v in (self.host, self.host_suffix, self.cidr) if v is not None]
        if len(given) != 1:
            raise ValueError("an egress rule names exactly one of host, host_suffix or cidr")
        return self

    @property
    def label(self) -> str:
        """How the rule is cited in an assessment."""
        if self.host is not None:
            return f"egress host {self.host}"
        if self.host_suffix is not None:
            return f"egress host_suffix {self.host_suffix}"
        return f"egress cidr {self.cidr}"

    def allows_host(self, host: str) -> bool:
        """Whether this rule allows the DNS name ``host``."""
        host = host.lower().rstrip(".")
        if self.host is not None:
            return host == self.host
        return self.host_suffix is not None and host.endswith(self.host_suffix)

    def allows_ip(self, address: str) -> bool:
        """Whether this rule allows the IP ``address``."""
        if self.cidr is None:
            return False
        try:
            return ipaddress.ip_address(address) in ipaddress.ip_network(self.cidr)
        except ValueError:
            return False


class SourceControl(_StrictModel):
    """Where the project's code lives, as the operator configured it.

    Pull request revisions are verified against this organization and
    project only, never against a caller-supplied URL.
    """

    provider: Literal["azure_devops"] = "azure_devops"
    organization_url: str = Field(pattern=r"^https://[^\s/]+(?:/[^\s/]+)*$")
    project: str = Field(min_length=1)

    @field_validator("organization_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class Exemptions(_StrictModel):
    """Explicit allowlist of evidence gaps a project accepts, each justified.

    ``unscanned_files`` are source files the static scan can't read (an
    unsupported language) whose dependencies a reviewer has checked; the
    same path globs as ``files``. ``unobservable`` are protocols the
    telemetry can't see, accepted per protocol (RUNTIME_UNOBSERVABLE).
    """

    files: list[FileExemption] = Field(default_factory=list)
    dependencies: list[DependencyExemption] = Field(default_factory=list)
    silent_services: list[SilentServiceExemption] = Field(default_factory=list)
    unscanned_files: list[FileExemption] = Field(default_factory=list)
    unobservable: list[UnobservableProtocolExemption] = Field(default_factory=list)


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
    from this cluster, ``telemetry_components`` to rows written by these
    Application Insights components (resource IDs); one of them is required
    when RUNTIME_TELEMETRY is a required source or runtime evidence must be
    live. Classic SDK and Azure Monitor Distro rows carry no cluster
    attribute, so components are their only scope (see ``TelemetryScope``).
    ``telemetry_additional_workspaces`` are Log Analytics workspaces read
    together with the onboarded one, in the same query, for estates whose
    Application Insights components send to several workspaces.

    ``runtime_lookback_days`` is how far back every telemetry query reads.
    ``max_telemetry_age_minutes`` bounds how old the newest live row may be
    for live evidence to count. ``egress`` lists external destinations the
    project calls on purpose. ``source_control`` is where pull request
    revisions are verified; without it they can't be (REVISION_UNVERIFIED).
    ``regulated_services`` are services whose changes are flagged for a
    regulatory-review decision (see ``RegulatedService``).
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
    telemetry_components: list[str] = Field(default_factory=list)
    telemetry_additional_workspaces: list[str] = Field(default_factory=list)
    runtime_lookback_days: int = Field(
        default=config.DEFAULT_RUNTIME_LOOKBACK_DAYS,
        ge=1,
        le=config.MAX_RUNTIME_LOOKBACK_DAYS,
    )
    max_telemetry_age_minutes: int = Field(
        default=config.DEFAULT_MAX_TELEMETRY_AGE_MINUTES, ge=5, le=1440
    )
    egress: list[EgressRule] = Field(default_factory=list)
    source_control: SourceControl | None = None
    regulated_services: list[RegulatedService] = Field(default_factory=list)

    @field_validator("kubernetes_api_server")
    @classmethod
    def _normalize_api_server(cls, value: str) -> str:
        return normalize_api_server(value) if value else ""

    @field_validator("telemetry_components")
    @classmethod
    def _normalize_components(cls, components: list[str]) -> list[str]:
        return normalize_component_ids(components)

    @field_validator("telemetry_additional_workspaces")
    @classmethod
    def _normalize_workspaces(cls, workspaces: list[str]) -> list[str]:
        return normalize_workspace_ids(workspaces)

    @property
    def telemetry_scope(self) -> TelemetryScope | None:
        """Which telemetry rows count for this cluster; None when unscoped."""
        return telemetry_scope_of(self.telemetry_cluster, self.telemetry_components)

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
        if DiscoveryMethod.HUMAN_DECLARED in self.required_sources:
            raise ValueError("HUMAN_DECLARED is not an automated source and can't be required")
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
        if needs_telemetry and self.telemetry_scope is None:
            raise ValueError(
                "telemetry_cluster or telemetry_components is required when "
                "RUNTIME_TELEMETRY is a required source or runtime_evidence is live"
            )
        return self

    @model_validator(mode="after")
    def _silent_services_are_checkable(self) -> Self:
        services = [rule.service for rule in self.exemptions.silent_services]
        if len(set(services)) != len(services):
            raise ValueError("exemptions.silent_services must list each service once")
        for rule in self.exemptions.silent_services:
            if rule.max_quiet_days and rule.max_quiet_days > self.runtime_lookback_days:
                raise ValueError(
                    f"{rule.service}: max_quiet_days {rule.max_quiet_days} exceeds "
                    f"runtime_lookback_days {self.runtime_lookback_days}, so it can't be checked"
                )
        return self

    @model_validator(mode="after")
    def _unobservable_protocols_are_unique(self) -> Self:
        protocols = [rule.protocol for rule in self.exemptions.unobservable]
        if len(set(protocols)) != len(protocols):
            raise ValueError("exemptions.unobservable must list each protocol once")
        return self

    def unobservable_rule(self, protocol: str) -> UnobservableProtocolExemption | None:
        """The exemption entry for ``protocol``, active or not."""
        return next((rule for rule in self.exemptions.unobservable if rule.protocol == protocol), None)

    @model_validator(mode="after")
    def _regulated_services_are_unique(self) -> Self:
        services = [entry.service for entry in self.regulated_services]
        if len(set(services)) != len(services):
            raise ValueError("regulated_services must list each service once")
        return self

    def regulated_service(self, service: str) -> RegulatedService | None:
        """Return the regulated-service entry for ``<namespace>/<name>``, if any."""
        return next((entry for entry in self.regulated_services if entry.service == service), None)

    def silent_service_rule(self, service: str) -> SilentServiceExemption | None:
        """Return the silent-service entry for ``<namespace>/<name>``, if any."""
        return next(
            (rule for rule in self.exemptions.silent_services if rule.service == service), None
        )


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
    config.DATA_ROOT / config.POLICIES_DIR
)


def default_policy_store() -> PolicyStore:
    """Return the store rooted at the project's ``policies`` directory."""
    return PolicyStore(DEFAULT_POLICIES_ROOT)
