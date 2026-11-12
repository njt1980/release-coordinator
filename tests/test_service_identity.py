"""Tests for FQURI service identities, resolution, and reconciliation."""

import unittest
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    PullRequestPayload,
    ServiceIdentity,
    ServiceNode,
    UnresolvedEntity,
    UnresolvedEntityKind,
)
from src.core.service_ids import (
    IdentityContext,
    IdentityIndex,
    classify_unresolved,
    parse_reference,
    resolve_reference,
    unresolved_entity,
)
from src.discovery_engine import nodes as discovery_nodes

CONTEXT = IdentityContext(tenant="acme")
STATIC = "urn:release-coord:acme:unknown:unknown:unknown"
DEFAULT_NS = "urn:release-coord:acme:unknown:unknown:default"


def _edge(source: str, target: str, method: DiscoveryMethod) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[method],
        confidence_score=0.7,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _identity(namespace: str, name: str, cluster: str = "unknown") -> ServiceIdentity:
    return ServiceIdentity(
        tenant="acme", cluster=cluster, namespace=namespace, service_name=name
    )


class ServiceIdentityTests(unittest.TestCase):
    def test_uri_round_trips(self) -> None:
        identity = ServiceIdentity(
            tenant="acme",
            environment="prod",
            cluster="aks-east",
            namespace="shop",
            service_name="order-service",
        )
        uri = "urn:release-coord:acme:prod:aks-east:shop:order-service"
        self.assertEqual(identity.uri, uri)
        self.assertEqual(ServiceIdentity.parse(uri), identity)

    def test_unknown_segments_default_to_unknown(self) -> None:
        identity = ServiceIdentity(tenant="acme", service_name="api")
        self.assertEqual(identity.uri, "urn:release-coord:acme:unknown:unknown:unknown:api")

    def test_parse_rejects_malformed_uris(self) -> None:
        for uri in [
            "default/order-service",
            "urn:other:acme:prod:c:ns:svc",
            "urn:release-coord:acme:prod:c:ns",
            "urn:release-coord:acme:prod:c:ns:svc:extra",
            "urn:release-coord:acme:prod::ns:svc",
            "urn:release-coord:acme:prod:c:ns:../svc",
        ]:
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError):
                    ServiceIdentity.parse(uri)

    def test_unknown_segments_match_as_wildcards(self) -> None:
        query = _identity("unknown", "api")
        self.assertTrue(query.matches(_identity("shop", "api", cluster="aks")))
        self.assertFalse(query.matches(_identity("shop", "web")))
        self.assertFalse(_identity("shop", "api").matches(_identity("billing", "api")))
        # A known query segment never matches a candidate's unknown segment.
        self.assertFalse(_identity("shop", "api").matches(_identity("unknown", "api")))

    def test_service_nodes_and_pr_targets_require_fquris(self) -> None:
        with self.assertRaises(ValidationError):
            ServiceNode(service_id="default/order-service", name="order-service")
        with self.assertRaises(ValidationError):
            PullRequestPayload(
                pr_id="1", title="t", target_service="default/order-service", diff="d"
            )


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = IdentityIndex(
            [
                _identity("default", "order-service"),
                _identity("shop", "rabbitmq"),
                _identity("a", "web"),
                _identity("b", "web"),
            ]
        )

    def test_parse_reference_forms(self) -> None:
        for raw, expected in [
            (f"{DEFAULT_NS}:x", _identity("default", "x")),
            ("default/order-service", _identity("default", "order-service")),
            ("order-service:3000", _identity("unknown", "order-service")),
            ("Product-Service.prod.svc.cluster.local", _identity("prod", "product-service")),
            ("10.0.0.12", None),
            ("api.example.com", None),
            ("Order Service", None),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_reference(raw, CONTEXT), expected)

    def test_resolution_rules(self) -> None:
        with_default = CONTEXT.model_copy(update={"default_namespace": "a"})
        for raw, context, expected in [
            ("order-service", CONTEXT, _identity("default", "order-service")),
            ("rabbitmq:5672", CONTEXT, _identity("shop", "rabbitmq")),
            (f"{STATIC}:order-service", CONTEXT, _identity("default", "order-service")),
            ("web", CONTEXT, None),  # ambiguous: never guessed
            ("web", with_default, _identity("a", "web")),
            ("web.b.svc", CONTEXT, _identity("b", "web")),
            ("ai-service", with_default, None),  # not in the inventory
            ("api.example.com", CONTEXT, None),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(resolve_reference(raw, self.index, context), expected)

    def test_classify_unresolved(self) -> None:
        for raw, kind in [
            ("10.0.0.12", UnresolvedEntityKind.IP_ADDRESS),
            ("[fe80::1]:443", UnresolvedEntityKind.IP_ADDRESS),
            ("https://203.0.113.9/v1", UnresolvedEntityKind.IP_ADDRESS),
            ("db.corp.internal", UnresolvedEntityKind.PRIVATE_DNS),
            ("mydb.privatelink.database.windows.net", UnresolvedEntityKind.PRIVATE_DNS),
            ("api.stripe.com:443", UnresolvedEntityKind.EXTERNAL_DNS),
            ("ai-service", UnresolvedEntityKind.UNREGISTERED_SERVICE),
            ("ghost.default.svc", UnresolvedEntityKind.UNREGISTERED_SERVICE),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(classify_unresolved(raw), kind)

    def test_unresolved_entity_ids_never_collide_with_fquris(self) -> None:
        entity = unresolved_entity(" API.Stripe.com ")
        self.assertEqual(entity.entity_id, "unresolved:api.stripe.com")
        with self.assertRaises(ValidationError):
            UnresolvedEntity(
                entity_id="unresolved:other",
                reference="api.stripe.com",
                kind=UnresolvedEntityKind.EXTERNAL_DNS,
            )


class KnowledgeGraphIntegrityTests(unittest.TestCase):
    def test_edges_must_join_to_services_or_unresolved_entities(self) -> None:
        service = ServiceNode(service_id=f"{DEFAULT_NS}:a", name="a")
        entity = unresolved_entity("10.0.0.1")
        KnowledgeGraph(
            services=[service],
            edges=[_edge(service.service_id, entity.entity_id, DiscoveryMethod.STATIC_CODE)],
            unresolved_entities=[entity],
        )
        with self.assertRaises(ValidationError) as caught:
            KnowledgeGraph(
                services=[service],
                edges=[_edge(service.service_id, "10.0.0.1", DiscoveryMethod.STATIC_CODE)],
            )
        self.assertIn("10.0.0.1", str(caught.exception))

    def test_duplicate_service_ids_are_rejected(self) -> None:
        service = ServiceNode(service_id=f"{DEFAULT_NS}:a", name="a")
        with self.assertRaises(ValidationError):
            KnowledgeGraph(services=[service, service], edges=[])


class ReconcileIdentityTests(unittest.TestCase):
    def test_static_and_k8s_evidence_share_fquris(self) -> None:
        state: Any = {
            "tenant": "acme",
            "target_namespaces": ["default"],
            "discovered_services": [
                ServiceNode(
                    service_id=f"{STATIC}:store-front",
                    name="store-front",
                    metadata={"repository": "repo-a"},
                ),
                ServiceNode(service_id=f"{DEFAULT_NS}:store-front", name="store-front"),
                ServiceNode(service_id=f"{DEFAULT_NS}:order-service", name="order-service"),
            ],
            "static_edges": [
                _edge(f"{STATIC}:store-front", "order-service", DiscoveryMethod.STATIC_CODE),
                _edge(f"{STATIC}:store-front", "store-front", DiscoveryMethod.STATIC_CODE),
            ],
            "runtime_edges": [
                _edge(
                    "store-front",
                    "order-service.default.svc.cluster.local",
                    DiscoveryMethod.RUNTIME_TELEMETRY,
                )
            ],
        }

        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)

        self.assertEqual(
            [(s.service_id, s.metadata) for s in graph["services"]],
            [
                (f"{DEFAULT_NS}:store-front", {"repository": "repo-a"}),
                (f"{DEFAULT_NS}:order-service", {}),
            ],
        )
        (edge,) = graph["edges"]
        self.assertEqual(
            (edge.source_service_id, edge.target_service_id),
            (f"{DEFAULT_NS}:store-front", f"{DEFAULT_NS}:order-service"),
        )
        self.assertEqual(
            edge.discovery_methods,
            [DiscoveryMethod.STATIC_CODE, DiscoveryMethod.RUNTIME_TELEMETRY],
        )
        self.assertEqual(graph["unresolved_entities"], [])

    def test_endpoints_outside_the_inventory_become_unresolved_entities(self) -> None:
        state: Any = {
            "tenant": "acme",
            "target_namespaces": ["a", "b"],
            "discovered_services": [
                ServiceNode(service_id=f"{STATIC}:checkout", name="checkout"),
            ],
            "static_edges": [
                _edge(f"{STATIC}:checkout", "ai-service", DiscoveryMethod.STATIC_CODE),
            ],
            "runtime_edges": [
                _edge("checkout", "10.0.0.12:5432", DiscoveryMethod.RUNTIME_TELEMETRY),
                _edge("checkout", "vault.corp.internal", DiscoveryMethod.RUNTIME_TELEMETRY),
            ],
        }

        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)

        self.assertEqual(
            [(e.entity_id, e.kind) for e in graph["unresolved_entities"]],
            [
                ("unresolved:ai-service", UnresolvedEntityKind.UNREGISTERED_SERVICE),
                ("unresolved:10.0.0.12:5432", UnresolvedEntityKind.IP_ADDRESS),
                ("unresolved:vault.corp.internal", UnresolvedEntityKind.PRIVATE_DNS),
            ],
        )
        self.assertEqual(
            [e.target_service_id for e in graph["edges"]],
            [e.entity_id for e in graph["unresolved_entities"]],
        )
        # The reconciled graph is a valid baseline graph.
        KnowledgeGraph(
            services=graph["services"],
            edges=graph["edges"],
            unresolved_entities=graph["unresolved_entities"],
        )

    def test_merging_keeps_mock_fixture_provenance_visible(self) -> None:
        a, b = f"{DEFAULT_NS}:a", f"{DEFAULT_NS}:b"
        static = _edge(a, b, DiscoveryMethod.STATIC_CODE).model_copy(
            update={"provenance": "azure_devops"}
        )
        mock = DependencyEdge(
            source_service_id="a",
            target_service_id="b",
            discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
            confidence_score=0.0,
            last_observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            provenance="mock_fixture",
            environment="mock",
        )
        state: Any = {
            "tenant": "acme",
            "discovered_services": [
                ServiceNode(service_id=a, name="a"),
                ServiceNode(service_id=b, name="b"),
            ],
            "static_edges": [static],
            "runtime_edges": [mock],
        }

        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)

        (edge,) = graph["edges"]
        self.assertEqual(edge.provenance, "azure_devops,mock_fixture")
        self.assertEqual(edge.environment, "unknown,mock")
        self.assertEqual(edge.confidence_score, 0.7)
        self.assertTrue(edge.is_mock_fixture)


class EdgeProvenanceTests(unittest.TestCase):
    def test_mock_fixture_edges_must_have_zero_confidence(self) -> None:
        with self.assertRaises(ValidationError):
            DependencyEdge(
                source_service_id="a",
                target_service_id="b",
                discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
                confidence_score=1.0,
                last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                provenance="mock_fixture",
            )

    def test_legacy_edges_default_to_unknown_labels(self) -> None:
        edge = DependencyEdge.model_validate(
            {
                "source_service_id": "a",
                "target_service_id": "b",
                "discovery_methods": ["STATIC_CODE"],
                "confidence_score": 0.7,
                "last_observed_at": "2026-01-01T00:00:00Z",
            }
        )
        self.assertEqual((edge.provenance, edge.environment), ("unknown", "unknown"))
        self.assertFalse(edge.is_mock_fixture)


if __name__ == "__main__":
    unittest.main()
