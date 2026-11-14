"""Read-only client for extracting service dependency telemetry."""

import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from collections.abc import Sequence
from typing import Any, TypeAlias

from azure.identity import DefaultAzureCredential
from azure.monitor.query import (
    LogsQueryClient,
    LogsQueryPartialResult,
    LogsQueryResult,
    LogsQueryStatus,
    LogsTable,
)
from pydantic import BaseModel, Field, ValidationError

from src.core import config
from src.core.models import (
    MOCK_FIXTURE_PROVENANCE,
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryResult,
    EdgeResolution,
    TargetDetail,
    TargetDetailKind,
    TelemetryRoleProfile,
    TelemetryScope,
    normalize_workspace_ids,
)
from src.core.telemetry_capability import telemetry_stack
from src.tools.telemetry_normalization import DropReason, normalize_edges, parse_target
from src.tools._timeout import (
    TIMEOUT_MESSAGE,
    VERIFY_TIMEOUT_SECONDS,
    run_with_timeout,
)


logger = logging.getLogger(__name__)
RuntimeMetrics: TypeAlias = dict[str, dict[str, int | float]]
_REQUIRED_COLUMNS = frozenset({"Source", "Target", "LastObservedAt"})
_NAMESPACE_COLUMN = "SourceNamespace"
_CALLS_COLUMN = "Calls"
_FAILURES_COLUMN = "Failures"
_TYPE_COLUMN = "DependencyType"
_NOISE_COLUMN = "Noise"
_CALLEE_COLUMN = "Callee"
_CALLEE_NAMESPACE_COLUMN = "CalleeNamespace"
_RESOLVED_CALLS_COLUMN = "ResolvedCalls"
_MAX_ITEM_COUNT_COLUMN = "MaxItemCount"
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# OpenTelemetry resource attributes, as Application Insights stores them in
# Properties: the workload's own name, and the Kubernetes namespace it runs
# in (service.namespace is a logical grouping in OpenTelemetry, not
# necessarily the Kubernetes namespace, so it is not used). The classic
# SDK's Kubernetes enricher (Microsoft.ApplicationInsights.Kubernetes)
# writes the namespace under its own key.
_SERVICE_NAME_PROPERTY = "service.name"
_NAMESPACE_PROPERTIES = ("k8s.namespace.name", "Kubernetes.Pod.Namespace")
AZURE_MONITOR_PROVENANCE = "azure_monitor"
MOCK_ENVIRONMENT = "mock"


class _MockDependencyRecord(BaseModel):
    """Validated shape of one AppDependencies query result row."""

    source: str = Field(alias="AppRoleName", min_length=1)
    target: str = Field(alias="Target", min_length=1)
    time_generated: datetime = Field(alias="TimeGenerated")
    success: bool = Field(alias="Success")
    dependency_type: str | None = Field(default=None, alias="DependencyType")


