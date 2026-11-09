"""Read-only interface for extracting service dependency telemetry."""

from src.core.models import DependencyEdge


class TelemetryExtractor:
    """Read-only client boundary for telemetry-derived dependencies."""

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

        Raises:
            NotImplementedError: Until the Azure SDK integration is added.
        """
        raise NotImplementedError("Azure SDK integration pending")
