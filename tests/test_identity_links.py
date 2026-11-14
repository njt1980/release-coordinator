"""Linking source-code names to runtime names (step 1d).

Only meaning-preserving canonical equality links (case and ``-_.``
separators, unique on both sides); near names are suggestions only.
"""

import unittest

from src.coordinator.nodes import identity_issues
from src.core.identity_links import LinkEvidenceKind, fold, link_identities, suggestions
from src.core.models import (
    IDENTITY_LINK_METADATA_KEY,
    IDENTITY_SUGGESTIONS_METADATA_KEY,
    IDENTITY_UNLINKED_METADATA_KEY,
    DiscoveryMethod,
    KnowledgeGraph,
    ReasonCode,
    UnresolvedEntityKind,
)
from tests.test_identity_scope import URN, _edge, _k8s, _reconcile, _static


class FoldTests(unittest.TestCase):
    def test_case_and_separators_only(self) -> None:
        self.assertEqual(fold("Order-Processor"), "orderprocessor")
        self.assertEqual(fold("order_processor.api"), "orderprocessorapi")
        # Suffixes and plurals are meaning, not form: never folded away.
        self.assertNotEqual(fold("webhookclient"), fold("webhooksclient"))
        self.assertNotEqual(fold("cart"), fold("cartservice"))


class LinkTests(unittest.TestCase):
    def test_eshop_names(self) -> None:
        result = link_identities(
            ["orderprocessor", "paymentprocessor", "webhookclient", "basket-api"],
            ["order-processor", "payment-processor", "webhooksclient", "eventbus"],
        )
        self.assertEqual(
            [(l.source_name, l.runtime_name, l.evidence) for l in result.links],
            [
                ("orderprocessor", "order-processor", LinkEvidenceKind.CANONICAL_EQUAL),
                ("paymentprocessor", "payment-processor", LinkEvidenceKind.CANONICAL_EQUAL),
            ],
        )
        self.assertEqual(result.unlinked["webhookclient"], ["webhooksclient"])
        self.assertEqual(result.unlinked["basket-api"], [])

    def test_equality_must_be_unique_on_both_sides(self) -> None:
        # Two runtime names fold to one key: nothing links.
        result = link_identities(["orderapi"], ["order-api", "order_api"])
        self.assertEqual(result.links, [])
        self.assertEqual(sorted(result.unlinked["orderapi"]), [])
        # Two source names fold to one key: nothing links either.
        self.assertEqual(link_identities(["order-api", "orderapi"], ["order.api"]).links, [])

    def test_suggestions_are_near_names_only(self) -> None:
        self.assertEqual(suggestions("cart", ["cartservice", "checkout", "kart"]), ["kart", "cartservice"])
        self.assertEqual(suggestions("payments", ["frontend"]), [])


class ReconcileLinkTests(unittest.TestCase):
    def test_linked_service_merges_and_its_code_edges_follow(self) -> None:
        source = _static("orderprocessor", "eShop", "src/OrderProcessor")
        graph = _reconcile(
            [
                source,
                _k8s("shop", "order-processor"),
                _k8s("shop", "eventbus"),
            ],
            static_edges=[
                _edge(source.service_id, "env:orderprocessor/EVENTBUS_URL", DiscoveryMethod.STATIC_CODE),
                _edge(source.service_id, "http://eventbus", DiscoveryMethod.STATIC_CODE),
            ],
        )
        services = {s.service_id: s for s in graph["services"]}
        merged = services[f"{URN}:shop:order-processor"]
        self.assertEqual(merged.metadata["repository"], "eShop")
        self.assertEqual(
            merged.metadata[IDENTITY_LINK_METADATA_KEY], "CANONICAL_EQUAL: orderprocessor = order-processor"
        )
        self.assertNotIn(f"{URN}:shop:orderprocessor", services)
        # The env reference now belongs to the runtime service (unresolved
        # here only because the test sets no environment).
        targets = {e.target_service_id for e in graph["edges"] if e.source_service_id == merged.service_id}
        self.assertIn(f"{URN}:shop:eventbus", targets)
        kinds = {u.reference: u.kind for u in graph["unresolved_entities"]}
        self.assertIn("env:order-processor/eventbus_url", {r.lower() for r in kinds})

    def test_unlinked_service_is_flagged_with_suggestions(self) -> None:
        graph = _reconcile(
            [_static("webhookclient", "eShop", "src/WebhookClient"), _k8s("shop", "webhooksclient")]
        )
        services = {s.service_id: s for s in graph["services"]}
        flagged = services[f"{URN}:shop:webhookclient"]
        self.assertEqual(flagged.metadata[IDENTITY_UNLINKED_METADATA_KEY], "true")
        self.assertEqual(flagged.metadata[IDENTITY_SUGGESTIONS_METADATA_KEY], "webhooksclient")
        self.assertIn(f"{URN}:shop:webhooksclient", services)  # never merged by a near name

    def test_exact_names_and_static_only_runs_are_unchanged(self) -> None:
        graph = _reconcile([_static("basket-api", "eShop", "src/Basket.API"), _k8s("shop", "basket-api")])
        (service,) = graph["services"]
        self.assertNotIn(IDENTITY_UNLINKED_METADATA_KEY, service.metadata)
        self.assertNotIn(IDENTITY_LINK_METADATA_KEY, service.metadata)
        # No Kubernetes source: nothing to link against, nothing flagged.
        graph = _reconcile([_static("orders", "repo", "orders")], namespaces=[])
        self.assertNotIn(IDENTITY_UNLINKED_METADATA_KEY, graph["services"][0].metadata)


class IdentityIssueTests(unittest.TestCase):
    def test_unlinked_services_in_the_radius_block(self) -> None:
        graph_dict = _reconcile(
            [
                _static("webhookclient", "eShop", "src/WebhookClient"),
                _k8s("shop", "webhooksclient"),
                _k8s("shop", "identity-api"),
            ]
        )
        graph = KnowledgeGraph(
            services=graph_dict["services"],
            edges=graph_dict["edges"],
            unresolved_entities=graph_dict["unresolved_entities"],
        )
        (issue,) = identity_issues(
            graph, [f"{URN}:shop:webhookclient", f"{URN}:shop:identity-api", f"{URN}:shop:webhookclient"]
        )
        self.assertEqual(issue.code, ReasonCode.IDENTITY_UNLINKED)
        self.assertIn("eShop/src/WebhookClient", issue.detail)
        self.assertIn("Is it deployed as webhooksclient?", issue.detail)
        self.assertTrue(ReasonCode.IDENTITY_UNLINKED.limits_actionability)
        self.assertEqual(identity_issues(graph, [f"{URN}:shop:identity-api"]), [])


if __name__ == "__main__":
    unittest.main()