class TelemetryExtractor:
    """Read-only client boundary for telemetry-derived dependencies."""

    def __init__(self, workspace_id: str, additional_workspaces: Sequence[str] = ()) -> None:
        """Bind the extractor to explicit Log Analytics workspaces.

        Args:
            workspace_id: Log Analytics workspace ID that every query targets.
            additional_workspaces: workspace IDs every dependency query also
                reads, as one query (joins span them all).

        Raises:
            ValueError: If workspace_id is blank, an additional workspace isn't
                a workspace ID, or TELEMETRY_SOURCE is invalid.
        """
        if not workspace_id or not workspace_id.strip():
            raise ValueError("workspace_id is required")
        self._workspace_id = workspace_id.strip()
        self._additional_workspaces = [
            workspace
            for workspace in normalize_workspace_ids(additional_workspaces)
            if workspace != self._workspace_id.lower()
        ]

        self._source = os.getenv("TELEMETRY_SOURCE", "mock").lower()
        if self._source not in {"mock", "azure"}:
            raise ValueError("TELEMETRY_SOURCE must be either 'mock' or 'azure'")

        self._runtime_metrics: RuntimeMetrics = {}
        self._dropped_records: dict[str, int] = {}
        self._noise: Counter[str] = Counter()
        self._sampling: dict[str, int] = {}
        self._credential: DefaultAzureCredential | None = None
        self._client: LogsQueryClient | None = None
        if self._source == "azure":
            self._credential = DefaultAzureCredential()
            self._client = LogsQueryClient(self._credential)

    @property
    def is_mock(self) -> bool:
        """Whether answers come from the demo fixture instead of Azure."""
        return self._source == "mock"

    def verify_access(self) -> tuple[bool, str]:
        """Confirm query access with a trivial KQL statement that scans no data.

        In mock mode no Azure access is required, so the check passes.
        Otherwise the query is bounded by a strict VERIFY_TIMEOUT_SECONDS
        deadline.

        Returns:
            (True, "Success") when the query succeeds, otherwise
            (False, <error message>).
        """
        if self._source == "mock":
            return True, "Success"
        try:
            client = self._client
            if client is None:
                raise RuntimeError("Azure telemetry client was not initialized")
            query = "print ok=1"
            timespan = timedelta(minutes=5)
            workspace_id = self._workspace_id
            response = run_with_timeout(
                lambda: client.query_workspace(
                    workspace_id, query, timespan=timespan
                ),
                VERIFY_TIMEOUT_SECONDS,
            )
            if not hasattr(response, "tables"):
                raise RuntimeError(str(getattr(response, "error", response)))
            return True, "Success"
        except TimeoutError:
            logger.warning("Telemetry access verification timed out")
            return False, TIMEOUT_MESSAGE
        except Exception as error:
            logger.warning("Telemetry access verification failed: %s", error)
            return False, str(error)

    def get_active_dependencies(
        self,
        days_back: int = 30,
        scope: TelemetryScope | None = None,
    ) -> DiscoveryResult[DependencyEdge]:
        """Return active dependency edges observed within a lookback period.

        With ``scope``, only rows from that cluster and/or those
        Application Insights components are read (see
        ``build_dependency_query``); the mock fixture ignores it. A source whose Kubernetes
        namespace is known is named ``<namespace>/<name>``.

        The local source reads AppDependencies-shaped records from the root
        mock_telemetry.json fixture. Set TELEMETRY_SOURCE=azure to use Azure.
        Mock edges are labeled ``provenance="mock_fixture"`` and
        ``environment="mock"`` with a confidence of 0.0: they are demo data,
        not evidence of real traffic.

        Targets are normalized and noise is dropped (see
        ``telemetry_normalization``); ``dropped_records`` counts what was
        dropped from the last call.

        Returns:
            SUCCESS with every observed edge, PARTIAL when some records or
            tables could not be used (or Azure reported a partial error), or
            FAILED with no edges when the source could not be read.
        """
        if days_back <= 0:
            raise ValueError("days_back must be a positive integer")
        if self._source == "mock":
            result = self._get_active_dependencies_from_mock()
        else:
            result = self._get_active_dependencies_from_azure(days_back, scope)
        edges, dropped = normalize_edges(result.items)
        for reason, count in self._noise.items():
            dropped[reason] = dropped.get(reason, 0) + count
        self._dropped_records = dropped
        return result.model_copy(update={"items": edges})

    def get_unscoped_sources(
        self, days_back: int, scope: TelemetryScope | None
    ) -> dict[str, int] | None:
        """Count rows the cluster filter drops for lacking its attribute, by source.

        A diagnostic only (see ``build_unscoped_query``): never raises, and
        returns None when the scope has no cluster filter, the source is the
        mock fixture, or the query fails.
        """
        if (
            scope is None
            or scope.cluster is None
            or self._source != "azure"
            or self._client is None
        ):
            return None
        try:
            return parse_unscoped_response(
                self._query(build_unscoped_query(days_back, scope), days_back)
            )
        except Exception as error:
            logger.warning("Unscoped telemetry diagnostic failed: %s", error)
            return None

    def get_role_profiles(
        self, days_back: int, scope: TelemetryScope | None
    ) -> list[TelemetryRoleProfile] | None:
        """Describe each telemetry role in scope (see ``build_role_profile_query``).

        A diagnostic: never raises, and returns None for the mock fixture or
        when the query fails.
        """
        if self._source != "azure" or self._client is None:
            return None
        try:
            return parse_role_profile_response(
                self._query(build_role_profile_query(days_back, scope), days_back)
            )
        except Exception as error:
            logger.warning("Telemetry role profile query failed: %s", error)
            return None

    @property
    def sampled_sources(self) -> dict[str, int]:
        """Sources whose rows in the last query were sampled, with the largest ItemCount.

        A row with ItemCount n stands for n calls; any n > 1 means the
        source's telemetry is sampled, so rare calls may have no row.
        """
        return dict(self._sampling)

    def _query(self, query: str, days_back: int) -> LogsQueryResult | LogsQueryPartialResult:
        """Run a query over the bound workspaces."""
        if self._client is None:
            raise RuntimeError("Azure telemetry client was not initialized")
        return self._client.query_workspace(
            self._workspace_id,
            query,
            timespan=timedelta(days=days_back),
            additional_workspaces=self._additional_workspaces or None,
        )

    @property
    def dropped_records(self) -> dict[str, int]:
        """Rows the last query dropped as noise, counted per reason."""
        return dict(self._dropped_records)

    def get_runtime_metrics(self) -> RuntimeMetrics:
        """Return per-source call metrics from the most recent query.

        Keyed by the source reference the edges use; each entry has
        ``request_count`` and ``error_rate``.
        """
        return {
            service: metrics.copy()
            for service, metrics in self._runtime_metrics.items()
        }

    def _get_active_dependencies_from_mock(self) -> DiscoveryResult[DependencyEdge]:
        mock_path = Path(__file__).resolve().parents[2] / "mock_telemetry.json"
        self._runtime_metrics = {}
        self._noise = Counter()
        self._sampling = {}
        try:
            with mock_path.open(encoding="utf-8") as mock_file:
                payload = json.load(mock_file)
            if not isinstance(payload, dict):
                raise ValueError("mock_telemetry.json must contain a JSON object")
            raw_records = payload.get("AppDependencies")
            if not isinstance(raw_records, list):
                raise ValueError(
                    "mock_telemetry.json must contain an AppDependencies list"
                )
        except (OSError, json.JSONDecodeError, ValueError) as error:
            logger.exception(
                "Failed to load mock application dependencies from %s", mock_path
            )
            return DiscoveryResult[DependencyEdge].failed(
                f"Could not load mock telemetry: {error}"
            )

        records: list[_MockDependencyRecord] = []
        skipped = 0
        for raw_record in raw_records:
            try:
                record = _MockDependencyRecord.model_validate(raw_record)
            except (ValidationError, TypeError) as error:
                logger.warning("Skipping invalid mock dependency record: %s", error)
                skipped += 1
                continue
            if not record.source.strip() or not record.target.strip():
                logger.warning("Skipping mock dependency with an empty service name")
                skipped += 1
                continue
            records.append(record)

        latest_observations: dict[tuple[str, str, str | None], datetime] = {}
        request_counts: dict[str, int] = defaultdict(int)
        failure_counts: dict[str, int] = defaultdict(int)
        for record in records:
            edge_key = (record.source, record.target, record.dependency_type)
            latest_observations[edge_key] = max(
                latest_observations.get(edge_key, record.time_generated),
                record.time_generated,
            )
            request_counts[record.source] += 1
            if not record.success:
                failure_counts[record.source] += 1

        edges = [
            DependencyEdge(
                source_service_id=source,
                target_service_id=target,
                discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
                confidence_score=0.0,
                last_observed_at=observed_at,
                provenance=MOCK_FIXTURE_PROVENANCE,
                environment=MOCK_ENVIRONMENT,
                dependency_type=dependency_type,
            )
            for (source, target, dependency_type), observed_at in latest_observations.items()
        ]
        self._runtime_metrics = {
            service: {
                "error_rate": failure_counts[service] / count,
                "request_count": count,
            }
            for service, count in request_counts.items()
        }
        errors = (
            [f"Skipped {skipped} invalid mock dependency record(s)"] if skipped else []
        )
        return DiscoveryResult[DependencyEdge].from_items(edges, errors)

    def _get_active_dependencies_from_azure(
        self, days_back: int, scope: TelemetryScope | None
    ) -> DiscoveryResult[DependencyEdge]:
        """Query Azure Monitor for observed dependency edges."""
        query = build_dependency_query(days_back, scope)
        self._runtime_metrics = {}
        self._noise = Counter()
        self._sampling = {}
        try:
            response = self._query(query, days_back)
        except Exception as error:
            logger.exception("Failed to query active application dependencies")
            return DiscoveryResult[DependencyEdge].failed(
                f"Telemetry query failed: {error}"
            )
        return parse_dependency_response(
            response, self._runtime_metrics, self._noise, self._sampling
        )


