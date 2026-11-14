"""Tests for telemetry parsing against the real azure-monitor-query result shapes.

The fixtures are built from the SDK's own ``LogsTable``, ``LogsQueryResult``
and ``LogsQueryPartialResult`` classes so the tests follow its contract:
``LogsTable.columns`` is a list of column-name strings, and a partial
response exposes ``partial_data``/``partial_error`` instead of ``tables``.
"""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from azure.monitor.query import (
    LogsQueryError,
    LogsQueryPartialResult,
    LogsQueryResult,
    LogsTable,
)

from src.core.models import DiscoveryStatus
from src.tools.telemetry_extractor import (
    TelemetryExtractor,
    parse_dependency_response,
)

COLUMNS = ["Source", "Target", "LastObservedAt"]
COLUMN_TYPES = ["string", "string", "datetime"]


def _table(rows: list[list[object]], columns: list[str] = COLUMNS) -> LogsTable:
    return LogsTable(
        name="PrimaryResult",
        columns=columns,
        columns_types=COLUMN_TYPES[: len(columns)],
        rows=rows,
    )


def _azure_extractor(response: object) -> TelemetryExtractor:
    extractor = TelemetryExtractor.__new__(TelemetryExtractor)
    extractor._additional_workspaces = []
    extractor._workspace_id = "ws-1"
    extractor._source = "azure"
    extractor._runtime_metrics = {}
    extractor._credential = None
    client = MagicMock()
    if isinstance(response, Exception):
        client.query_workspace.side_effect = response
    else:
        client.query_workspace.return_value = response
    extractor._client = client
    return extractor


class SdkShapeTests(unittest.TestCase):
    def test_sdk_columns_are_plain_strings(self) -> None:
        """Guards the assumption the parser relies on."""
        self.assertEqual(_table([]).columns, COLUMNS)


