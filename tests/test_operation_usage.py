"""Tests for HTTP consumer attribution: matching observed request names to
broken OpenAPI operations, the telemetry query, and the briefing lines."""

import unittest
from unittest.mock import MagicMock

from azure.monitor.query import LogsQueryResult, LogsTable

from src.coordinator.contracts import attribute_contract_operations, contract_links, evaluate_contracts
from src.core.contract_findings import ContractChangeKind, ContractFinding
from src.core.models import (
    ContractLink,
    ContractRole,
    ContractScope,
    RepositoryCoverage,
    ServiceCoverage,
)
from src.core.operation_usage import (
    ObservedOperation,
    attribute_operations,
    broken_operations,
    operation_matches,
    split_operation,
)
from src.orchestration.briefing import _attribution_lines
from src.tools.telemetry_extractor import (
    build_operation_usage_query,
    parse_operation_usage_response,
)
from tests.test_contract_compat import LINK_GRAPH, _id

DOC = "src/Basket.API/openapi.yaml"


def _finding(symbol: str, change: ContractChangeKind = ContractChangeKind.OPERATION_REMOVED) -> ContractFinding:
    return ContractFinding(path=DOC, symbol=symbol, change=change, detail=f"{symbol} changed.")


def _observed(caller: str, operation: str, callee: str = "basket") -> ObservedOperation:
    return ObservedOperation(caller=caller, callee=callee, operation=operation, calls=3)


class MatchingTests(unittest.TestCase):
    def test_split_operation(self) -> None:
        self.assertEqual(split_operation("GET /api/Orders/{id}?x=1"), ("GET", ["api", "orders", "{id}"]))
        self.assertEqual(split_operation("/orders"), (None, ["orders"]))
        self.assertEqual(split_operation("POST https://h:80/a/b"), ("POST", ["a", "b"]))
        self.assertIsNone(split_operation("ServiceBusProcessor.ProcessMessage"))

    def test_templates_and_concrete_paths_match(self) -> None:
        self.assertTrue(operation_matches("GET /orders/{}", "GET /orders/{orderId}"))
        self.assertTrue(operation_matches("GET /orders/{}", "GET /Orders/42"))
        self.assertTrue(operation_matches("GET /orders/{}", "/orders/42"))
        self.assertFalse(operation_matches("GET /orders/{}", "POST /orders/42"))
        self.assertFalse(operation_matches("GET /orders/{}", "GET /orders"))
        self.assertFalse(operation_matches("GET /orders/{}", "GET /items/42"))

    def test_broken_operations_only_breaking_with_an_operation(self) -> None:
        findings = [
            _finding("GET /orders/{}"),
            _finding("POST /orders request.note", ContractChangeKind.REQUEST_FIELD_REQUIRED_ADDED),
            _finding("PUT /orders/{}", ContractChangeKind.OPERATION_ADDED),
            ContractFinding(path="other/openapi.yaml", symbol="GET /x", change=ContractChangeKind.OPERATION_REMOVED, detail="x"),
            ContractFinding(path=DOC, change=ContractChangeKind.FILE_REMOVED, detail="gone"),
        ]
        self.assertEqual(broken_operations(findings, [DOC]), {DOC: ["GET /orders/{}", "POST /orders"]})

    def test_attribution_keeps_every_caller(self) -> None:
        roles = {"webapp": "w", "ordering": "o", "admin": "a"}
        result = attribute_operations(
            ["GET /orders/{}", "DELETE /orders/{}"],
            ["w", "o", "a"],
            [
                _observed("shop/webapp", "GET /orders/{id}"),
                _observed("ordering", "GET /health"),
                _observed("stranger", "DELETE /orders/7"),
            ],
            roles,
            7,
        )
        self.assertEqual(result.operation_callers, {"GET /orders/{}": ["w"], "DELETE /orders/{}": []})
        self.assertEqual(result.other_operations_only, ["o"])
        self.assertEqual(result.unattributed, ["a"])
        self.assertEqual(result.unmatched_roles, ["stranger"])