# How many unscoped source names the diagnostic returns at most.
_UNSCOPED_SOURCE_LIMIT = 50


def _component_clause(scope: TelemetryScope | None) -> str:
    """KQL keeping rows written by the scope's Application Insights components."""
    if scope is None or not scope.components:
        return ""
    quoted = ", ".join(f'"{component}"' for component in scope.components)
    return f"| where _ResourceId in~ ({quoted})\n"


def _cluster_clause(scope: TelemetryScope | None) -> str:
    """KQL keeping rows whose cluster attribute equals the scope's value."""
    if scope is None or scope.cluster is None:
        return ""
    return (
        f'| where tostring(Properties["{scope.cluster.attribute}"]) == '
        f'"{scope.cluster.value}"\n'
    )


def _namespace_expression() -> str:
    """KQL for a row's Kubernetes namespace, from whichever property carries it."""
    reads = ", ".join(f'tostring(Properties["{key}"])' for key in _NAMESPACE_PROPERTIES)
    return f"coalesce({reads})"


def build_unscoped_query(days_back: int, scope: TelemetryScope) -> str:
    """Return the KQL counting dependency rows the cluster filter drops.

    Rows without the filter's attribute (for example from an Application
    Insights SDK whose exporter doesn't copy Kubernetes resource attributes)
    are never this cluster's evidence; counting them by source name explains
    why a radius service may look silent. Rows from another cluster, or
    outside the scope's components, are not counted.

    Raises:
        ValueError: if days_back isn't positive or the scope has no cluster filter.
    """
    if days_back <= 0:
        raise ValueError("days_back must be a positive integer")
    if scope.cluster is None:
        raise ValueError("the unscoped diagnostic needs a cluster filter")
    return (
        "AppDependencies\n"
        f"| where TimeGenerated >= ago({days_back}d)\n"
        f"{_component_clause(scope)}"
        f'| where isempty(tostring(Properties["{scope.cluster.attribute}"]))\n'
        f'| extend SourceName = coalesce(tostring(Properties["{_SERVICE_NAME_PROPERTY}"]), AppRoleName)\n'
        "| where isnotempty(SourceName)\n"
        "| summarize Rows=count() by Source=SourceName\n"
        f"| top {_UNSCOPED_SOURCE_LIMIT} by Rows desc\n"
    )


