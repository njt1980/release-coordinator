"""Tests for contract v2: the OpenAPI differ, message payload types, OpenAPI
contract links, and both in the coordinator's contract check."""

import json
import unittest
from typing import Any
from unittest.mock import patch

from fastapi import BackgroundTasks

from src import webhooks
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.contracts import (
    contract_callers,
    contract_links,
    evaluate_contracts,
    is_contract_file,
    message_type_consumers,
    payload_candidates,
    read_contract_diffs,
    read_payload_diffs,
)
from src.core.contract_findings import ContractChangeKind, ContractFinding
from src.core.message_contract import PayloadType, diff_payload
from src.core.models import (
    ChannelBroker,
    ChannelKind,
    ChannelRole,
    ChannelUse,
    ContractCheckStatus,
    ContractLink,
    ContractRole,
    ContractScope,
    KnowledgeGraph,
    MessageChannel,
    ReasonCode,
    RepositoryCoverage,
    RiskAssessment,
    ServiceChannelUse,
    ServiceCoverage,
    channel_id,
)
from src.core.openapi_contract import OpenApiParseError, diff_openapi, is_openapi_file, parse_openapi
from src.orchestration.briefing import build_briefing
from src.tools.build_projects import parse_csproj
from src.tools.message_schemas import read_payload_types
from tests.fake_devops import FakeDevOps
from tests.test_contract_compat import LINK_GRAPH, _id
from tests.test_pr_webhook import BASE, HEAD, POLICY_YAML, BaselineFixture, _payload

ORDERS = """
openapi: 3.0.3
info: {title: orders, version: "1"}
paths:
  /orders/{id}:
    parameters:
      - {name: id, in: path, required: true, schema: {type: string}}
    get:
      operationId: getOrder
      parameters:
        - {name: verbose, in: query, schema: {type: boolean}}
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {$ref: "#/components/schemas/Order"}
        "404": {description: missing}
  /orders:
    post:
      operationId: createOrder
      requestBody:
        content:
          application/json:
            schema: {$ref: "#/components/schemas/NewOrder"}
      responses:
        "201": {description: created}
components:
  schemas:
    Order:
      allOf:
        - {$ref: "#/components/schemas/NewOrder"}
        - type: object
          required: [id, status]
          properties:
            id: {type: string}
            status: {type: string, enum: [open, shipped]}
            parent: {$ref: "#/components/schemas/Order"}
    NewOrder:
      type: object
      required: [customer]
      properties:
        customer: {type: string}
        quantity: {type: integer, format: int32}
        channel: {type: string, enum: [web, store, phone]}
"""


def _edit(text: str, *replacements: tuple[str, str]) -> str:
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    return text


def _changes(base: str, head: str) -> dict[ContractChangeKind, list[str]]:
    findings = diff_openapi("api/openapi.yaml", parse_openapi(base), parse_openapi(head))
    grouped: dict[ContractChangeKind, list[str]] = {}
    for finding in findings:
        grouped.setdefault(finding.change, []).append(finding.symbol)
    return grouped


class OpenApiNamingTests(unittest.TestCase):
    def test_document_names(self) -> None:
        for path in ("openapi.yaml", "src/Api/swagger.json", "openapi-v2.yml", "orders.openapi.yaml",
                     "docs/swagger_public.json", "Orders.Swagger.json"):
            self.assertTrue(is_openapi_file(path), path)
            self.assertTrue(is_contract_file(path), path)
        for path in ("openapi.md", "myopenapiish.yaml", "values.yaml", "swagger.js", "basket.proto.txt"):
            self.assertFalse(is_openapi_file(path), path)


