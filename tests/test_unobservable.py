"""Protocol-aware live coverage (step 1e): unobservable is not silent.

A radius service talking only protocols the telemetry can't parse (AMQP
0-9-1, Azure Service Bus...) is never seen however busy it is. It must not
pass as a quiet service; it is accepted only per protocol, with a reason.
"""

import unittest
from datetime import date, datetime, timedelta, timezone


from src.coordinator.project_policy import (
    live_coverage,
    live_runtime_state,
    runtime_evidence_issue,
    unobservable_protocols,
)
from src.core.models import DependencyEdge, DiscoveryMethod, ReasonCode, RuntimeEvidenceState
from src.core.telemetry_capability import BEYLA, wire_protocol
from src.tools.baseline_store import BaselineScope
from src.tools.policy_store import PolicyInvalidError, parse_policy
from tests.test_identity_scope import URN as SCOPE_URN
from tests.test_identity_scope import _edge as _scope_edge
from tests.test_identity_scope import _k8s, _reconcile, _static
from tests.test_live_evidence import FRESH, NOW, POLICY, POLICY_YAML, URN, _observed

ORDER, PAYMENTS, RABBIT, FRONT = (f"{URN}:{n}" for n in ("order", "payments", "rabbitmq", "front"))


def _edge(source: str, target: str, protocol: str | None, seen: bool = False) -> DependencyEdge:
    methods = [DiscoveryMethod.RUNTIME_TELEMETRY] if seen else [DiscoveryMethod.STATIC_CODE]
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=methods,
        confidence_score=0.7,
        last_observed_at=FRESH,
        protocol=protocol,
    )


def _policy(extra: str) -> object:
    return parse_policy(POLICY_YAML + extra, BaselineScope(tenant="acme", project_id="shop"))