def parse_unscoped_response(response: LogsQueryResult | LogsQueryPartialResult) -> dict[str, int]:
    """Read ``Source``/``Rows`` pairs from a diagnostic query response.

    Raises:
        ValueError: if the response isn't a complete result with those columns.
    """
    if getattr(response, "status", None) != LogsQueryStatus.SUCCESS:
        raise ValueError(f"unscoped telemetry query status {getattr(response, 'status', None)!r}")
    counts: dict[str, int] = {}
    for table in list(getattr(response, "tables", None) or []):
        columns = list(table.columns)
        if "Source" not in columns or "Rows" not in columns:
            raise ValueError(f"table {table.name!r} lacks Source/Rows columns")
        source_index, rows_index = columns.index("Source"), columns.index("Rows")
        for row in table.rows:
            source = str(row[source_index] or "").strip()
            if source:
                counts[source] = counts.get(source, 0) + int(str(row[rows_index]))
    return counts


# A dependency to the Kubernetes API (the classic SDK's Kubernetes enricher
# reading its own pod, ReplicaSet and node): the cluster's control plane,
# not a service dependency. Matched on the request URL's path, as the API
# server's address differs per cluster.
_KUBERNETES_API_PATH = (
    r"^https?://[^/]+/(api/v1/(nodes|namespaces)|apis/[a-z0-9.-]+/v[0-9][a-z0-9]*/)"
)
KUBERNETES_API_NOISE = "kubernetes_api"


def _role_columns(prefix: str) -> str:
    """KQL naming a row's service and namespace ``<prefix>Name``/``<prefix>Namespace``."""
    return (
        f'| extend {prefix}Name = coalesce(tostring(Properties["{_SERVICE_NAME_PROPERTY}"]), AppRoleName),\n'
        f"    {prefix}Namespace = {_namespace_expression()}\n"
    )