class OpenApiReaderTests(unittest.TestCase):
    def test_reads_operations_parameters_refs_and_all_of(self) -> None:
        document = parse_openapi(ORDERS)
        self.assertEqual(sorted(document.operations), ["GET /orders/{}", "POST /orders"])
        get = document.operations["GET /orders/{}"]
        self.assertEqual(sorted(get.parameters), ["path:#0", "query:verbose"])
        self.assertTrue(get.parameters["path:#0"].required)
        order = get.responses["200"]
        assert order is not None
        self.assertEqual(sorted(order.properties), ["channel", "customer", "id", "parent", "quantity", "status"])
        self.assertEqual(order.required, ["customer", "id", "status"])
        self.assertTrue(order.properties["parent"].opaque.startswith("recursive"))
        self.assertIsNone(get.responses["404"])
        post = document.operations["POST /orders"]
        self.assertFalse(post.request_required)
        assert post.request is not None
        self.assertEqual(post.request.properties["channel"].enum, ["phone", "store", "web"])

    def test_swagger_two_body_parameters_and_definitions(self) -> None:
        swagger = {
            "swagger": "2.0",
            "paths": {"/items": {"post": {
                "parameters": [
                    {"name": "item", "in": "body", "required": True, "schema": {"$ref": "#/definitions/Item"}},
                    {"name": "dryRun", "in": "query", "type": "boolean"},
                ],
                "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/Item"}}},
            }}},
            "definitions": {"Item": {"type": "object", "properties": {"sku": {"type": "string"}}}},
        }
        document = parse_openapi(json.dumps(swagger), "swagger.json")
        post = document.operations["POST /items"]
        self.assertTrue(post.request_required)
        assert post.request is not None and post.responses["200"] is not None
        self.assertEqual(list(post.request.properties), ["sku"])
        self.assertEqual(post.parameters["query:dryRun"].schema_shape.type, "boolean")

    def test_not_openapi_is_a_parse_error(self) -> None:
        for text, path in (("a: [", "openapi.yaml"), ("name: chart", "openapi.yaml"), ("[1]", "swagger.json")):
            with self.assertRaises(OpenApiParseError):
                parse_openapi(text, path)
        with self.assertRaises(OpenApiParseError):
            parse_openapi(ORDERS.replace('"#/components/schemas/NewOrder"}\n      responses', '"#/components/schemas/Gone"}\n      responses'))


