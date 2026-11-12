"""Identity resolution: map raw service references onto registered FQURIs.

Every service is identified by a FQURI
(``urn:release-coord:{tenant}:{environment}:{cluster}:{namespace}:{name}``,
see ``ServiceIdentity``). Static-code and telemetry evidence name services by
other means: a DNS hostname (``order-service``, ``order-service:3000`` or
``order-service.default.svc.cluster.local``), the older ``<namespace>/<name>``
form, a FQURI with unknown segments, or something that is not a service at
all (an IP address, an external or private DNS name). These helpers turn a
reference into a partial identity and resolve it against the inventory.
A reference resolves only when exactly one registered service matches;
anything else is classified as an ``UnresolvedEntityKind``.
"""

import ipaddress
import re
from collections.abc import Iterable, Mapping
from typing import Final
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError

from src.core.models import (
    IDENTITY_SEGMENT_PATTERN,
    UNKNOWN_LABEL,
    ServiceIdentity,
    UnresolvedEntity,
    UnresolvedEntityKind,
    unresolved_entity_id,
)

_CLUSTER_DNS_MARKER: Final[str] = "svc"
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Dotted quads, including redacted placeholders such as x.x.x.x.
_IP_LIKE_PATTERN = re.compile(r"^[0-9x*]{1,3}(?:\.[0-9x*]{1,3}){3}$")
# DNS labels that mark a private or corporate zone wherever they appear,
# e.g. db.corp.internal or payments.internal.example.com.
_PRIVATE_DNS_LABELS: Final[frozenset[str]] = frozenset(
    {"corp", "internal", "intranet", "lan", "local", "localdomain", "private", "privatelink"}
)
_PRIVATE_DNS_SUFFIXES: Final[tuple[str, ...]] = (".home.arpa",)
ENV_REFERENCE_PREFIX: Final[str] = "env:"


def env_var_reference(service_name: str, variable: str) -> str:
    """Return the edge target for an environment variable read by a service.

    Scoped per service (``env:<service>/<VARIABLE>``) because each service
    configures its own value for a variable name.
    """
    return f"{ENV_REFERENCE_PREFIX}{service_name}/{variable}"


def is_env_var_reference(reference: str) -> bool:
    """Return True for references built by ``env_var_reference``."""
    return reference.strip().lower().startswith(ENV_REFERENCE_PREFIX)


def parse_env_var_reference(reference: str) -> tuple[str, str] | None:
    """Split an ``env:<service>/<VARIABLE>`` reference into its two parts.

    Returns None for anything that is not a well-formed env reference.
    """
    raw = reference.strip()
    if not is_env_var_reference(raw):
        return None
    service_name, separator, variable = raw[len(ENV_REFERENCE_PREFIX) :].partition("/")
    if not (separator and service_name and variable):
        return None
    return service_name, variable


def is_private_dns_host(host: str) -> bool:
    """Return True for dotted hostnames in a private or corporate DNS zone."""
    host = host.lower().rstrip(".")
    if "." not in host:
        return False
    return host.endswith(_PRIVATE_DNS_SUFFIXES) or bool(
        _PRIVATE_DNS_LABELS.intersection(host.split("."))
    )


class IdentityContext(BaseModel):
    """Identity segments known for the discovery run resolving references.

    ``default_namespace`` is used for bare names when exactly one namespace
    was scanned, mirroring how Kubernetes resolves short names in-namespace.
    """

    tenant: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    environment: str = Field(default=UNKNOWN_LABEL, pattern=IDENTITY_SEGMENT_PATTERN)
    cluster: str = Field(default=UNKNOWN_LABEL, pattern=IDENTITY_SEGMENT_PATTERN)
    default_namespace: str | None = None

    def identity(self, service_name: str, namespace: str | None = None) -> ServiceIdentity:
        """Return the identity of ``service_name`` within this context.

        Raises:
            pydantic.ValidationError: if a segment is not a valid FQURI segment.
        """
        return ServiceIdentity(
            tenant=self.tenant,
            environment=self.environment,
            cluster=self.cluster,
            namespace=namespace or UNKNOWN_LABEL,
            service_name=service_name,
        )


class IdentityIndex:
    """Lookup of registered service identities supporting partial queries."""

    def __init__(self, identities: Iterable[ServiceIdentity]) -> None:
        self._identities = list(dict.fromkeys(identities))
        self._exact = set(self._identities)

    def resolve(self, query: ServiceIdentity) -> ServiceIdentity | None:
        """Return the single registered identity ``query`` refers to, or None.

        An exact match wins. Otherwise ``unknown`` segments in ``query`` act
        as wildcards, and the query resolves only if exactly one registered
        identity matches; ambiguous queries are never resolved by guessing.
        """
        if query in self._exact:
            return query
        matches = [identity for identity in self._identities if query.matches(identity)]
        return matches[0] if len(matches) == 1 else None