class QueryTests(unittest.TestCase):
    def test_query_restricts_callees_and_rejects_unsafe_names(self) -> None:
        query = build_operation_usage_query(7, ["Basket", 'x") | drop', "ns/bad name"])
        self.assertIn('in~ ("basket")', query)
        self.assertNotIn("drop", query)
        self.assertIn("$left.Id == $right.ParentId", query)
        self.assertIn("sum(ItemCount)", query)
        with self.assertRaises(ValueError):
            build_operation_usage_query(7, ['"; drop'])
        with self.assertRaises(ValueError):
            build_operation_usage_query(0, ["basket"])

    def test_parse_response(self) -> None:
        table = LogsTable(
            name="PrimaryResult",
            columns=["Caller", "Callee", "Operation", "Calls"],
            columns_types=["string", "string", "string", "long"],
            rows=[["webapp", "basket", "GET /orders/{id}", 5], ["", "basket", "GET /x", 1]],
        )
        response = LogsQueryResult(tables=[table])
        self.assertEqual(
            parse_operation_usage_response(response),
            [ObservedOperation(caller="webapp", callee="basket", operation="GET /orders/{id}", calls=5)],
        )
        broken = MagicMock(status="nope")
        with self.assertRaises(ValueError):
            parse_operation_usage_response(broken)


class ContractAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = LINK_GRAPH.model_copy(update={"static_coverage": [RepositoryCoverage(
            repository="Shop", commit_sha="a" * 40,
            services=[
                ServiceCoverage(service_name="basket", root="src/Basket.API",
                                contracts=[ContractLink(path=DOC, role=ContractRole.SERVER)]),
                ServiceCoverage(service_name="webapp", root="src/WebApp",
                                contracts=[ContractLink(path=DOC, role=ContractRole.CLIENT)]),
                ServiceCoverage(service_name="ordering", root="src/Ordering"),
            ],
        )]})
        self.links = contract_links(self.graph)
        self.check = evaluate_contracts(
            ContractScope.STRICT, [DOC], [_finding("GET /orders/{}")], {},
            {DOC: [_id("webapp")]}, [], callers={DOC: [_id("ordering"), _id("webapp")]},
        )

    def test_attributes_callers_seen_on_the_server(self) -> None:
        asked: list[list[str]] = []

        def read(callees: list[str]) -> list[ObservedOperation]:
            asked.append(callees)
            return [
                _observed("webapp", "GET /orders/{id}"),
                _observed("ordering", "GET /orders/{id}", callee="elsewhere"),  # another callee: ignored
            ]

        result = attribute_contract_operations(self.check, self.links, self.graph, "Shop", read, 7)
        self.assertEqual(asked, [["basket"]])
        assert result is not None
        self.assertEqual(result.operation_callers, {"GET /orders/{}": [_id("webapp")]})
        self.assertEqual(result.unattributed, [_id("ordering")])
        lines = _attribution_lines(self.check.model_copy(update={"operation_attribution": result}))
        self.assertIn("GET /orders/{} was used by webapp in the last 7 day(s) (live telemetry)", lines)
        self.assertIn("No requests seen, so the operations they use are unknown: ordering", lines)

    def test_nothing_when_telemetry_cannot_answer_or_not_breaking(self) -> None:
        self.assertIsNone(attribute_contract_operations(self.check, self.links, self.graph, "Shop", lambda _: None, 7))
        compatible = evaluate_contracts(ContractScope.STRICT, [DOC], [], {}, {}, [])
        self.assertIsNone(
            attribute_contract_operations(compatible, self.links, self.graph, "Shop", lambda _: [], 7)
        )
        self.assertEqual(_attribution_lines(self.check), [])


if __name__ == "__main__":
    unittest.main()
