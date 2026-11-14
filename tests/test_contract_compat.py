"""Tests for contract compatibility v1 (plan step 2e): .proto differ, contract
links from .NET builds, strict and interfaces scopes, and the verdict."""

import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from fastapi import BackgroundTasks

from src import webhooks
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.contracts import (
    contract_consumers,
    contract_links,
    evaluate_contracts,
    read_contract_diffs,
    uncovered_callers,
)
from src.coordinator.policy import decide_release, plan_reason_codes
from src.coordinator.sequencing import DeploymentPlan
from src.core.models import (
    ContractCheckStatus,
    ContractLink,
    ContractRole,
    ContractScope,
    ContractChange,
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    ReasonCode,
    RepositoryCoverage,
    RiskAssessment,
    ServiceCoverage,
    ServiceNode,
)
from src.core.proto_contract import ContractChangeKind, ProtoParseError, diff_protos, parse_proto
from src.orchestration.briefing import build_briefing
from src.tools.build_projects import parse_csproj
from tests.fake_devops import FakeDevOps
from tests.test_pr_webhook import BASE, HEAD, POLICY_YAML, BaselineFixture, _id, _payload

# eShop's Basket.API/Proto/basket.proto (dotnet/eShop, MIT), the plan's acceptance case.
BASKET = """﻿syntax = "proto3";

option csharp_namespace = "eShop.Basket.API.Grpc";

package BasketApi;

service Basket {
    rpc GetBasket(GetBasketRequest) returns (CustomerBasketResponse) {}
    rpc UpdateBasket(UpdateBasketRequest) returns (CustomerBasketResponse) {}
    rpc DeleteBasket(DeleteBasketRequest) returns (DeleteBasketResponse) {}
}

message GetBasketRequest {
}

message CustomerBasketResponse {
    repeated BasketItem items = 1;
}

message BasketItem {
    int32 product_id = 2;
    int32 quantity = 6;
}

message UpdateBasketRequest {
    repeated BasketItem items = 2;
}

message DeleteBasketRequest {
}

message DeleteBasketResponse {
}"""
# Allowed: a new optional field and a new RPC.
BASKET_ADDITIVE = BASKET.replace(
    "    int32 quantity = 6;\n", "    int32 quantity = 6;\n    string note = 7; // gift note\n"
).replace("service Basket {\n", "service Basket {\n    rpc ClearBasket(DeleteBasketRequest) returns (DeleteBasketResponse);\n")
# Blocked: quantity removed, product_id renamed.
BASKET_BREAKING = BASKET.replace("    int32 quantity = 6;\n", "").replace("product_id = 2", "item_id = 2")

PATH = "src/Basket.API/Proto/basket.proto"


def _kinds(base: str | None, head: str | None) -> set[ContractChangeKind]:
    return {
        finding.change
        for finding in diff_protos("x.proto", parse_proto(base) if base else None, parse_proto(head) if head else None)
    }


class ProtoReaderTests(unittest.TestCase):
    def test_reads_messages_services_enums_and_nesting(self) -> None:
        proto = parse_proto(
            """
            syntax = "proto3";
            package shop.v1;
            import "google/protobuf/timestamp.proto";
            /* block comment */
            message Order {
              option deprecated = true;
              reserved 4, 5;
              message Line { string sku = 1; }
              enum State { STATE_UNKNOWN = 0; STATE_PAID = 1 [deprecated = true]; }
              repeated Line lines = 1;
              map<string, int32> tags = 2;
              oneof payment { string card = 3; string iban = 6; }
              optional .shop.v1.Order.State state = 7 [json_name = "st"];
            }
            service Orders { rpc Watch(stream Order) returns (stream Order); }
            """
        )
        order = proto.messages["shop.v1.Order"]
        self.assertEqual(order.fields[1].type, "Line")  # as written
        self.assertEqual((order.fields[2].label, order.fields[2].type), ("map", "map<string,int32>"))
        self.assertEqual(order.fields[6].oneof, "payment")
        self.assertEqual((order.fields[7].label, order.fields[7].type), ("optional", "Order.State"))
        self.assertIn("shop.v1.Order.Line", proto.messages)
        self.assertEqual(proto.enums["shop.v1.Order.State"].values, {0: "STATE_UNKNOWN", 1: "STATE_PAID"})
        rpc = proto.services["shop.v1.Orders"].rpcs["Watch"]
        self.assertTrue(rpc.client_streaming and rpc.server_streaming)

    def test_garbage_is_a_parse_error(self) -> None:
        for text in ("message {", "message A { string a = ; }", "@@@"):
            with self.subTest(text=text), self.assertRaises(ProtoParseError):
                parse_proto(text)


