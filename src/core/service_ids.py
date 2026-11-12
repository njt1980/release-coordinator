"""Canonical service identifiers shared by services and dependency edges.

Kubernetes-discovered services are identified as ``<namespace>/<name>``.
Static-code and telemetry evidence usually names a service by its DNS
hostname instead (``order-service``, ``order-service:3000`` or
``order-service.default.svc.cluster.local``). These helpers map such
references onto the namespaced form so that edges join to service nodes.
"""

from collections.abc import Iterable, Mapping

_CLUSTER_DNS_MARKER = "svc"


def build_name_index(service_ids: Iterable[str]) -> dict[str, str]:
    """Map bare service names to namespaced IDs where the name is unambiguous.

    Only namespaced IDs (``ns/name``) are indexed; a name that appears in
    more than one namespace is left out so it is never resolved by guessing.
    """
    candidates: dict[str, set[str]] = {}
    for service_id in service_ids:
        namespace, separator, name = service_id.partition("/")
        if separator and namespace and name:
            candidates.setdefault(name, set()).add(service_id)
    return {
        name: next(iter(ids)) for name, ids in candidates.items() if len(ids) == 1
    }


def canonical_service_id(
    raw_id: str,
    name_index: Mapping[str, str],
    default_namespace: str | None = None,
) -> str:
    """Return the ``<namespace>/<name>`` form of a service reference.

    Resolution order:
      1. IDs that are already namespaced are returned unchanged.
      2. Cluster DNS names (``name.ns.svc[.cluster.local]``) use their namespace.
      3. Bare names resolve through ``name_index`` (discovered services).
      4. Bare names fall back to ``default_namespace`` when one is given,
         mirroring how Kubernetes resolves short names in-namespace.
    Anything else (for example an external FQDN) is returned unchanged.
    """
    if "/" in raw_id:
        return raw_id
    host = raw_id.strip().lower().split(":", 1)[0]
    labels = host.split(".")
    if len(labels) >= 3 and labels[2] == _CLUSTER_DNS_MARKER:
        return f"{labels[1]}/{labels[0]}"
    if len(labels) == 1 and host:
        if host in name_index:
            return name_index[host]
        if default_namespace:
            return f"{default_namespace}/{host}"
    return raw_id