class OpenApiDiffTests(unittest.TestCase):
    def test_additive_changes_are_compatible(self) -> None:
        head = _edit(
            ORDERS,
            ("        - {name: verbose, in: query, schema: {type: boolean}}",
             "        - {name: verbose, in: query, schema: {type: boolean}}\n"
             "        - {name: fields, in: query, schema: {type: string}}"),
            ("        quantity: {type: integer, format: int32}",
             "        quantity: {type: integer, format: int32}\n        note: {type: string}"),
            ("        \"404\": {description: missing}", "        \"404\": {description: missing}\n        \"410\": {description: gone}"),
            ("  /orders:\n", "  /health:\n    get:\n      responses:\n        \"200\": {description: ok}\n  /orders:\n"),
            ("enum: [web, store, phone]", "enum: [web, store, phone, app]"),
            ("/orders/{id}:\n    parameters:\n      - {name: id,", "/orders/{orderId}:\n    parameters:\n      - {name: orderId,"),
        )
        findings = diff_openapi("openapi.yaml", parse_openapi(ORDERS), parse_openapi(head))
        self.assertFalse([f.detail for f in findings if f.breaking])
        kinds = {f.change for f in findings}
        self.assertEqual(
            kinds,
            {ContractChangeKind.PARAMETER_ADDED, ContractChangeKind.REQUEST_FIELD_ADDED,
             ContractChangeKind.RESPONSE_FIELD_ADDED, ContractChangeKind.RESPONSE_ADDED,
             ContractChangeKind.OPERATION_ADDED, ContractChangeKind.ENUM_WIDENED},
        )

    def test_breaking_changes(self) -> None:
        head = _edit(
            ORDERS,
            ("        - {name: verbose, in: query, schema: {type: boolean}}",
             "        - {name: verbose, in: query, required: true, schema: {type: boolean}}\n"
             "        - {name: tenant, in: header, required: true, schema: {type: string}}"),
            ("      required: [customer]\n", "      required: [customer, channel, region]\n"),
            ("        quantity: {type: integer, format: int32}",
             "        quantity: {type: integer, format: int64}\n        region: {type: string}"),
            ("enum: [web, store, phone]", "enum: [web, store]"),
            ("          required: [id, status]", "          required: [id]"),
            ("            status: {type: string, enum: [open, shipped]}\n", ""),
            ("      requestBody:\n", "      requestBody:\n        required: true\n"),
        )
        changes = _changes(ORDERS, head)
        self.assertEqual(
            sorted(changes[ContractChangeKind.PARAMETER_REQUIRED_ADDED]),
            ["GET /orders/{id} header parameter tenant", "GET /orders/{id} query parameter verbose"],
        )
        self.assertEqual(changes[ContractChangeKind.REQUEST_BODY_REQUIRED], ["POST /orders request body"])
        self.assertEqual(
            sorted(changes[ContractChangeKind.REQUEST_FIELD_REQUIRED_ADDED]),
            ["POST /orders request.channel", "POST /orders request.region"],
        )
        self.assertIn("POST /orders request.quantity", changes[ContractChangeKind.SCHEMA_TYPE_CHANGED])
        self.assertIn("GET /orders/{id} 200 response.quantity", changes[ContractChangeKind.SCHEMA_TYPE_CHANGED])
        self.assertEqual(changes[ContractChangeKind.ENUM_NARROWED], ["POST /orders request.channel"])
        self.assertEqual(changes[ContractChangeKind.RESPONSE_FIELD_REMOVED], ["GET /orders/{id} 200 response.status"])

    def test_removed_renamed_operations_and_responses(self) -> None:
        head = _edit(
            ORDERS,
            ("  /orders:\n    post:", "  /v2/orders:\n    post:"),
            ("        \"404\": {description: missing}", ""),
            ("      responses:\n        \"200\":", "      responses:\n        \"202\":"),
        )
        changes = _changes(ORDERS, head)
        self.assertEqual(changes[ContractChangeKind.OPERATION_RENAMED], ["POST /orders"])
        self.assertEqual(changes[ContractChangeKind.RESPONSE_REMOVED], ["GET /orders/{id} 200"])  # 404 isn't success
        self.assertNotIn(ContractChangeKind.OPERATION_ADDED, changes)
        no_id = _edit(head, ("      operationId: createOrder\n", ""))
        changes = _changes(_edit(ORDERS, ("      operationId: createOrder\n", "")), no_id)
        self.assertEqual(changes[ContractChangeKind.OPERATION_REMOVED], ["POST /orders"])
        self.assertEqual(changes[ContractChangeKind.OPERATION_ADDED], ["POST /v2/orders"])

    def test_response_enum_narrowing_is_safe_and_files(self) -> None:
        head = _edit(ORDERS, ("enum: [open, shipped]", "enum: [open]"))
        self.assertEqual(_changes(ORDERS, head), {})
        document = parse_openapi(ORDERS)
        self.assertEqual(diff_openapi("o.yaml", None, document)[0].change, ContractChangeKind.FILE_ADDED)
        self.assertTrue(diff_openapi("o.yaml", document, None)[0].breaking)