def build_dependency_query(
    days_back: int,
    scope: TelemetryScope | None = None,
    join_days: int = config.REQUEST_JOIN_DAYS,
    join_lag_minutes: int = config.REQUEST_JOIN_LAG_MINUTES,
) -> str:
    """Return the KQL that summarizes AppDependencies into edges.

    The source is the row's ``service.name`` resource attribute, falling
    back to ``AppRoleName`` (which some exporters prefix with
    ``service.namespace``), with its Kubernetes namespace alongside (from
    ``k8s.namespace.name`` or the classic enricher's
    ``Kubernetes.Pod.Namespace``), and rows are kept apart by
    ``DependencyType`` so normalization can tell a messaging delivery from
    a call (see ``telemetry_normalization``). Calls are ``sum(ItemCount)``:
    each sampled row stands for that many calls; ``MaxItemCount`` above 1
    shows the source is sampled.

    Each (source, target, type) row also names the callees its calls
    reached: a request whose ``ParentId`` is the dependency's ``Id`` in the
    same operation (an HTTP server span, or a message consumer's processing
    span whose parent is the send). That join reads the last ``join_days``
    (at most ``days_back``) up to ``join_lag_minutes`` ago; ``ResolvedCalls``
    is how many of the row's calls it matched. Rows to the Kubernetes API
    are marked ``Noise``.

    A scope keeps only rows (dependencies and requests) from its components
    and/or whose cluster attribute equals its value; both are
    pattern-restricted so they cannot break out of the quoted strings.
    """
    if days_back <= 0 or join_days <= 0 or join_lag_minutes < 0:
        raise ValueError("days_back and join_days must be positive, join_lag_minutes not negative")
    join_days = min(join_days, days_back)
    scope_clauses = f"{_component_clause(scope)}{_cluster_clause(scope)}"
    return (
        "let deps = AppDependencies\n"
        f"| where TimeGenerated >= ago({days_back}d)\n"
        f"{scope_clauses}"
        f"{_role_columns('Source')}"
        "| where isnotempty(SourceName) and isnotempty(Target)\n"
        f'| extend {_NOISE_COLUMN} = iff(Data matches regex @"{_KUBERNETES_API_PATH}", '
        f'"{KUBERNETES_API_NOISE}", "");\n'
        "let callees = AppRequests\n"
        f"| where TimeGenerated >= ago({join_days}d)\n"
        f"{scope_clauses}"
        f"{_role_columns('Callee')}"
        "| where isnotempty(CalleeName) and isnotempty(ParentId)\n"
        "| distinct OperationId, ParentId, CalleeName, CalleeNamespace;\n"
        "let resolved = deps\n"
        f"| where isempty({_NOISE_COLUMN}) and TimeGenerated between "
        f"(ago({join_days}d) .. ago({join_lag_minutes}m))\n"
        "| project OperationId, Id, SourceName, SourceNamespace, Target, DependencyType, ItemCount\n"
        "| join kind=inner hint.strategy=shuffle (callees)\n"
        "    on $left.OperationId == $right.OperationId, $left.Id == $right.ParentId\n"
        f"| summarize {_RESOLVED_CALLS_COLUMN} = sum(ItemCount)\n"
        f"    by Source = SourceName, {_NAMESPACE_COLUMN} = SourceNamespace, Target, {_TYPE_COLUMN},\n"
        f"    {_CALLEE_COLUMN} = CalleeName, {_CALLEE_NAMESPACE_COLUMN} = CalleeNamespace;\n"
        "deps\n"
        f"| summarize LastObservedAt = max(TimeGenerated), {_CALLS_COLUMN} = sum(ItemCount),\n"
        f"    {_FAILURES_COLUMN} = sumif(ItemCount, Success == false), "
        f"{_MAX_ITEM_COUNT_COLUMN} = max(ItemCount)\n"
        f"    by Source = SourceName, {_NAMESPACE_COLUMN} = SourceNamespace, Target, {_TYPE_COLUMN}, {_NOISE_COLUMN}\n"
        f"| join kind=leftouter (resolved) on Source, {_NAMESPACE_COLUMN}, Target, {_TYPE_COLUMN}\n"
        f"| project Source, {_NAMESPACE_COLUMN}, Target, {_TYPE_COLUMN}, {_NOISE_COLUMN}, "
        f"LastObservedAt, {_CALLS_COLUMN}, {_FAILURES_COLUMN}, {_MAX_ITEM_COUNT_COLUMN},\n"
        f"    {_CALLEE_COLUMN}, {_CALLEE_NAMESPACE_COLUMN}, {_RESOLVED_CALLS_COLUMN}\n"
    )


