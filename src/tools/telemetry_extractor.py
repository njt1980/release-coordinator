"""Read-only client for extracting service dependency telemetry."""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeAlias

from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient
from pydantic import BaseModel, Field, ValidationError

from src.core.models import DependencyEdge, DiscoveryMethod


logger = logging.getLogger(__name__)
RuntimeMetrics: TypeAlias = dict[str, dict[str, int | float]]


class _MockDependencyRecord(BaseModel):
    """Validated shape of one AppDependencies query result row."""

    source: str = Field(alias="AppRoleName", min_length=1)
    target: str = Field(alias="Target", min_length=1)
    time_generated: datetime = Field(alias="TimeGenerated")
    success: bool = Field(alias="Success")


class TelemetryExtractor:
    """Read-only client boundary for telemetry-derived dependencies."""

    def __init__(self) -> None:
        self._source = os.getenv("TELEMETRY_SOURCE", "mock").lower()
        if self._source not in {"mock", "azure"}:
            raise ValueError("TELEMETRY_SOURCE must be either 'mock' or 'azure'")

        self._runtime_metrics: RuntimeMetrics = {}
        self._credential: DefaultAzureCredential | None = None
        self._client: LogsQueryClient | None = None
        if self._source == "azure":
            self._credential = DefaultAzureCredential()
            self._client = LogsQueryClient(self._credential)
        self._resource_id = os.getenv("APPLICATIONINSIGHTS_RESOURCE_ID")
        self._workspace_id = (
            os.getenv("LOG_ANALYTICS_WORKSPACE_ID")
            or os.getenv("APPLICATIONINSIGHTS_WORKSPACE_ID")
        )

    def verify_access(self) -> tuple[bool, str]:
        """Confirm query access with a trivial KQL statement that scans no data.

        In mock mode no Azure access is required, so the check passes.

        Returns:
            (True, "Success") when the query succeeds, otherwise
            (False, <error message>).
        """
        if self._source == "mock":
            return True, "Success"
        try:
            if self._client is None:
                raise RuntimeError("Azure telemetry client was not initialized")
            query = "print ok=1"
            timespan = timedelta(minutes=5)
            if self._resource_id:
                response = self._client.query_resource(
                    self._resource_id, query, timespan=timespan
                )
            elif self._workspace_id:
                response = self._client.query_workspace(
                    self._workspace_id, query, timespan=timespan
                )
            else:
                raise ValueError(
                    "Set APPLICATIONINSIGHTS_RESOURCE_ID or "
                    "LOG_ANALYTICS_WORKSPACE_ID to query telemetry"
                )
            if not hasattr(response, "tables"):
                raise RuntimeError(str(getattr(response, "error", response)))
            return True, "Success"
        except Exception as error:
            logger.warning("Telemetry access verification failed: %s", error)
            return False, str(error)

    def get_active_dependencies(
        self, days_back: int = 30
    ) -> list[DependencyEdge]:
        """Return active dependency edges observed within a lookback period.

        The local source reads AppDependencies-shaped records from the root
        mock_telemetry.json fixture. Set TELEMETRY_SOURCE=azure to use Azure.
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

    def _get_active_dependencies_from_mock(self) -> list[DependencyEdge]:
        mock_path = Path(__file__).resolve().parents[2] / "mock_telemetry.json"
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

            records: list[_MockDependencyRecord] = []
            for raw_record in raw_records:
                try:
                    record = _MockDependencyRecord.parse_obj(raw_record)
                except (ValidationError, TypeError) as error:
                    logger.warning(
                        "Skipping invalid mock dependency record: %s", error
                    )
                    continue
                if not record.source.strip() or not record.target.strip():
                    logger.warning(
                        "Skipping mock dependency with an empty service name"
                    )
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
                    confidence_score=1.0,
                    last_observed_at=observed_at,
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
            return edges
        except (OSError, json.JSONDecodeError, ValueError):
            self._runtime_metrics = {}
            logger.exception(
                "Failed to load mock application dependencies from %s", mock_path
            )
            return []

    def _get_active_dependencies_from_azure(
        self, days_back: int
    ) -> list[DependencyEdge]:
        """Query Azure Monitor for observed dependency edges."""
        try:
            resource_id = self._resource_id
            workspace_id = self._workspace_id
            if not resource_id and not workspace_id:
                raise ValueError(
                    "Set APPLICATIONINSIGHTS_RESOURCE_ID or "
                    "LOG_ANALYTICS_WORKSPACE_ID to query telemetry"
                )

            query = f"""
                AppDependencies
                | where TimeGenerated >= ago({days_back}d)
                | where isnotempty(AppRoleName) and isnotempty(Target)
                | summarize LastObservedAt=max(TimeGenerated)
                    by Source=AppRoleName, Target
                | project Source, Target, LastObservedAt
            """
            if self._client is None:
                raise RuntimeError("Azure telemetry client was not initialized")
            if resource_id:
                response = self._client.query_resource(
                    resource_id,
                    query,
                    timespan=timedelta(days=days_back),
                )
            elif workspace_id:
                response = self._client.query_workspace(
                    workspace_id,
                    query,
                    timespan=timedelta(days=days_back),
                )
            else:
                raise ValueError(
                    "Set APPLICATIONINSIGHTS_RESOURCE_ID or "
                    "LOG_ANALYTICS_WORKSPACE_ID to query telemetry"
                )

            if not hasattr(response, "tables"):
                logger.error(
                    "Telemetry query failed: %s",
                    getattr(response, "error", response),
                )
                return []
            partial_error = getattr(response, "partial_error", None)
            if partial_error is not None:
                logger.error(
                    "Telemetry query returned partial results: %s", partial_error
                )

            edges: list[DependencyEdge] = []
            for table in response.tables:
                column_indexes = {
                    column.name: index for index, column in enumerate(table.columns)
                }
                required_columns = {"Source", "Target", "LastObservedAt"}
                if not required_columns.issubset(column_indexes):
                    logger.error(
                        "Telemetry query result is missing expected columns: %s",
                        required_columns.difference(column_indexes),
                    )
                    continue

                for row in table.rows:
                    source = row[column_indexes["Source"]]
                    target = row[column_indexes["Target"]]
                    observed_at = row[column_indexes["LastObservedAt"]]
                    if not source or not target or observed_at is None:
                        continue
                    if isinstance(observed_at, str):
                        observed_at = datetime.fromisoformat(
                            observed_at.replace("Z", "+00:00")
                        )
                    edges.append(
                        DependencyEdge(
                            source_service_id=str(source),
                            target_service_id=str(target),
                            discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
                            confidence_score=1.0,
                            last_observed_at=observed_at,
                        )
                    )
            return edges
        except Exception:
            logger.exception("Failed to query active application dependencies")
            return []