class PayloadTests(unittest.TestCase):
    CS = """namespace Ordering.IntegrationEvents;
public record OrderStartedIntegrationEvent(string UserId) : IntegrationEvent;
public record OrderPaidIntegrationEvent : IntegrationEvent
{
    public int OrderId { get; init; }
    public IEnumerable<OrderStockItem> OrderStockItems { get; init; }
    public static int Version { get; } = 1;
    private string Secret { get; set; }
}
"""

    def test_reads_csharp_and_java_types(self) -> None:
        types = read_payload_types(self.CS, "Events/OrderPaidIntegrationEvent.cs")
        assert types is not None
        self.assertEqual(types["OrderStartedIntegrationEvent"].properties, {"UserId": "string"})
        self.assertEqual(
            types["OrderPaidIntegrationEvent"].properties,
            {"OrderId": "int", "OrderStockItems": "IEnumerable<OrderStockItem>"},
        )
        java = read_payload_types(
            "class OrderEvent { private String orderId; private static final long serialVersionUID = 1L;"
            " transient int cache; }\nrecord Paid(String id, long amount) {}",
            "OrderEvent.java",
        )
        assert java is not None
        self.assertEqual(java["OrderEvent"].properties, {"orderId": "String"})
        self.assertEqual(java["Paid"].properties, {"id": "String", "amount": "long"})
        self.assertIsNone(read_payload_types("class {", "Broken.cs"))
        self.assertIsNone(read_payload_types("x = 1", "event.py"))

    def test_diff_rules(self) -> None:
        base = PayloadType(name="E", properties={"a": "int", "b": "string", "c": "string"})
        head = PayloadType(name="E", properties={"a": "long", "c": "string", "d": "bool"})
        findings = {f.symbol: f for f in diff_payload("E.cs", "E", base, head)}
        self.assertEqual(findings["E.a"].change, ContractChangeKind.FIELD_TYPE_CHANGED)
        self.assertEqual(findings["E.b"].change, ContractChangeKind.FIELD_REMOVED)
        self.assertFalse(findings["E.d"].breaking)
        self.assertTrue(all(findings[s].breaking for s in ("E.a", "E.b")))
        self.assertTrue(diff_payload("E.cs", "E", base, None)[0].breaking)
        self.assertFalse(diff_payload("E.cs", "E", None, head)[0].breaking)

    def test_candidates_and_reading(self) -> None:
        names = ["OrderPaidIntegrationEvent", "OrderStartedIntegrationEvent"]
        named, nearby = payload_candidates(
            ["/src/Ordering/Events/OrderPaidIntegrationEvent.cs", "src/Ordering/IntegrationEvents/Shared.cs",
             "src/Ordering/Program.cs", "src/Ordering/Events/notes.md"],
            names,
        )
        self.assertEqual(named, ["src/Ordering/Events/OrderPaidIntegrationEvent.cs"])
        self.assertEqual(nearby, ["src/Ordering/IntegrationEvents/Shared.cs"])
        head = self.CS.replace("    public int OrderId { get; init; }\n", "")
        files = {(named[0], "b"): self.CS, (named[0], "h"): head, (nearby[0], "b"): "class {", (nearby[0], "h"): "class {"}
        findings, unreadable = read_payload_diffs(
            lambda path, commit: files.get((path, commit)), read_payload_types, named, nearby, names, "b", "h"
        )
        self.assertEqual([(f.symbol, f.change) for f in findings],
                         [("OrderPaidIntegrationEvent.OrderId", ContractChangeKind.FIELD_REMOVED)])
        self.assertEqual(unreadable, {})  # the nearby file doesn't parse: skipped, not unreadable
        files[(named[0], "h")] = "class {"
        _, unreadable = read_payload_diffs(
            lambda path, commit: files.get((path, commit)), read_payload_types, named, [], names, "b", "h"
        )
        self.assertIn(named[0], unreadable)

    def test_message_type_consumers(self) -> None:
        def use(service: str, role: ChannelRole, kind: ChannelKind = ChannelKind.MESSAGE_TYPE) -> ServiceChannelUse:
            return ServiceChannelUse(service_id=service, use=ChannelUse(
                broker=ChannelBroker.EVENT_BUS, kind=kind, name="OrderPaid", role=role, file_path="a.cs", line=1, via="x"))
        channel = MessageChannel(
            channel_id=channel_id(ChannelBroker.EVENT_BUS, "OrderPaid"), broker=ChannelBroker.EVENT_BUS,
            kind=ChannelKind.MESSAGE_TYPE, name="OrderPaid", producers=["o"], consumers=["p", "w"],
            uses=[use("o", ChannelRole.PRODUCER), use("p", ChannelRole.CONSUMER), use("w", ChannelRole.CONSUMER)],
        )
        queue = channel.model_copy(update={"kind": ChannelKind.QUEUE, "name": "orders"})
        self.assertEqual(message_type_consumers([channel, queue]), {"OrderPaid": ["p", "w"]})


