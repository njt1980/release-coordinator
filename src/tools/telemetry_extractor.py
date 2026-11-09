"""Read-only client for extracting service dependency telemetry."""

import logging
import os
from datetime import datetime, timedelta

from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient

from src.core.models import DependencyEdge, DiscoveryMethod


logger = logging.getLogger(__name__)


class TelemetryExtractor:
    """Read-only client boundary for telemetry-derived dependencies."""

    def __init__(self) -> None:
        self._credential = DefaultAzureCredential()
        self._client = LogsQueryClient(self._credential)
        self._resource_id = os.getenv("APPLICATIONINSIGHTS_RESOURCE_ID")
        self._workspace_id = (
            os.getenv("LOG_ANALYTICS_WORKSPACE_ID")
            or os.getenv("APPLICATIONINSIGHTS_WORKSPACE_ID")
        )

    def get_active_dependencies(
        self, days_back: int = 30
    ) -> list[DependencyEdge]:
        """Return active dependency edges observed within a lookback period.

        This method is intended to query telemetry for observed service
        interactions only. It must not emit telemetry, change monitoring
        configuration, or otherwise mutate the source system.

        Args:
            days_back: Number of preceding days of telemetry to inspect.

        Returns:
            Dependency edges observed during the requested period.
        """
        try:
            resource_id = self._resource_id
            workspace_id = self._workspace_id
            if days_back <= 0:
                raise ValueError("days_back must be a positive integer")
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