# How many roles the profile query returns at most (busiest first).
ROLE_PROFILE_LIMIT = 1000
_PROFILE_COLUMNS = frozenset(
    {"Role", "Namespace", "RequestRows", "DependencyRows", "Represented", "MaxItemCount",
     "RowsWithNamespace", "SdkVersions", "Components", "LastSeen"}
)


def build_role_profile_query(days_back: int, scope: TelemetryScope | None = None) -> str:
    """Return the KQL describing each telemetry role in scope.

    Per role (``service.name`` or ``AppRoleName``; "" for rows without
    one): request and dependency rows, the calls they stand for
    (``sum(ItemCount)``) and the largest ``ItemCount``, how many rows carry
    a namespace and one such namespace, the ``SDKVersion`` values and the
    Application Insights components seen, and the newest row.
    """
    if days_back <= 0:
        raise ValueError("days_back must be a positive integer")
    return (
        "union AppRequests, AppDependencies\n"
        f"| where TimeGenerated >= ago({days_back}d)\n"
        f"{_component_clause(scope)}"
        f"{_cluster_clause(scope)}"
        f'| extend Role = coalesce(tostring(Properties["{_SERVICE_NAME_PROPERTY}"]), AppRoleName),\n'
        f"    RoleNamespace = {_namespace_expression()}\n"
        '| summarize RequestRows = countif(Type == "AppRequests"),\n'
        '    DependencyRows = countif(Type == "AppDependencies"),\n'
        "    Represented = sum(ItemCount), MaxItemCount = max(ItemCount),\n"
        "    RowsWithNamespace = countif(isnotempty(RoleNamespace)),\n"
        "    Namespace = take_anyif(RoleNamespace, isnotempty(RoleNamespace)),\n"
        "    SdkVersions = make_set(SDKVersion, 8), Components = make_set(_ResourceId, 8),\n"
        "    LastSeen = max(TimeGenerated)\n"
        "    by Role\n"
        f"| top {ROLE_PROFILE_LIMIT} by Represented desc\n"
    )


def _string_list(value: object) -> list[str]:
    """Read a ``make_set`` column (a list, or its JSON text)."""
    if isinstance(value, str):
        value = json.loads(value) if value.strip() else []
    if not isinstance(value, list):
        raise ValueError(f"expected a list, got {value!r}")
    return [str(item) for item in value if str(item).strip()]


def parse_role_profile_response(
    response: LogsQueryResult | LogsQueryPartialResult,
) -> list[TelemetryRoleProfile]:
    """Read role profiles from a ``build_role_profile_query`` response.

    Raises:
        ValueError: if the response isn't complete or lacks the columns.
    """
    if getattr(response, "status", None) != LogsQueryStatus.SUCCESS:
        raise ValueError(f"role profile query status {getattr(response, 'status', None)!r}")
    profiles: list[TelemetryRoleProfile] = []
    for table in list(getattr(response, "tables", None) or []):
        columns = {column: index for index, column in enumerate(table.columns)}
        missing = _PROFILE_COLUMNS.difference(columns)
        if missing:
            raise ValueError(f"table {table.name!r} lacks columns {sorted(missing)}")
        for row in table.rows:
            versions = _string_list(row[columns["SdkVersions"]])
            last_seen = row[columns["LastSeen"]]
            profiles.append(
                TelemetryRoleProfile(
                    role=_text(row, columns["Role"]),
                    namespace=_text(row, columns["Namespace"]),
                    request_rows=_count(row, columns["RequestRows"]),
                    dependency_rows=_count(row, columns["DependencyRows"]),
                    represented_calls=_count(row, columns["Represented"]),
                    max_item_count=_count(row, columns["MaxItemCount"]),
                    rows_with_namespace=_count(row, columns["RowsWithNamespace"]),
                    stacks=sorted({telemetry_stack(v) for v in versions}, key=lambda s: s.value),
                    sdk_versions=sorted(versions),
                    components=sorted(
                        {c.rsplit("/", 1)[-1] for c in _string_list(row[columns["Components"]])}
                    ),
                    last_seen=_parse_observed_at(last_seen) if last_seen else None,
                )
            )
    return profiles


