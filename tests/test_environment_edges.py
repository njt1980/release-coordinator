"""Edges a workload's Kubernetes environment declares (step 1f).

An endpoint-like variable set to a registered service is an edge even when
no code read of it was found (a Go helper hiding the name, a destructured
process.env, an SDK reading it itself). It is weaker evidence than code and
carries its own provenance; values naming nothing add no gaps.
"""

import unittest
from datetime import datetime, timezone
from typing import Any

from src.core.edge_resolution import K8S_ENV_PROVENANCE, environment_addresses, environment_edges
from src.core.models import DiscoveryMethod, ServiceEnvironment
from src.core.service_ids import IdentityContext, IdentityIndex
from src.discovery_engine import nodes as discovery_nodes
from tests.test_identity_scope import URN, _edge, _k8s, _static

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
CHECKOUT, CART, PAYMENT, OTHER = (f"{URN}:shop:{n}" for n in ("checkout", "cart", "payment", "other"))
OTHER_NS_CART = f"{URN}:other:cart"


class AddressTests(unittest.TestCase):
    def test_only_endpoint_like_names_with_addresses(self) -> None:
        variables = {
            "CART_SERVICE_ADDR": "cart:7070",
            "PAYMENT_URL": "http://payment:8080/pay",
            "ConnectionStrings__orders": "Host=orders-db;Database=o;Password=x",
            "APP_NAME": "frontend",  # not an endpoint name
            "LOG_LEVEL": "info",
            "FLAGD_HOST": "",
        }
        self.assertEqual(
            environment_addresses(variables),
            [("CART_SERVICE_ADDR", "cart:7070"), ("ConnectionStrings__orders", "orders-db"),
             ("PAYMENT_URL", "http://payment:8080")],
        )


class EdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = IdentityIndex(
            node.identity for node in (_k8s("shop", "checkout"), _k8s("shop", "cart"),
                                       _k8s("shop", "payment"), _k8s("other", "cart"))
        )
        self.context = IdentityContext(tenant="acme", environment="prod", cluster="aks-1")

    def edges(self, variables: dict[str, str]) -> list[tuple[str, str, str | None]]:
        environment = ServiceEnvironment(service_id=CHECKOUT, variables=variables)
        found = environment_edges({CHECKOUT: environment}, self.index, self.context, NOW)
        for edge in found:
            self.assertEqual(edge.provenance, K8S_ENV_PROVENANCE)
            self.assertEqual(edge.discovery_methods, [DiscoveryMethod.K8S_MANIFEST])
        return [(e.source_service_id, e.target_service_id, e.protocol) for e in found]

    def test_registered_targets_in_the_callers_namespace(self) -> None:
        self.assertEqual(
            self.edges({"CART_SERVICE_ADDR": "cart:7070", "PAYMENT_URL": "http://payment/pay"}),
            [(CHECKOUT, CART, None), (CHECKOUT, PAYMENT, "http")],
        )

    def test_a_variable_only_imported_from_a_shared_config_map_is_no_edge(self) -> None:
        # bank-of-anthos: several workloads import one address book (envFrom);
        # explicitly wired variables, and a config map only one service
        # imports, are edges.
        def sources(origin: str) -> list[str]:
            return [f"deployment/checkout/server:{origin}"]

        checkout = ServiceEnvironment(
            service_id=CHECKOUT,
            variables={"CART_SERVICE_ADDR": "cart:7070", "PAYMENT_URL": "http://payment/pay", "OTHER_URL": "http://other"},
            variable_sources={
                "CART_SERVICE_ADDR": sources("configMap/service-api-config"),
                "PAYMENT_URL": sources("configMapKeyRef/service-api-config"),
                "OTHER_URL": sources("configMap/checkout-config"),
            },
        )
        cart = ServiceEnvironment(
            service_id=CART,
            variables={"CART_SERVICE_ADDR": "cart:7070"},
            variable_sources={"CART_SERVICE_ADDR": ["deployment/cart/server:configMap/service-api-config"]},
        )
        index = IdentityIndex(
            node.identity
            for node in (_k8s("shop", "checkout"), _k8s("shop", "cart"), _k8s("shop", "payment"), _k8s("shop", "other"))
        )
        found = environment_edges({CHECKOUT: checkout, CART: cart}, index, self.context, NOW)
        self.assertEqual(sorted(e.target_service_id for e in found), sorted([PAYMENT, OTHER]))

    def test_unknown_self_and_non_endpoint_values_add_nothing(self) -> None:
        self.assertEqual(
            self.edges({"LEGACY_URL": "http://mainframe", "SELF_URL": "http://checkout", "APP_NAME": "cart"}),
            [],
        )


class ReconcileTests(unittest.TestCase):
    def test_env_edges_join_the_graph_and_corroborate_code(self) -> None:
        source = _static("checkout", "shop", "checkout", namespace="shop")
        state: Any = {
            "tenant": "acme",
            "environment": "prod",
            "cluster": "aks-1",
            "target_namespaces": ["shop"],
            "discovered_services": [source, _k8s("shop", "checkout"), _k8s("shop", "cart"), _k8s("shop", "payment")],
            "static_edges": [_edge(source.service_id, "payment", DiscoveryMethod.STATIC_CODE)],
            "runtime_edges": [],
            "service_environments": [
                ServiceEnvironment(
                    service_id=CHECKOUT,
                    variables={"CART_SERVICE_ADDR": "cart:7070", "PAYMENT_SERVICE_ADDR": "payment:50051"},
                )
            ],
        }
        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        edges = {(e.source_service_id, e.target_service_id): e for e in graph["edges"]}
        self.assertEqual(edges[(CHECKOUT, CART)].provenance, K8S_ENV_PROVENANCE)
        # The code edge to payment is corroborated, not duplicated.
        payment = edges[(CHECKOUT, PAYMENT)]
        self.assertIn(K8S_ENV_PROVENANCE, payment.provenance.split(","))
        self.assertIn(DiscoveryMethod.STATIC_CODE, payment.discovery_methods)
        self.assertEqual(len(graph["edges"]), 2)


if __name__ == "__main__":
    unittest.main()
