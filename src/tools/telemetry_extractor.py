"""Read-only client for extracting service dependency telemetry."""

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
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

from src.core.models import (
    MOCK_FIXTURE_PROVENANCE,
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryResult,
    TelemetryClusterFilter,
)
from src.tools.telemetry_normalization import normalize_edges
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
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# OpenTelemetry resource attributes, as Application Insights stores them in
# Properties: the workload's own name, and the Kubernetes namespace it runs
# in (service.namespace is a logical grouping in OpenTelemetry, not
# necessarily the Kubernetes namespace, so it is not used).
_SERVICE_NAME_PROPERTY = "service.name"
_NAMESPACE_PROPERTY = "k8s.namespace.name"
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

    def __init__(self, workspace_id: str) -> None:
        """Bind the extractor to an explicit Log Analytics workspace.

        Args:
            workspace_id: Log Analytics workspace ID that every query targets.

        Raises:
            ValueError: If workspace_id is blank or TELEMETRY_SOURCE is invalid.
        """
        if not workspace_id or not workspace_id.strip():
            raise ValueError("workspace_id is required")
        self._workspace_id = workspace_id.strip()

        self._source = os.getenv("TELEMETRY_SOURCE", "mock").lower()
        if self._source not in {"mock", "azure"}:
            raise ValueError("TELEMETRY_SOURCE must be either 'mock' or 'azure'")

        self._runtime_metrics: RuntimeMetrics = {}
        self._dropped_records: dict[str, int] = {}
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
        cluster_filter: TelemetryClusterFilter | None = None,
    ) -> DiscoveryResult[DependencyEdge]:
        """Return active dependency edges observed within a lookback period.

        With ``cluster_filter``, only rows emitted from that cluster are
        read (see ``build_dependency_query``); the mock fixture has no
        cluster attributes and ignores it. A source whose Kubernetes
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
            result = self._get_active_dependencies_from_azure(days_back, cluster_filter)
        edges, self._dropped_records = normalize_edges(result.items)
        return result.model_copy(update={"items": edges})

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
        self, days_back: int, cluster_filter: TelemetryClusterFilter | None
    ) -> DiscoveryResult[DependencyEdge]:
        """Query Azure Monitor for observed dependency edges."""
        query = build_dependency_query(days_back, cluster_filter)
        self._runtime_metrics = {}
        try:
            if self._client is None:
                raise RuntimeError("Azure telemetry client was not initialized")
            response = self._client.query_workspace(
                self._workspace_id,
                query,
                timespan=timedelta(days=days_back),
            )
        except Exception as error:
            logger.exception("Failed to query active application dependencies")
            return DiscoveryResult[DependencyEdge].failed(
                f"Telemetry query failed: {error}"
            )
        return parse_dependency_response(response, self._runtime_metrics)


def build_dependency_query(
    days_back: int, cluster_filter: TelemetryClusterFilter | None = None
) -> str:
    """Return the KQL that summarizes AppDependencies into edges.

    The source is the row's ``service.name`` resource attribute, falling
    back to ``AppRoleName`` (which some exporters prefix with
    ``service.namespace``), with its Kubernetes namespace alongside, and
    rows are kept apart by ``DependencyType`` so normalization can tell a
    messaging delivery from a call (see ``telemetry_normalization``). A
    cluster filter keeps only rows whose attribute equals the value; its
    attribute and value are pattern-restricted by ``TelemetryClusterFilter``
    so they cannot break out of the quoted strings.
    """
    if days_back <= 0:
        raise ValueError("days_back must be a positive integer")
    cluster_clause = (
        f'| where tostring(Properties["{cluster_filter.attribute}"]) == '
        f'"{cluster_filter.value}"\n'
        if cluster_filter
        else ""
    )
    return (
        "AppDependencies\n"
        f"| where TimeGenerated >= ago({days_back}d)\n"
        f"{cluster_clause}"
        f'| extend SourceName = coalesce(tostring(Properties["{_SERVICE_NAME_PROPERTY}"]), AppRoleName),\n'
        f'    {_NAMESPACE_COLUMN} = tostring(Properties["{_NAMESPACE_PROPERTY}"])\n'
        "| where isnotempty(SourceName) and isnotempty(Target)\n"
        f"| summarize LastObservedAt=max(TimeGenerated), {_CALLS_COLUMN}=count(),\n"
        f"    {_FAILURES_COLUMN}=countif(Success == false)\n"
        f"    by Source=SourceName, {_NAMESPACE_COLUMN}, Target, {_TYPE_COLUMN}\n"
        f"| project Source, {_NAMESPACE_COLUMN}, Target, {_TYPE_COLUMN}, "
        f"LastObservedAt, {_CALLS_COLUMN}, {_FAILURES_COLUMN}\n"
    )


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
    source: str, target: str, observed_at: datetime, dependency_type: str | None = None
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
    )


def _parse_observed_at(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"unsupported LastObservedAt value: {value!r}")


def _count(row: Any, index: int | None) -> int:
    """Read an optional non-negative count column (0 when absent)."""
    if index is None or row[index] is None:
        return 0
    value = int(str(row[index]))
    if value < 0:
        raise ValueError(f"negative count {value}")
    return value


def _add_metrics(metrics: RuntimeMetrics, calls: dict[str, tuple[int, int]]) -> None:
    """Turn per-source (calls, failures) totals into request_count/error_rate."""
    for source, (total, failures) in calls.items():
        if total:
            metrics[source] = {"request_count": total, "error_rate": failures / total}


def _parse_dependency_table(
    table: LogsTable,
    calls: dict[str, tuple[int, int]] | None = None,
) -> tuple[list[DependencyEdge], list[str]]:
    """Parse one result table into edges and per-row errors.

    ``LogsTable.columns`` is a list of column-name strings in
    azure-monitor-query; row values are read by the matching index. The
    ``SourceNamespace`` column is optional; when present it qualifies the
    source (see ``source_reference``); the optional ``DependencyType``
    column is carried on the edge. The optional ``Calls`` and
    ``Failures`` columns are summed per source into ``calls``.

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

    namespace_index = column_indexes.get(_NAMESPACE_COLUMN)
    calls_index = column_indexes.get(_CALLS_COLUMN)
    failures_index = column_indexes.get(_FAILURES_COLUMN)
    type_index = column_indexes.get(_TYPE_COLUMN)
    edges: list[DependencyEdge] = []
    bad_rows = 0
    for row in table.rows:
        try:
            source = row[column_indexes["Source"]]
            target = row[column_indexes["Target"]]
            observed_at = _parse_observed_at(row[column_indexes["LastObservedAt"]])
            if not source or not target:
                raise ValueError("empty Source or Target")
            namespace = row[namespace_index] if namespace_index is not None else None
            reference = source_reference(str(source), str(namespace or ""))
            row_calls = _count(row, calls_index)
            row_failures = _count(row, failures_index)
            dependency_type = row[type_index] if type_index is not None else None
            edges.append(
                _runtime_edge(
                    reference, str(target), observed_at, str(dependency_type or "") or None
                )
            )
            if calls is not None:
                total, failed = calls.get(reference, (0, 0))
                calls[reference] = (total + row_calls, failed + row_failures)
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
) -> DiscoveryResult[DependencyEdge]:
    """Turn a ``query_workspace`` response into an explicit discovery result.

    A ``LogsQueryResult`` exposes ``tables``; a ``LogsQueryPartialResult``
    exposes ``partial_data`` and ``partial_error``. Partial responses keep
    whatever edges were returned but are marked PARTIAL, never SUCCESS.
    When ``metrics`` is given, per-source call metrics are written into it.
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
            table_edges, table_errors = _parse_dependency_table(table, calls)
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