def source_reference(name: str, namespace: str | None) -> str:
    """Return how an edge names its telemetry source.

    ``<namespace>/<name>`` when both are DNS labels, so the source resolves
    in exactly that namespace; otherwise the bare name, resolved like any
    short service name.
    """
    name = name.strip()
    namespace = (namespace or "").strip().lower()
    if namespace and _DNS_LABEL.match(namespace) and _DNS_LABEL.match(name.lower()):
        return f"{namespace}/{name.lower()}"
    return name


def _runtime_edge(
    source: str,
    target: str,
    observed_at: datetime,
    dependency_type: str | None = None,
    resolution: EdgeResolution = EdgeResolution.TARGET,
    target_details: list[TargetDetail] | None = None,
) -> DependencyEdge:
    """Build an edge from live Azure Monitor telemetry."""
    return DependencyEdge(
        dependency_type=dependency_type,
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
        confidence_score=1.0,
        last_observed_at=observed_at,
        provenance=AZURE_MONITOR_PROVENANCE,
        resolution=[resolution],
        target_details=target_details,
    )


def _callee_details(target: str, dependency_type: str | None) -> list[TargetDetail]:
    """What a joined callee edge keeps of the address: the address and what was behind it."""
    parsed = parse_target(target, dependency_type)
    if isinstance(parsed, DropReason):
        return [TargetDetail(kind=TargetDetailKind.ADDRESS, name=target.strip())]
    return [*parsed.details, TargetDetail(kind=TargetDetailKind.ADDRESS, name=parsed.host)]


def _parse_observed_at(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"unsupported LastObservedAt value: {value!r}")


def _count(row: Any, index: int | None) -> int:
    """Read an optional non-negative count column (0 when absent or null)."""
    if index is None or row[index] is None:
        return 0
    value = int(float(str(row[index])))
    if value < 0:
        raise ValueError(f"negative count {value}")
    return value


def _text(row: Any, index: int | None) -> str:
    """Read an optional text column ('' when absent or null)."""
    if index is None or row[index] is None:
        return ""
    return str(row[index]).strip()


def _add_metrics(metrics: RuntimeMetrics, calls: dict[str, tuple[int, int]]) -> None:
    """Turn per-source (calls, failures) totals into request_count/error_rate."""
    for source, (total, failures) in calls.items():
        if total:
            metrics[source] = {"request_count": total, "error_rate": failures / total}


def _parse_dependency_table(
    table: LogsTable,
    calls: dict[str, tuple[int, int]] | None = None,
    noise: Counter[str] | None = None,
    sampling: dict[str, int] | None = None,
) -> tuple[list[DependencyEdge], list[str]]:
    """Parse one result table into edges and per-row errors.

    ``LogsTable.columns`` is a list of column-name strings in
    azure-monitor-query; row values are read by the matching index. The
    ``SourceNamespace`` column is optional; when present it qualifies the
    source (see ``source_reference``); the optional ``DependencyType``
    column is carried on the edge. The optional ``Calls`` and
    ``Failures`` columns are summed per source into ``calls``, and the
    largest optional ``MaxItemCount`` above 1 per source is kept in
    ``sampling``.

    Each (source, target, type) yields an edge to the target address
    (``EdgeResolution.TARGET``); a row naming a ``Callee`` also yields an
    edge to that callee (``REQUEST_JOIN``, namespaced when
    ``CalleeNamespace`` is known) carrying the address it was reached at.
    A target joined to several callees comes as several rows: its calls
    count once. Rows marked ``Noise`` yield no edge and are counted in
    ``noise`` per reason.

    Raises:
        ValueError: if the table does not have the expected columns.
    """
    columns = list(table.columns)
    if not all(isinstance(column, str) for column in columns):
        raise ValueError(
            f"table {table.name!r} columns are not column-name strings: {columns!r}"
        )
    column_indexes = {column: index for index, column in enumerate(columns)}
    missing = _REQUIRED_COLUMNS.difference(column_indexes)
    if missing:
        raise ValueError(
            f"table {table.name!r} is missing columns: {', '.join(sorted(missing))}"
        )

    index_of = column_indexes.get
    edges: list[DependencyEdge] = []
    counted: set[tuple[str, str, str]] = set()
    bad_rows = 0
    for row in table.rows:
        try:
            source = row[column_indexes["Source"]]
            target = row[column_indexes["Target"]]
            observed_at = _parse_observed_at(row[column_indexes["LastObservedAt"]])
            if not source or not target:
                raise ValueError("empty Source or Target")
            reference = source_reference(str(source), _text(row, index_of(_NAMESPACE_COLUMN)))
            dependency_type = _text(row, index_of(_TYPE_COLUMN)) or None
            key = (reference, str(target), dependency_type or "")
            first_sighting = key not in counted
            counted.add(key)
            if reason := _text(row, index_of(_NOISE_COLUMN)):
                if noise is not None and first_sighting:
                    noise[reason] += 1
                continue
            row_calls = _count(row, index_of(_CALLS_COLUMN))
            row_failures = _count(row, index_of(_FAILURES_COLUMN))
            item_count = _count(row, index_of(_MAX_ITEM_COUNT_COLUMN))
            if sampling is not None and item_count > 1:
                sampling[reference] = max(sampling.get(reference, 0), item_count)
            if first_sighting:
                edges.append(_runtime_edge(reference, str(target), observed_at, dependency_type))
                if calls is not None:
                    total, failed = calls.get(reference, (0, 0))
                    calls[reference] = (total + row_calls, failed + row_failures)
            if callee := _text(row, index_of(_CALLEE_COLUMN)):
                callee_namespace = _text(row, index_of(_CALLEE_NAMESPACE_COLUMN))
                edges.append(
                    _runtime_edge(
                        reference,
                        source_reference(callee, callee_namespace),
                        observed_at,
                        dependency_type,
                        EdgeResolution.REQUEST_JOIN,
                        _callee_details(str(target), dependency_type),
                    )
                )
        except (IndexError, ValueError, ValidationError) as error:
            logger.warning("Skipping malformed telemetry row: %s", error)
            bad_rows += 1
    errors = (
        [f"table {table.name!r}: skipped {bad_rows} malformed row(s)"]
        if bad_rows
        else []
    )
    return edges, errors