def _host_of(reference: str) -> str:
    """Return the lower-case host part of a hostname, ``host:port`` or URL."""
    raw = reference.strip().lower()
    if "://" in raw:
        try:
            return (urlsplit(raw).hostname or "").rstrip(".")
        except ValueError:
            return ""
    raw = raw.split("/", 1)[0]
    if raw.startswith("["):
        return raw[1:].split("]", 1)[0]
    if raw.count(":") > 1:  # bare IPv6 address
        return raw
    return raw.split(":", 1)[0].rstrip(".")


def _is_ip(host: str) -> bool:
    if _IP_LIKE_PATTERN.match(host):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def parse_reference(reference: str, context: IdentityContext) -> ServiceIdentity | None:
    """Return the (possibly partial) identity a raw reference names.

    Accepted forms: a FQURI, ``<namespace>/<name>``, a cluster DNS name
    (``name.ns.svc[.cluster.local]``, with optional port), or a bare service
    name (with optional port). Returns None for references that cannot name
    an in-cluster service, such as IP addresses, other dotted hosts and
    environment-variable references.
    """
    raw = reference.strip()
    if is_env_var_reference(raw):
        return None
    try:
        return ServiceIdentity.parse(raw)
    except ValueError:
        pass
    namespace, separator, name = raw.lower().partition("/")
    if separator and _DNS_LABEL_PATTERN.match(namespace) and _DNS_LABEL_PATTERN.match(name):
        return context.identity(name, namespace)
    host = _host_of(raw)
    if not host or _is_ip(host):
        return None
    labels = host.split(".")
    try:
        if len(labels) >= 3 and labels[2] == _CLUSTER_DNS_MARKER:
            if _DNS_LABEL_PATTERN.match(labels[0]) and _DNS_LABEL_PATTERN.match(labels[1]):
                return context.identity(labels[0], labels[1])
            return None
        if len(labels) == 1 and _DNS_LABEL_PATTERN.match(host):
            return context.identity(host)
    except ValidationError:
        return None
    return None


def resolve_reference(
    reference: str, index: IdentityIndex, context: IdentityContext
) -> ServiceIdentity | None:
    """Resolve a raw reference to exactly one registered identity, or None.

    A name whose namespace is unknown resolves only within
    ``default_namespace`` when the context has one, as Kubernetes resolves
    short names in the caller's namespace: a service elsewhere is never
    chosen just because its name is unique. Without a known namespace, the
    name must match exactly one registered service in any namespace.
    """
    query = parse_reference(reference, context)
    if query is None:
        return None
    if query.namespace == UNKNOWN_LABEL and context.default_namespace:
        query = query.model_copy(update={"namespace": context.default_namespace})
    return index.resolve(query)


def resolve_env_var_reference(
    reference: str,
    environments: Mapping[str, Mapping[str, str]],
    index: IdentityIndex,
    context: IdentityContext,
) -> ServiceIdentity | None:
    """Resolve ``env:<service>/<VARIABLE>`` through its runtime value.

    ``environments`` maps service FQURIs to the variables Kubernetes sets
    for them. The owning service must resolve to exactly one registered
    service, define ``VARIABLE`` (exact, case-sensitive name), and the value
    must itself resolve to exactly one registered service. Bare names in the
    value resolve within the owning service's namespace, as in-cluster DNS
    does. Returns None when any step fails, so the reference stays unresolved.
    """
    parsed = parse_env_var_reference(reference)
    if parsed is None:
        return None
    service_name, variable = parsed
    owner = resolve_reference(service_name, index, context)
    if owner is None:
        return None
    value = environments.get(owner.uri, {}).get(variable)
    if value is None:
        return None
    owner_context = context.model_copy(update={"default_namespace": owner.namespace})
    return resolve_reference(value, index, owner_context)


def classify_unresolved(reference: str) -> UnresolvedEntityKind:
    """Classify why ``reference`` is not a registered service."""
    if is_env_var_reference(reference):
        return UnresolvedEntityKind.ENVIRONMENT_VARIABLE
    host = _host_of(reference)
    if host and _is_ip(host):
        return UnresolvedEntityKind.IP_ADDRESS
    labels = host.split(".")
    if "." not in host or (len(labels) >= 3 and labels[2] == _CLUSTER_DNS_MARKER):
        return UnresolvedEntityKind.UNREGISTERED_SERVICE
    if is_private_dns_host(host):
        return UnresolvedEntityKind.PRIVATE_DNS
    return UnresolvedEntityKind.EXTERNAL_DNS


def unresolved_entity(reference: str) -> UnresolvedEntity:
    """Build the unresolved-entity node for a raw reference."""
    normalized = reference.strip().lower()
    return UnresolvedEntity(
        entity_id=unresolved_entity_id(normalized),
        reference=normalized,
        kind=classify_unresolved(normalized),
    )