class ProtoDiffTests(unittest.TestCase):
    def test_eshop_basket_additive_change_is_compatible(self) -> None:
        kinds = _kinds(BASKET, BASKET_ADDITIVE)
        self.assertEqual(kinds, {ContractChangeKind.FIELD_ADDED, ContractChangeKind.RPC_ADDED})

    def test_eshop_basket_breaking_change_is_breaking(self) -> None:
        findings = diff_protos(PATH, parse_proto(BASKET), parse_proto(BASKET_BREAKING))
        self.assertEqual(
            {(f.change, f.symbol) for f in findings},
            {
                (ContractChangeKind.FIELD_REMOVED, "BasketItem.quantity"),
                (ContractChangeKind.FIELD_RENAMED, "BasketItem.product_id"),
            },
        )
        self.assertTrue(all(f.breaking for f in findings))

    def test_rules(self) -> None:
        base = """syntax = "proto3"; package p;
            message M { string a = 1; int32 b = 2; repeated string c = 3; string d = 4; }
            enum E { E_ZERO = 0; E_ONE = 1; }
            service S { rpc Get(M) returns (M); rpc Stream(M) returns (M); }"""
        cases = {
            "string a = 1;": ("bytes a = 1;", ContractChangeKind.FIELD_TYPE_CHANGED),
            "repeated string c = 3;": ("string c = 3;", ContractChangeKind.FIELD_CARDINALITY_CHANGED),
            "string d = 4;": ("oneof o { string d = 4; }", ContractChangeKind.FIELD_ONEOF_CHANGED),
            "E_ONE = 1;": ("E_UNO = 1;", ContractChangeKind.ENUM_VALUE_RENAMED),
            "rpc Get(M) returns (M);": ("", ContractChangeKind.RPC_REMOVED),
            "rpc Stream(M) returns (M);": ("rpc Stream(M) returns (stream M);", ContractChangeKind.RPC_STREAMING_CHANGED),
            "package p;": ("package q;", ContractChangeKind.PACKAGE_CHANGED),
        }
        for old, (new, expected) in cases.items():
            with self.subTest(change=expected):
                kinds = _kinds(base, base.replace(old, new))
                self.assertIn(expected, kinds)
        self.assertEqual(_kinds(base, base.replace("int32 b = 2;", "optional int32 b = 2;")),
                         {ContractChangeKind.FIELD_PRESENCE_CHANGED})
        self.assertEqual(_kinds(base, base.replace("E_ONE = 1;", "E_ONE = 1; E_TWO = 2;")),
                         {ContractChangeKind.ENUM_VALUE_ADDED})
        self.assertEqual(_kinds(None, base), {ContractChangeKind.FILE_ADDED})
        self.assertEqual(_kinds(base, None), {ContractChangeKind.FILE_REMOVED})


class ContractLinkTests(unittest.TestCase):
    def test_csproj_protobuf_items_become_links(self) -> None:
        files = {PATH, "src/WebApp/WebApp.csproj", "src/Shared/a.proto", "src/Shared/b.proto"}
        web = parse_csproj(
            """<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup>
                 <Protobuf Include="..\\Basket.API\\Proto\\basket.proto" GrpcServices="Client" />
                 <Protobuf Include="..\\Shared\\*.proto" />
                 <Protobuf Include="..\\Shared\\a.proto" GrpcServices="None" />
                 <Protobuf Include="..\\Missing\\x.proto" GrpcServices="Server" />
               </ItemGroup></Project>""",
            "/src/WebApp/WebApp.csproj",
            files,
        )
        self.assertEqual(
            [(link.path, link.role) for link in web.contracts],
            [(PATH, ContractRole.CLIENT), ("src/Shared/a.proto", ContractRole.BOTH), ("src/Shared/b.proto", ContractRole.BOTH)],
        )
        self.assertEqual(web.contracts[0].project_file, "src/WebApp/WebApp.csproj")


def _node(name: str, root: str) -> ServiceNode:
    return ServiceNode(service_id=_id(name), name=name, metadata={"repository": "Shop", "service_root": root})


def _edge(source: str, target: str) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=_id(source), target_service_id=_id(target), discovery_methods=[DiscoveryMethod.STATIC_CODE],
        confidence_score=1.0, last_observed_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
    )