class LinkAndEvaluationTests(unittest.TestCase):
    def test_csproj_openapi_reference_is_a_client_link(self) -> None:
        csproj = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <ItemGroup>
    <OpenApiReference Include="..\\Catalog.API\\openapi.json" CodeGenerator="NSwagCSharp" />
    <OpenApiReference Include="https://example.com/swagger.json" />
  </ItemGroup>
</Project>"""
        project = parse_csproj(csproj, "src/WebApp/WebApp.csproj", {"src/Catalog.API/openapi.json"})
        self.assertEqual(
            project.contracts,
            [ContractLink(path="src/Catalog.API/openapi.json", role=ContractRole.CLIENT, project_file="src/WebApp/WebApp.csproj")],
        )

    def test_callers_of_a_served_document(self) -> None:
        doc = "src/Basket.API/openapi.yaml"
        graph = LINK_GRAPH.model_copy(update={"static_coverage": [RepositoryCoverage(
            repository="Shop", commit_sha="a" * 40,
            services=[ServiceCoverage(service_name="basket", root="src/Basket.API",
                                      contracts=[ContractLink(path=doc, role=ContractRole.SERVER)])],
        )]})
        links = contract_links(graph)
        self.assertEqual(contract_callers(links, graph.edges, "shop", [doc]), {doc: [_id("ordering"), _id("webapp")]})

    def test_openapi_only_pull_requests(self) -> None:
        doc = "api/openapi.yaml"
        additive = _edit(ORDERS, ("        quantity: {type: integer, format: int32}",
                                  "        quantity: {type: integer, format: int32}\n        note: {type: string}"))
        files = {(doc, "b"): ORDERS, (doc, "h"): additive}
        findings, unreadable = read_contract_diffs(lambda p, c: files.get((p, c)), [doc], "b", "h")
        check = evaluate_contracts(ContractScope.STRICT, [doc], findings, unreadable, {}, [])
        self.assertEqual(check.status, ContractCheckStatus.COMPATIBLE)
        files[(doc, "h")] = "openapi: 3.0.0\npaths: [1]"
        findings, unreadable = read_contract_diffs(lambda p, c: files.get((p, c)), [doc], "b", "h")
        self.assertIn("not understood", unreadable[doc])

    def test_payload_breaking_names_its_file(self) -> None:
        finding = ContractFinding(path="src/Events/E.cs", symbol="E.a", change=ContractChangeKind.FIELD_REMOVED,
                                  detail="Message property E.a was removed.")
        check = evaluate_contracts(ContractScope.STRICT, ["src/Events/E.cs"], [finding], {}, {}, [],
                                   message_consumers={"E": ["p"]})
        self.assertEqual(check.status, ContractCheckStatus.BREAKING)
        self.assertIn("src/Events/E.cs", check.detail)
        self.assertEqual(check.message_consumers, {"E": ["p"]})


class EndToEndTests(BaselineFixture):
    """The coordinator graph on pull requests changing an OpenAPI document
    (order-service's) and an integration event's payload type."""

    DOC = "src/order-service/openapi.yaml"
    EVENT = "src/order-service/Events/OrderPaid.cs"
    EVENT_BASE = "public record OrderPaid(string OrderId, decimal Total) : IntegrationEvent;"

    def _graph(self, scan: Any = None) -> KnowledgeGraph:
        graph = super()._graph() if scan is None else super()._graph(scan)
        uses = [
            ServiceChannelUse(service_id=service, use=ChannelUse(
                broker=ChannelBroker.EVENT_BUS, kind=ChannelKind.MESSAGE_TYPE, name="OrderPaid", role=role,
                file_path="x.cs", line=1, via="IEventBus"))
            for service, role in ((_id("order-service"), ChannelRole.PRODUCER), (_id("store-front"), ChannelRole.CONSUMER))
        ]
        channel = MessageChannel(
            channel_id=channel_id(ChannelBroker.EVENT_BUS, "OrderPaid"), broker=ChannelBroker.EVENT_BUS,
            kind=ChannelKind.MESSAGE_TYPE, name="OrderPaid",
            producers=[_id("order-service")], consumers=[_id("store-front")], uses=uses,
        )
        return graph.model_copy(update={"channels": [channel]})

    def _run(self, files: dict[tuple[str, str], str]) -> Any:
        self._write_baseline()
        changed = sorted({path for path, _ in files})
        payload = _payload(changed_files=changed, change_type="additive")
        tasks = BackgroundTasks()
        webhooks.pr_assessment_webhook(payload, tasks)
        devops = FakeDevOps(history=[BASE, HEAD], changes={(BASE, HEAD): changed}, files=files)
        with (
            patch.object(coordinator_nodes, "BASELINE_STORE", self.store),
            patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls,
            patch.object(coordinator_nodes, "DevOpsExtractor", devops),
        ):
            reasoner_cls.return_value.assess.return_value = RiskAssessment(
                risk_score=0.1, approved=True, justification="low risk"
            )
            final = webhooks.coordinator_app.invoke(tasks.tasks[0].args[2])
        return final["release_assessment"]

    def test_compatible_openapi_change_is_verified(self) -> None:
        head = _edit(ORDERS, ("        quantity: {type: integer, format: int32}",
                              "        quantity: {type: integer, format: int32}\n        note: {type: string}"))
        assessment = self._run({(self.DOC, BASE): ORDERS, (self.DOC, HEAD): head})
        self.assertEqual(assessment.evidence.contract_check.status, ContractCheckStatus.COMPATIBLE)
        self.assertNotIn(ReasonCode.COMPATIBILITY_UNVERIFIED, assessment.reason_codes)

    def test_breaking_openapi_change(self) -> None:
        head = _edit(ORDERS, ("            status: {type: string, enum: [open, shipped]}\n", ""))
        assessment = self._run({(self.DOC, BASE): ORDERS, (self.DOC, HEAD): head})
        self.assertEqual(assessment.reason_codes, [ReasonCode.CONTRACT_BREAKING])
        self.assertTrue(assessment.actionable)

    def test_breaking_event_payload_names_its_consumers(self) -> None:
        head = "public record OrderPaid(string OrderId) : IntegrationEvent;"
        assessment = self._run({(self.EVENT, BASE): self.EVENT_BASE, (self.EVENT, HEAD): head})
        check = assessment.evidence.contract_check
        self.assertEqual(check.status, ContractCheckStatus.BREAKING)
        self.assertEqual(check.message_consumers, {"OrderPaid": [_id("store-front")]})
        self.assertIn(ReasonCode.CONTRACT_BREAKING, assessment.reason_codes)
        group = next(g for g in build_briefing(assessment).groups if g.root_key == "decision:contracts")
        self.assertIn("Message property OrderPaid.Total was removed.", group.what_to_check)
        self.assertIn("Message type OrderPaid is consumed by: store-front", group.what_to_check)

    def test_additive_event_payload_stays_unverified_code(self) -> None:
        head = "public record OrderPaid(string OrderId, decimal Total, string Currency) : IntegrationEvent;"
        assessment = self._run({(self.EVENT, BASE): self.EVENT_BASE, (self.EVENT, HEAD): head})
        self.assertEqual(assessment.evidence.contract_check.status, ContractCheckStatus.NO_CONTRACT)
        self.assertNotIn(ReasonCode.CONTRACT_BREAKING, assessment.reason_codes)


if __name__ == "__main__":
    unittest.main()