def parse_dependency_response(
    response: LogsQueryResult | LogsQueryPartialResult,
    metrics: RuntimeMetrics | None = None,
    noise: Counter[str] | None = None,
    sampling: dict[str, int] | None = None,
) -> DiscoveryResult[DependencyEdge]:
    """Turn a ``query_workspace`` response into an explicit discovery result.

    A ``LogsQueryResult`` exposes ``tables``; a ``LogsQueryPartialResult``
    exposes ``partial_data`` and ``partial_error``. Partial responses keep
    whatever edges were returned but are marked PARTIAL, never SUCCESS.
    When ``metrics`` is given, per-source call metrics are written into it,
    when ``noise`` is given, rows marked as noise are counted in it, and
    when ``sampling`` is given, sampled sources are recorded in it.
    """
    status = getattr(response, "status", None)
    errors: list[str] = []
    if status == LogsQueryStatus.SUCCESS:
        tables = list(getattr(response, "tables", None) or [])
    elif status == LogsQueryStatus.PARTIAL:
        tables = list(getattr(response, "partial_data", None) or [])
        partial_error = getattr(response, "partial_error", None)
        message = getattr(partial_error, "message", None) or str(partial_error)
        logger.error("Telemetry query returned partial results: %s", message)
        errors.append(f"Telemetry query returned partial results: {message}")
    else:
        logger.error("Unexpected telemetry query response: %r", response)
        return DiscoveryResult[DependencyEdge].failed(
            f"Unexpected telemetry query response status: {status!r}"
        )

    if not tables:
        return DiscoveryResult[DependencyEdge].failed(
            "Telemetry query returned no result tables"
            + (f" ({errors[0]})" if errors else "")
        )

    edges: list[DependencyEdge] = []
    calls: dict[str, tuple[int, int]] = {}
    parsed_tables = 0
    for table in tables:
        try:
            table_edges, table_errors = _parse_dependency_table(table, calls, noise, sampling)
        except ValueError as error:
            logger.error("Unusable telemetry result table: %s", error)
            errors.append(str(error))
            continue
        parsed_tables += 1
        edges.extend(table_edges)
        errors.extend(table_errors)

    if parsed_tables == 0:
        return DiscoveryResult[DependencyEdge].failed("; ".join(errors))
    if metrics is not None:
        _add_metrics(metrics, calls)
    return DiscoveryResult[DependencyEdge].from_items(edges, errors)