LINK_GRAPH = KnowledgeGraph(
    services=[_node("basket", "src/Basket.API"), _node("webapp", "src/WebApp"), _node("ordering", "src/Ordering")],
    edges=[_edge("webapp", "basket"), _edge("ordering", "basket")],
    static_coverage=[
        RepositoryCoverage(
            repository="Shop",
            commit_sha="a" * 40,
            services=[
                ServiceCoverage(service_name="basket", root="src/Basket.API",
                                contracts=[ContractLink(path=PATH, role=ContractRole.SERVER)]),
                ServiceCoverage(service_name="webapp", root="src/WebApp",
                                contracts=[ContractLink(path=PATH, role=ContractRole.CLIENT)]),
                ServiceCoverage(service_name="ordering", root="src/Ordering"),
            ],
        )
    ],
)


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.links = contract_links(LINK_GRAPH)

    def test_consumers_and_uncovered_callers(self) -> None:
        self.assertEqual(contract_consumers(self.links, "shop", [PATH]), {PATH: [_id("webapp")]})
        # ordering calls basket over no contract; webapp has one.
        self.assertEqual(uncovered_callers(self.links, [_id("basket")], LINK_GRAPH.edges), [_id("ordering")])
        # A changed service serving no contract is itself uncovered.
        self.assertEqual(uncovered_callers(self.links, [_id("ordering")], LINK_GRAPH.edges), [_id("ordering")])

    def test_status_order_and_scopes(self) -> None:
        added = diff_protos(PATH, parse_proto(BASKET), parse_proto(BASKET_ADDITIVE))
        broken = diff_protos(PATH, parse_proto(BASKET), parse_proto(BASKET_BREAKING))
        code = "src/Basket.API/Program.cs"
        cases = [
            (ContractScope.STRICT, [PATH], added, {}, [], ContractCheckStatus.COMPATIBLE),
            (ContractScope.STRICT, [PATH], broken, {}, [], ContractCheckStatus.BREAKING),
            (ContractScope.STRICT, [PATH], added, {PATH: "couldn't be read"}, [], ContractCheckStatus.UNREADABLE),
            (ContractScope.STRICT, [PATH, code], added, {}, [], ContractCheckStatus.CODE_CHANGED),
            (ContractScope.STRICT, [code], [], {}, [], ContractCheckStatus.NO_CONTRACT),
            (ContractScope.INTERFACES, [code], [], {}, [_id("ordering")], ContractCheckStatus.NO_CONTRACT),
            (ContractScope.INTERFACES, [PATH, code], added, {}, [], ContractCheckStatus.COMPATIBLE),
            (ContractScope.INTERFACES, [PATH, code], broken, {}, [], ContractCheckStatus.BREAKING),
        ]
        for scope, files, findings, unreadable, uncovered, expected in cases:
            with self.subTest(scope=scope, files=files, expected=expected):
                check = evaluate_contracts(scope, files, findings, unreadable, {}, uncovered)
                self.assertEqual(check.status, expected)
                self.assertTrue(check.detail)

    def test_reading_tells_absent_from_failed(self) -> None:
        files = {(PATH, "base"): BASKET}

        def read(path: str, commit: str) -> str | None:
            if path == "boom.proto":
                raise RuntimeError("403")
            return files.get((path, commit))

        findings, unreadable = read_contract_diffs(read, [PATH, "boom.proto", "gone.proto"], "base", "head")
        self.assertEqual([f.change for f in findings], [ContractChangeKind.FILE_REMOVED])
        self.assertEqual(set(unreadable), {"boom.proto", "gone.proto"})


class VerdictTests(unittest.TestCase):
    def _plan(self, **fields: Any) -> DeploymentPlan:
        return DeploymentPlan(change_type=ContractChange.ADDITIVE, sequence=["a"], **fields)

    def test_codes(self) -> None:
        self.assertEqual(plan_reason_codes(self._plan()), [ReasonCode.COMPATIBILITY_UNVERIFIED])
        self.assertEqual(plan_reason_codes(self._plan(compatibility_verified=True)), [])
        self.assertEqual(plan_reason_codes(self._plan(contract_breaking=True)), [ReasonCode.CONTRACT_BREAKING])

    def test_breaking_is_an_actionable_no_go(self) -> None:
        advisory = RiskAssessment(risk_score=0.1, approved=True, justification="fine")
        verdict = decide_release([], self._plan(contract_breaking=True), advisory)
        self.assertFalse(verdict.approved)
        self.assertTrue(verdict.actionable)
        self.assertTrue(decide_release([], self._plan(compatibility_verified=True), advisory).approved)