class WireProtocolTests(unittest.TestCase):
    def test_schemes(self) -> None:
        cases = {
            "http://orders:8080/api": "http",
            "https+http://catalog-api": "https",
            "jdbc:postgresql://db:5432/app": "postgres",
            "amqp://eventbus:5672": "amqp",  # 0-9-1 or 1.0: the scheme doesn't say
            "sb://bus.servicebus.windows.net": "servicebus",
            "lb://vets-service": "http",
            "tcp://legacy:9000": "unknown",
            "eventbus:5672": None,
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.assertEqual(wire_protocol(address), expected)

    def test_beyla_profile(self) -> None:
        for protocol in ("http", "grpc", "kafka", "postgres", "redis"):
            self.assertTrue(BEYLA.can_observe(protocol), protocol)
        for protocol in ("amqp", "amqps", "unknown", None):
            self.assertFalse(BEYLA.can_observe(protocol), protocol)


class UnobservableProtocolTests(unittest.TestCase):
    def test_only_provably_blind_services(self) -> None:
        edges = [
            _edge(PAYMENTS, RABBIT, "amqp"),  # payments: AMQP only
            _edge(ORDER, RABBIT, "amqp"),
            _edge(ORDER, PAYMENTS, "http"),  # order and payments also speak HTTP...
            _edge(FRONT, "unresolved:legacy", None),  # ...front's protocol is unknown
        ]
        blind = unobservable_protocols(edges, {PAYMENTS, ORDER, RABBIT, FRONT})
        # payments is reached over HTTP, so it is observable; rabbitmq only over AMQP.
        self.assertEqual(blind, {RABBIT: ["amqp"]})

    def test_a_telemetry_sighting_makes_an_edge_observable(self) -> None:
        edges = [_edge(PAYMENTS, RABBIT, "amqp", seen=True)]
        self.assertEqual(unobservable_protocols(edges, {RABBIT}), {})


class CoverageTests(unittest.TestCase):
    blind = {PAYMENTS: ["amqp"]}

    def test_unobservable_service_is_never_silent(self) -> None:
        # POLICY allows shop/payments as a silent service; that must not apply.
        coverage = live_coverage(POLICY, [_observed(FRONT, ORDER)], {FRONT, ORDER, PAYMENTS}, NOW, self.blind)
        self.assertEqual(coverage.unobservable_services, {PAYMENTS: ["amqp"]})
        self.assertEqual(coverage.silent_services, [])
        self.assertEqual(coverage.uncovered_services, [])
        state, why = live_runtime_state(POLICY, coverage, NOW)
        self.assertIs(state, RuntimeEvidenceState.LIVE_UNOBSERVABLE)
        self.assertIn(f"{PAYMENTS} (amqp)", why)
        self.assertIn("exemptions.unobservable", why)
        issue = runtime_evidence_issue(POLICY, state, why)
        assert issue is not None
        self.assertIs(issue.code, ReasonCode.RUNTIME_UNOBSERVABLE)
        self.assertTrue(issue.code.limits_actionability)

    def test_protocol_exemption_accepts_the_blind_spot(self) -> None:
        policy = _policy(
            "  unobservable:\n"
            "    - protocol: amqp\n"
            "      reason: consumers checked by hand\n"
            "      expires_on: 2026-12-31\n"
        )
        coverage = live_coverage(policy, [_observed(FRONT, ORDER)], {FRONT, ORDER, PAYMENTS}, NOW, self.blind)  # type: ignore[arg-type]
        self.assertEqual(coverage.accepted_unobservable, {PAYMENTS: ["amqp"]})
        self.assertEqual(live_runtime_state(policy, coverage, NOW)[0], RuntimeEvidenceState.LIVE)  # type: ignore[arg-type]

    def test_expired_protocol_exemption_is_ignored_and_reported(self) -> None:
        policy = _policy(
            "  unobservable:\n"
            "    - protocol: amqp\n"
            "      reason: old\n"
            "      expires_on: 2026-01-01\n"
        )
        coverage = live_coverage(policy, [_observed(FRONT, ORDER)], {FRONT, ORDER, PAYMENTS}, NOW, self.blind)  # type: ignore[arg-type]
        self.assertEqual(coverage.unobservable_services, {PAYMENTS: ["amqp"]})
        self.assertIn("unobservable protocol amqp", coverage.expired_exemptions)

    def test_uncovered_takes_precedence_and_mentions_blind_spots(self) -> None:
        cart = f"{URN}:cart"  # quiet, observable, no allowance: uncovered
        radius = {FRONT, ORDER, cart, RABBIT}
        coverage = live_coverage(POLICY, [_observed(FRONT, ORDER)], radius, NOW, {RABBIT: ["amqp"]})
        state, why = live_runtime_state(POLICY, coverage, NOW)
        self.assertIs(state, RuntimeEvidenceState.LIVE_UNCOVERED)
        self.assertIn(cart, why)
        self.assertIn(f"{RABBIT} (amqp)", why)

    def test_policy_lists_each_protocol_once(self) -> None:
        entry = "    - protocol: amqp\n      reason: r\n      expires_on: 2026-12-31\n"
        with self.assertRaises(PolicyInvalidError):
            _policy("  unobservable:\n" + entry + entry)
        with self.assertRaises(PolicyInvalidError):
            _policy("  unobservable:\n    - protocol: AMQP!\n      reason: r\n      expires_on: 2026-12-31\n")
        self.assertEqual(date(2026, 12, 31), _policy("  unobservable:\n" + entry).exemptions.unobservable[0].expires_on)  # type: ignore[attr-defined]


class ReconcileProtocolTests(unittest.TestCase):
    def test_static_and_env_edges_carry_their_protocol(self) -> None:
        source = _static("payments", "shop", "payments", namespace="shop")
        graph = _reconcile(
            [source, _k8s("shop", "eventbus"), _k8s("shop", "orders")],
            static_edges=[
                _scope_edge(source.service_id, "env:payments/EVENTBUS_URL", DiscoveryMethod.STATIC_CODE),
                _scope_edge(source.service_id, "orders", DiscoveryMethod.STATIC_CODE).model_copy(
                    update={"protocol": "http"}
                ),
            ],
        )
        protocols = {e.target_service_id: e.protocol for e in graph["edges"]}
        self.assertEqual(protocols[f"{SCOPE_URN}:shop:orders"], "http")
        # Without a Kubernetes value the env reference stays unresolved, protocol unknown.
        unresolved = [p for t, p in protocols.items() if t.startswith("unresolved:")]
        self.assertEqual(unresolved, [None])


if __name__ == "__main__":
    unittest.main()
