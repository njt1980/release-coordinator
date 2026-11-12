"""Tests for explicit discovery outcomes and how stages aggregate them."""

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from src.core.models import (
    DiscoveryMethod,
    DiscoveryResult,
    DiscoveryStageReport,
    DiscoveryStatus,
    ServiceNode,
    baseline_problems,
    combine_statuses,
)
from src.discovery_engine import nodes as discovery_nodes
from src.core.service_ids import IdentityContext
from src.tools.k8s_extractor import KubernetesExtractor

CONTEXT = IdentityContext(tenant="acme")

S = DiscoveryStatus


class DiscoveryResultTests(unittest.TestCase):
    def test_incomplete_results_must_explain_themselves(self) -> None:
        for status in (S.PARTIAL, S.FAILED):
            with self.subTest(status=status):
                with self.assertRaises(ValidationError):
                    DiscoveryResult[str](status=status)

    def test_failed_and_skipped_results_carry_no_items(self) -> None:
        with self.assertRaises(ValidationError):
            DiscoveryResult[str](status=S.FAILED, items=["x"], errors=["boom"])
        with self.assertRaises(ValidationError):
            DiscoveryResult[str](status=S.SKIPPED, items=["x"])

    def test_from_items_is_partial_only_with_errors(self) -> None:
        self.assertEqual(DiscoveryResult[str].from_items(["a"], []).status, S.SUCCESS)
        self.assertEqual(
            DiscoveryResult[str].from_items(["a"], ["dropped 1"]).status, S.PARTIAL
        )

    def test_combine_statuses(self) -> None:
        cases = [
            ([], S.SKIPPED),
            ([S.SKIPPED], S.SKIPPED),
            ([S.SUCCESS, S.SKIPPED], S.SUCCESS),
            ([S.FAILED, S.FAILED], S.FAILED),
            ([S.SUCCESS, S.FAILED], S.PARTIAL),
            ([S.SUCCESS, S.PARTIAL], S.PARTIAL),
        ]
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                self.assertEqual(combine_statuses(statuses), expected)

    def test_baseline_problems(self) -> None:
        complete = [
            DiscoveryStageReport(source=DiscoveryMethod.STATIC_CODE, status=S.SKIPPED),
            DiscoveryStageReport(source=DiscoveryMethod.K8S_MANIFEST, status=S.SUCCESS),
            DiscoveryStageReport(
                source=DiscoveryMethod.RUNTIME_TELEMETRY, status=S.SKIPPED
            ),
        ]
        self.assertEqual(baseline_problems(complete), [])

        failed = [
            *complete[:2],
            DiscoveryStageReport(
                source=DiscoveryMethod.RUNTIME_TELEMETRY,
                status=S.FAILED,
                errors=["403"],
            ),
        ]
        self.assertEqual(baseline_problems(failed), ["RUNTIME_TELEMETRY FAILED: 403"])


def _k8s_extractor(api: object | None) -> KubernetesExtractor:
    extractor = KubernetesExtractor.__new__(KubernetesExtractor)
    extractor._core_v1_api = api  # type: ignore[assignment]
    return extractor


def _service(name: str | None) -> SimpleNamespace:
    return SimpleNamespace(metadata=SimpleNamespace(name=name))


class KubernetesExtractorResultTests(unittest.TestCase):
    def test_lists_services(self) -> None:
        api = MagicMock()
        api.list_namespaced_service.return_value = SimpleNamespace(
            items=[_service("orders")]
        )
        result = _k8s_extractor(api).get_namespace_services("prod", CONTEXT)
        self.assertEqual(result.status, S.SUCCESS)
        self.assertEqual(
            result.items,
            [
                ServiceNode(
                    service_id="urn:release-coord:acme:unknown:unknown:prod:orders",
                    name="orders",
                )
            ],
        )

    def test_unavailable_client_is_failed(self) -> None:
        result = _k8s_extractor(None).get_namespace_services("prod", CONTEXT)
        self.assertEqual(result.status, S.FAILED)

    def test_list_error_is_failed_not_empty(self) -> None:
        api = MagicMock()
        api.list_namespaced_service.side_effect = RuntimeError("403")
        with self.assertLogs("src.tools.k8s_extractor", level="ERROR"):
            result = _k8s_extractor(api).get_namespace_services("prod", CONTEXT)
        self.assertEqual(result.status, S.FAILED)
        self.assertIn("403", result.errors[0])

    def test_nameless_service_makes_result_partial(self) -> None:
        api = MagicMock()
        api.list_namespaced_service.return_value = SimpleNamespace(
            items=[_service("orders"), _service(None)]
        )
        with self.assertLogs("src.tools.k8s_extractor", level="WARNING"):
            result = _k8s_extractor(api).get_namespace_services("prod", CONTEXT)
        self.assertEqual(result.status, S.PARTIAL)
        self.assertEqual(len(result.items), 1)


class DiscoveryStageAggregationTests(unittest.TestCase):
    def test_one_failed_namespace_makes_k8s_stage_partial(self) -> None:
        state: Any = {"target_namespaces": ["a", "b"], "discovered_services": []}
        with patch.object(discovery_nodes, "KubernetesExtractor") as k8s_cls:
            k8s_cls.return_value.get_namespace_services.side_effect = [
                DiscoveryResult[ServiceNode].success(
                    [ServiceNode(service_id=CONTEXT.identity("x", "a").uri, name="x")]
                ),
                DiscoveryResult[ServiceNode].failed("b: 403"),
            ]
            result = discovery_nodes.extract_k8s_topology(state)

        self.assertEqual(
            result["discovery_reports"],
            [
                DiscoveryStageReport(
                    source=DiscoveryMethod.K8S_MANIFEST,
                    status=S.PARTIAL,
                    item_count=1,
                    errors=["b: 403"],
                )
            ],
        )

    def test_failed_telemetry_stage_contributes_no_edges(self) -> None:
        state: Any = {"workspace_id": "ws-1", "discovery_reports": []}
        with patch.object(discovery_nodes, "TelemetryExtractor") as telemetry_cls:
            telemetry_cls.return_value.get_active_dependencies.return_value = (
                DiscoveryResult.failed("query timed out")
            )
            result = discovery_nodes.extract_runtime_telemetry(state)

        self.assertEqual(result["runtime_edges"], [])
        reports = result["discovery_reports"]
        assert isinstance(reports, list)
        self.assertEqual(reports[0].status, S.FAILED)
        self.assertEqual(reports[0].errors, ["query timed out"])


if __name__ == "__main__":
    unittest.main()