class EndToEndTests(BaselineFixture):
    """The coordinator graph on a contract-only pull request (order-service owns the proto)."""

    PROTO = "src/order-service/proto/orders.proto"

    def _run(self, head: str, extra_files: list[str] | None = None, policy_extra: str = "",
             head_contracts: dict[str, list[ContractLink]] | None = None) -> Any:
        if policy_extra:
            self._write_policy(POLICY_YAML + policy_extra)
        self._write_baseline()
        changed = [self.PROTO, *(extra_files or [])]
        payload = _payload(changed_files=changed, change_type="additive")
        tasks = BackgroundTasks()
        webhooks.pr_assessment_webhook(payload, tasks)
        devops = FakeDevOps(
            history=[BASE, HEAD],
            changes={(BASE, HEAD): changed},
            files={(self.PROTO, BASE): BASKET, (self.PROTO, HEAD): head},
            head_contracts=head_contracts,
        )
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

    def test_compatible_contract_only_change_is_approved(self) -> None:
        assessment = self._run(BASKET_ADDITIVE)
        self.assertEqual(assessment.evidence.contract_check.status, ContractCheckStatus.COMPATIBLE)
        self.assertEqual(assessment.reason_codes, [])
        self.assertTrue(assessment.approved and assessment.actionable and assessment.safe_order_established)
        briefing = build_briefing(assessment)
        self.assertTrue(briefing.headline.startswith("Go"))
        self.assertIn("info:contracts", [group.root_key for group in briefing.groups])

    def test_breaking_contract_change_is_blocked_but_actionable(self) -> None:
        assessment = self._run(BASKET_BREAKING)
        self.assertEqual(assessment.reason_codes, [ReasonCode.CONTRACT_BREAKING])
        self.assertFalse(assessment.approved)
        self.assertTrue(assessment.actionable)
        briefing = build_briefing(assessment)
        self.assertTrue(briefing.headline.startswith("No-go: risk found"))
        group = next(g for g in briefing.groups if g.root_key == "decision:contracts")
        self.assertIn("Field BasketItem.quantity = 6 was removed.", group.what_to_check)

    def test_code_changes_need_the_interfaces_scope_and_covered_callers(self) -> None:
        code = "src/order-service/routes/root.js"
        strict = self._run(BASKET_ADDITIVE, [code])
        self.assertEqual(strict.evidence.contract_check.status, ContractCheckStatus.CODE_CHANGED)
        self.assertIn(ReasonCode.COMPATIBILITY_UNVERIFIED, strict.reason_codes)
        # interfaces: store-front calls order-service but compiles no contract.
        uncovered = self._run(BASKET_ADDITIVE, [code], policy_extra="contract_scope: interfaces\n")
        self.assertEqual(uncovered.evidence.contract_check.status, ContractCheckStatus.NO_CONTRACT)
        # order-service serves no contract and store-front compiles none.
        self.assertEqual(
            uncovered.evidence.contract_check.uncovered_callers, [_id("order-service"), _id("store-front")]
        )
        # Both sides compile the proto (order-service at the head too): verified.
        self.client_link = True
        covered = self._run(
            BASKET_ADDITIVE, [code], policy_extra="contract_scope: interfaces\n",
            head_contracts={"src/order-service": [ContractLink(path=self.PROTO, role=ContractRole.SERVER)]},
        )
        self.assertEqual(covered.evidence.contract_check.status, ContractCheckStatus.COMPATIBLE)
        self.assertNotIn(ReasonCode.COMPATIBILITY_UNVERIFIED, covered.reason_codes)

    def _graph(self, scan: Any = None) -> KnowledgeGraph:
        graph = super()._graph() if scan is None else super()._graph(scan)
        if not getattr(self, "client_link", False):
            return graph
        coverage = [
            repository.model_copy(update={"services": [
                service.model_copy(update={"contracts": [
                    ContractLink(path=self.PROTO, role=ContractRole.CLIENT if service.root == "src/store-front"
                                 else ContractRole.SERVER)
                ]}) if service.root in ("src/store-front", "src/order-service") else service
                for service in repository.services
            ]})
            for repository in graph.static_coverage or []
        ]
        return graph.model_copy(update={"static_coverage": coverage})


if __name__ == "__main__":
    unittest.main()
