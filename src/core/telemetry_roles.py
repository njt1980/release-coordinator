"""Opt-in telemetry filter by role name and role instance.

A project policy may ask that only telemetry rows whose role name is in the
service inventory, and/or whose role instance is one of the pod names the
Kubernetes scan read, count as evidence. This narrows the rows; it does
**not** prove which cluster they came from: another cluster running the same
workload names reports the same role names, and a pod name is only unique
within its own cluster's namespace. Only a ``TelemetryScope`` (cluster
attribute or Application Insights components) scopes telemetry to a cluster;
this filter is applied on top of it.
"""

import re
from collections.abc import Iterable
from typing import Final, List, Optional

from pydantic import BaseModel, field_validator

# Names kept in a filter are interpolated into telemetry queries, so only
# characters that cannot end a quoted string. Kubernetes object names
# (DNS-1123) and the usual service names fit.
ROLE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]{0,252}$")

NOT_A_CLUSTER_PROOF: Final[str] = (
    "a filter only: it does not prove which cluster the rows came from"
)


class TelemetryRoleFilter(BaseModel):
    """Which role filters a project opts into (see the module docstring).

    ``role_names_in_inventory`` keeps rows whose role name (``service.name``
    or ``AppRoleName``, without a ``<namespace>/`` prefix) is a name in the
    inventory: a discovered service, or a workload or container the
    Kubernetes scan read. ``role_instances_in_pods`` keeps rows whose
    ``AppRoleInstance`` is the name of a pod the scan read; pods that have
    been replaced since then don't match, so this suits a recent lookback.
    """

    model_config = {"extra": "forbid", "frozen": True}

    role_names_in_inventory: bool = False
    role_instances_in_pods: bool = False

    @property
    def enabled(self) -> bool:
        """Whether any part of the filter is switched on."""
        return self.role_names_in_inventory or self.role_instances_in_pods

    def describe(self) -> str:
        """Human-readable form, e.g. ``role name in the inventory``."""
        parts = []
        if self.role_names_in_inventory:
            parts.append("role name in the inventory")
        if self.role_instances_in_pods:
            parts.append("role instance among the scanned pods")
        return " and ".join(parts)


def _normalized_names(names: Optional[List[str]]) -> Optional[List[str]]:
    """Lower-case, sort and de-duplicate names, keeping only query-safe ones."""
    if names is None:
        return None
    return sorted(
        {name.strip().lower() for name in names if ROLE_NAME_PATTERN.match(name.strip().lower())}
    )


class TelemetryRoleAllowList(BaseModel):
    """The names one run's telemetry query keeps (see ``TelemetryRoleFilter``).

    ``role_names`` and ``role_instances`` are None when that part is not
    applied; a list (never empty) when it is. Names that aren't query-safe
    (``ROLE_NAME_PATTERN``) are left out.
    """

    model_config = {"extra": "forbid", "frozen": True}

    role_names: Optional[List[str]] = None
    role_instances: Optional[List[str]] = None

    @field_validator("role_names", "role_instances")
    @classmethod
    def _normalize(cls, names: Optional[List[str]]) -> Optional[List[str]]:
        normalized = _normalized_names(names)
        if normalized is not None and not normalized:
            raise ValueError("an applied role filter needs at least one name")
        return normalized

    @property
    def applied(self) -> TelemetryRoleFilter:
        """The filter parts this allow list applies."""
        return TelemetryRoleFilter(
            role_names_in_inventory=self.role_names is not None,
            role_instances_in_pods=self.role_instances is not None,
        )


def role_allow_list(
    role_filter: TelemetryRoleFilter,
    inventory_names: Iterable[str],
    pod_names: Optional[Iterable[str]],
) -> tuple[TelemetryRoleAllowList | None, List[str]]:
    """Return the allow list a filter makes from this run's inventory and pods.

    ``pod_names`` is None when the pods could not be read. A part with no
    usable name to keep is not applied (filtering by an empty list would drop
    every row) and a note says why; the caller reports it, so the run shows
    the filter it asked for was not fully applied.

    Returns:
        The allow list (None when no part applies) and the notes.
    """
    notes: List[str] = []
    role_names: Optional[List[str]] = None
    role_instances: Optional[List[str]] = None
    if role_filter.role_names_in_inventory:
        role_names = _normalized_names(list(inventory_names)) or None
        if role_names is None:
            notes.append(
                "Role-name telemetry filter not applied: the inventory has no usable service name"
            )
    if role_filter.role_instances_in_pods:
        if pod_names is None:
            notes.append(
                "Role-instance telemetry filter not applied: the Kubernetes pods were not read"
            )
        else:
            role_instances = _normalized_names(list(pod_names)) or None
            if role_instances is None:
                notes.append(
                    "Role-instance telemetry filter not applied: the Kubernetes scan read no pods"
                )
    if role_names is None and role_instances is None:
        return None, notes
    return TelemetryRoleAllowList(role_names=role_names, role_instances=role_instances), notes