class ParseDependencyResponseTests(unittest.TestCase):
    def test_success_parses_rows_by_column_name_strings(self) -> None:
        response = LogsQueryResult(
            tables=[
                _table(
                    [
                        ["store-front", "order-service", "2026-01-02T03:04:05Z"],
                        ["order-service", "rabbitmq", "2026-01-03T00:00:00Z"],
                    ]
                )
            ]
        )

        result = parse_dependency_response(response)

        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        self.assertEqual(result.errors, [])
        self.assertEqual(
            [(e.source_service_id, e.target_service_id) for e in result.items],
            [("store-front", "order-service"), ("order-service", "rabbitmq")],
        )
        self.assertEqual(
            result.items[0].last_observed_at,
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )

    def test_column_order_follows_the_table_not_a_fixed_layout(self) -> None:
        table = LogsTable(
            name="PrimaryResult",
            columns=["LastObservedAt", "Target", "Source"],
            columns_types=["datetime", "string", "string"],
            rows=[["2026-01-01T00:00:00Z", "order-service", "store-front"]],
        )

        result = parse_dependency_response(LogsQueryResult(tables=[table]))

        edge = result.items[0]
        self.assertEqual(
            (edge.source_service_id, edge.target_service_id),
            ("store-front", "order-service"),
        )

    def test_success_with_no_rows_is_a_genuine_empty_success(self) -> None:
        result = parse_dependency_response(LogsQueryResult(tables=[_table([])]))
        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        self.assertEqual(result.items, [])

    def test_partial_response_keeps_partial_data_but_is_not_success(self) -> None:
        response = LogsQueryPartialResult(
            partial_data=[
                _table([["store-front", "order-service", "2026-01-01T00:00:00Z"]])
            ],
            partial_error=LogsQueryError(
                code="PartialError", message="Query exceeded the row limit"
            ),
        )

        result = parse_dependency_response(response)

        self.assertEqual(result.status, DiscoveryStatus.PARTIAL)
        self.assertEqual(len(result.items), 1)
        self.assertIn("Query exceeded the row limit", result.errors[0])

    def test_partial_response_without_data_is_failed(self) -> None:
        response = LogsQueryPartialResult(
            partial_data=[],
            partial_error=LogsQueryError(code="PartialError", message="timeout"),
        )
        result = parse_dependency_response(response)
        self.assertEqual(result.status, DiscoveryStatus.FAILED)
        self.assertEqual(result.items, [])
        self.assertIn("timeout", result.errors[0])

    def test_missing_columns_is_failed_not_empty(self) -> None:
        response = LogsQueryResult(
            tables=[_table([["store-front", "order-service"]], COLUMNS[:2])]
        )
        result = parse_dependency_response(response)
        self.assertEqual(result.status, DiscoveryStatus.FAILED)
        self.assertIn("LastObservedAt", result.errors[0])

    def test_column_objects_are_rejected_rather_than_misread(self) -> None:
        table = SimpleNamespace(
            name="PrimaryResult",
            columns=[SimpleNamespace(name=c) for c in COLUMNS],
            rows=[],
        )
        response = SimpleNamespace(status=LogsQueryResult().status, tables=[table])
        result = parse_dependency_response(response)  # type: ignore[arg-type]
        self.assertEqual(result.status, DiscoveryStatus.FAILED)
        self.assertIn("not column-name strings", result.errors[0])

    def test_malformed_rows_are_skipped_and_mark_result_partial(self) -> None:
        table = LogsTable(
            name="PrimaryResult",
            columns=COLUMNS,
            columns_types=["string", "string", "string"],
            rows=[
                ["store-front", "order-service", "2026-01-01T00:00:00Z"],
                ["store-front", "", "2026-01-01T00:00:00Z"],
                ["order-service", "rabbitmq", "not-a-date"],
            ],
        )

        result = parse_dependency_response(LogsQueryResult(tables=[table]))

        self.assertEqual(result.status, DiscoveryStatus.PARTIAL)
        self.assertEqual(len(result.items), 1)
        self.assertIn("skipped 2 malformed row(s)", result.errors[0])

    def test_no_tables_is_failed(self) -> None:
        result = parse_dependency_response(LogsQueryResult(tables=[]))
        self.assertEqual(result.status, DiscoveryStatus.FAILED)

    def test_unknown_response_status_is_failed(self) -> None:
        result = parse_dependency_response(SimpleNamespace(tables=[]))  # type: ignore[arg-type]
        self.assertEqual(result.status, DiscoveryStatus.FAILED)


class AzureExtractorTests(unittest.TestCase):
    def test_get_active_dependencies_uses_sdk_response(self) -> None:
        response = LogsQueryResult(
            tables=[_table([["a", "b", "2026-01-01T00:00:00Z"]])]
        )
        extractor = _azure_extractor(response)

        result = extractor.get_active_dependencies(days_back=7)

        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        self.assertEqual(len(result.items), 1)
        extractor._client.query_workspace.assert_called_once()  # type: ignore[union-attr]

    def test_query_exception_is_failed_not_empty_list(self) -> None:
        extractor = _azure_extractor(RuntimeError("403 Forbidden"))

        with self.assertLogs("src.tools.telemetry_extractor", level="ERROR"):
            result = extractor.get_active_dependencies(days_back=7)

        self.assertEqual(result.status, DiscoveryStatus.FAILED)
        self.assertEqual(result.items, [])
        self.assertIn("403 Forbidden", result.errors[0])


class MockSourceTests(unittest.TestCase):
    def test_mock_fixture_loads_as_success(self) -> None:
        extractor = TelemetryExtractor.__new__(TelemetryExtractor)
        extractor._additional_workspaces = []
        extractor._source = "mock"
        extractor._runtime_metrics = {}

        result = extractor.get_active_dependencies(days_back=7)

        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        self.assertTrue(result.items)
        for edge in result.items:
            self.assertEqual(edge.provenance, "mock_fixture")
            self.assertEqual(edge.environment, "mock")
            self.assertEqual(edge.confidence_score, 0.0)
            self.assertTrue(edge.is_mock_fixture)


if __name__ == "__main__":
    unittest.main()
