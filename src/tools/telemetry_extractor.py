"""Read-only client for extracting service dependency telemetry."""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeAlias

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
)
from src.tools._timeout import (
    TIMEOUT_MESSAGE,
    VERIFY_TIMEOUT_SECONDS,
    run_with_timeout,
)


logger = logging.getLogger(__name__)
RuntimeMetrics: TypeAlias = dict[str, dict[str, int | float]]
_REQUIRED_COLUMNS = frozenset({"Source", "Target", "LastObservedAt"})
AZURE_MONITOR_PROVENANCE = "azure_monitor"
MOCK_ENVIRONMENT = "mock"


class _MockDependencyRecord(BaseModel):
    """Validated shape of one AppDependencies query result row."""

    source: str = Field(alias="AppRoleName", min_length=1)
    target: str = Field(alias="Target", min_length=1)
    time_generated: datetime = Field(alias="TimeGenerated")
    success: bool = Field(alias="Success")


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
        self._credential: DefaultAzureCredential | None = None
        self._client: LogsQueryClient | None = None
        if self._source == "azure":
            self._credential = DefaultAzureCredential()
            self._client = LogsQueryClient(self._credential)

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
        self, days_back: int = 30
    ) -> DiscoveryResult[DependencyEdge]:
        """Return active dependency edges observed within a lookback period.

        The local source reads AppDependencies-shaped records from the root
        mock_telemetry.json fixture. Set TELEMETRY_SOURCE=azure to use Azure.
        Mock edges are labeled ``provenance="mock_fixture"`` and
        ``environment="mock"`` with a confidence of 0.0: they are demo data,
        not evidence of real traffic.

        Returns:
            SUCCESS with every observed edge, PARTIAL when some records or
            tables could not be used (or Azure reported a partial error), or
            FAILED with no edges when the source could not be read.
        """
        if days_back <= 0:
            raise ValueError("days_back must be a positive integer")
        if self._source == "mock":
            return self._get_active_dependencies_from_mock()
        return self._get_active_dependencies_from_azure(days_back)

    def get_runtime_metrics(self) -> RuntimeMetrics:
        """Return metrics derived from the most recently loaded mock records."""
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

        latest_observations: dict[tuple[str, str], datetime] = {}
        request_counts: dict[str, int] = defaultdict(int)
        failure_counts: dict[str, int] = defaultdict(int)
        for record in records:
            edge_key = (record.source, record.target)
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
            )
            for (source, target), observed_at in latest_observations.items()
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
        self, days_back: int
    ) -> DiscoveryResult[DependencyEdge]:
        """Query Azure Monitor for observed dependency edges."""
        query = f"""
            AppDependencies
            | where TimeGenerated >= ago({days_back}d)
            | where isnotempty(AppRoleName) and isnotempty(Target)
            | summarize LastObservedAt=max(TimeGenerated)
                by Source=AppRoleName, Target
            | project Source, Target, LastObservedAt
        """
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
        return parse_dependency_response(response)


def _runtime_edge(source: str, target: str, observed_at: datetime) -> DependencyEdge:
    """Build an edge from live Azure Monitor telemetry."""
    return DependencyEdge(
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


def _parse_dependency_table(
    table: LogsTable,
) -> tuple[list[DependencyEdge], list[str]]:
    """Parse one result table into edges and per-row errors.

    ``LogsTable.columns`` is a list of column-name strings in
    azure-monitor-query; row values are read by the matching index.

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

    edges: list[DependencyEdge] = []
    bad_rows = 0
    for row in table.rows:
        try:
            source = row[column_indexes["Source"]]
            target = row[column_indexes["Target"]]
            observed_at = _parse_observed_at(row[column_indexes["LastObservedAt"]])
            if not source or not target:
                raise ValueError("empty Source or Target")
            edges.append(_runtime_edge(str(source), str(target), observed_at))
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
) -> DiscoveryResult[DependencyEdge]:
    """Turn a ``query_workspace`` response into an explicit discovery result.

    A ``LogsQueryResult`` exposes ``tables``; a ``LogsQueryPartialResult``
    exposes ``partial_data`` and ``partial_error``. Partial responses keep
    whatever edges were returned but are marked PARTIAL, never SUCCESS.
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
    parsed_tables = 0
    for table in tables:
        try:
            table_edges, table_errors = _parse_dependency_table(table)
        except ValueError as error:
            logger.error("Unusable telemetry result table: %s", error)
            errors.append(str(error))
            continue
        parsed_tables += 1
        edges.extend(table_edges)
        errors.extend(table_errors)

    if parsed_tables == 0:
        return DiscoveryResult[DependencyEdge].failed("; ".join(errors))
    return DiscoveryResult[DependencyEdge].from_items(edges, errors)
