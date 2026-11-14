"""Capability profiles per telemetry stack for LIVE_UNOBSERVABLE (F2).

Only lab-verified or vendor-documented claims make a protocol observable;
an unverified claim never clears a service. Edges are judged by the
profiles of the services at either end, from their own telemetry.
"""

import unittest

from azure.monitor.query import LogsQueryResult, LogsTable

from src.coordinator.observability import service_capabilities, service_profiles
from src.coordinator.project_policy import live_coverage, live_runtime_state, unobservable_protocols
from src.core.models import DependencyEdge, DiscoveryMethod, RuntimeEvidenceState
from src.core.telemetry_capability import (
    BEYLA,
    PROFILES,
    CapabilityEvidence,
    CollectorSource,
    ProtocolClaim,
    TelemetryCapability,
    TelemetryProfile,
    TelemetryStack,
    combined_capability,
    profiles_for_stacks,
    wire_protocol,
)
from src.tools.telemetry_extractor import build_role_profile_query, parse_role_profile_response
from tests.test_live_evidence import FRESH, NOW, POLICY, _observed
from tests.test_observability_gaps import CONTEXT, GRAPH, _id, _profile

ORDERS, INVENTORY, GATEWAY, FULFILLMENT, MSSQL = (
    _id(n) for n in ("orders", "inventory", "gateway", "fulfillment", "mssql")
)
QUEUE = "unresolved:bus"


def _edge(source: str, target: str, protocol: str | None) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.STATIC_CODE],
        confidence_score=0.7,
        last_observed_at=FRESH,
        protocol=protocol,
    )


class ProfileTests(unittest.TestCase):
    def test_unverified_claims_never_observe(self) -> None:
        sdk = PROFILES[TelemetryProfile.OTEL_SDK]
        self.assertIn("http", sdk.unverified)
        self.assertEqual(sdk.observable, frozenset())
        self.assertFalse(sdk.can_observe("http"))

    def test_every_claim_is_typed_and_sourced(self) -> None:
        for name, profile in PROFILES.items():
            with self.subTest(profile=name):
                self.assertEqual(profile.profile, name.value)
                self.assertTrue(profile.source)
                self.assertTrue(profile.claims)

    def test_lab_facts(self) -> None:
        classic = PROFILES[TelemetryProfile.APPINSIGHTS_CLASSIC]
        distro = PROFILES[TelemetryProfile.AZURE_MONITOR_DISTRO]
        activity = PROFILES[TelemetryProfile.AZURE_MONITOR_DISTRO_ACTIVITY_SOURCE]
        for protocol in ("http", "https", "sqlserver", "servicebus"):
            self.assertTrue(classic.can_observe(protocol), protocol)
        self.assertFalse(distro.can_observe("servicebus"))  # consumer records nothing by default
        self.assertTrue(activity.can_observe("servicebus"))
        for profile in (classic, distro, activity, BEYLA):
            self.assertFalse(profile.can_observe("amqps"), profile.profile)  # RabbitMQ over TLS
        for profile in (classic, distro, activity):
            self.assertIn("grpc", profile.unverified, profile.profile)  # not observed in the lab

    def test_duplicate_claims_are_rejected(self) -> None:
        claim = ProtocolClaim(protocol="http", evidence=CapabilityEvidence.LAB_VERIFIED)
        with self.assertRaises(ValueError):
            TelemetryCapability(profile="x", claims=(claim, claim))

    def test_combined_capability_keeps_the_best_evidence(self) -> None:
        both = combined_capability([TelemetryProfile.OTEL_SDK, TelemetryProfile.AZURE_MONITOR_DISTRO])
        self.assertTrue(both.can_observe("http"))  # Distro: lab, beats the SDK's unverified claim
        self.assertFalse(both.can_observe("redis"))
        self.assertIn("redis", both.unverified)
        self.assertFalse(combined_capability([]).can_observe("http"))

    def test_service_bus_is_its_own_protocol(self) -> None:
        self.assertEqual(wire_protocol("sb://ns.servicebus.windows.net/"), "servicebus")
        self.assertEqual(wire_protocol("amqps://user@ns.servicebus.windows.net:5671/q"), "servicebus")
        self.assertEqual(wire_protocol("amqps://rabbit.example.com:5671"), "amqps")


class StackMappingTests(unittest.TestCase):
    def test_stacks_to_profiles(self) -> None:
        self.assertEqual(
            profiles_for_stacks([TelemetryStack.APPINSIGHTS_CLASSIC, TelemetryStack.UNKNOWN], CollectorSource.OTEL_SDK),
            [TelemetryProfile.APPINSIGHTS_CLASSIC],
        )
        self.assertEqual(
            profiles_for_stacks([TelemetryStack.AZURE_MONITOR_OTEL], CollectorSource.OTEL_SDK, activity_source=True),
            [TelemetryProfile.AZURE_MONITOR_DISTRO_ACTIVITY_SOURCE],
        )
        self.assertEqual(
            profiles_for_stacks([TelemetryStack.OTEL_COLLECTOR], CollectorSource.BEYLA), [TelemetryProfile.BEYLA]
        )
        self.assertEqual(
            profiles_for_stacks([TelemetryStack.OTEL_COLLECTOR], CollectorSource.OTEL_SDK),
            [TelemetryProfile.OTEL_SDK],
        )

    def test_roles_resolve_to_services(self) -> None:
        profiles = [
            _profile("orders"),
            _profile("fulfillment", stacks=[TelemetryStack.AZURE_MONITOR_OTEL], service_bus_activity_rows=37),
            _profile("gateway", stacks=[TelemetryStack.AZURE_MONITOR_OTEL]),
            _profile("unknown-role"),
            _profile("", namespace=""),
        ]
        by_service = service_profiles(GRAPH, CONTEXT, profiles, CollectorSource.OTEL_SDK)
        self.assertEqual(
            by_service,
            {
                FULFILLMENT: [TelemetryProfile.AZURE_MONITOR_DISTRO_ACTIVITY_SOURCE],
                GATEWAY: [TelemetryProfile.AZURE_MONITOR_DISTRO],
                ORDERS: [TelemetryProfile.APPINSIGHTS_CLASSIC],
            },
        )


class PerServiceCoverageTests(unittest.TestCase):
    capabilities = service_capabilities(
        {
            ORDERS: [TelemetryProfile.APPINSIGHTS_CLASSIC],
            GATEWAY: [TelemetryProfile.AZURE_MONITOR_DISTRO],
            FULFILLMENT: [TelemetryProfile.OTEL_SDK],
        }
    )

    def test_either_end_can_see_the_edge(self) -> None:
        edges = [
            _edge(ORDERS, MSSQL, "sqlserver"),  # classic caller sees SQL: mssql observable
            _edge(GATEWAY, QUEUE, "servicebus"),  # Distro without activity source: blind
            _edge(FULFILLMENT, INVENTORY, "http"),  # neither end proves HTTP (SDK unverified)
        ]
        blind = unobservable_protocols(
            edges, {MSSQL, QUEUE, INVENTORY, GATEWAY}, service_capabilities=self.capabilities
        )
        self.assertEqual(blind, {GATEWAY: ["servicebus"], INVENTORY: ["http"], QUEUE: ["servicebus"]})

    def test_without_profiles_the_default_judges(self) -> None:
        edges = [_edge(FULFILLMENT, INVENTORY, "http")]
        self.assertEqual(unobservable_protocols(edges, {INVENTORY}), {})
        self.assertEqual(
            unobservable_protocols(edges, {INVENTORY}, BEYLA, {}), {INVENTORY: ["http"]}
        )

    def test_explanation_names_the_profiles(self) -> None:
        coverage = live_coverage(
            POLICY, [_observed(GATEWAY, ORDERS)], {GATEWAY, ORDERS, QUEUE}, NOW, {QUEUE: ["servicebus"]}
        ).model_copy(
            update={"telemetry_profiles": {QUEUE: ["azure-monitor-distro-dotnet"]}}
        )
        state, why = live_runtime_state(POLICY, coverage, NOW)
        self.assertIs(state, RuntimeEvidenceState.LIVE_UNOBSERVABLE)
        self.assertIn(f"{QUEUE} is instrumented with azure-monitor-distro-dotnet.", why)


class ActivitySourceSignalTests(unittest.TestCase):
    def test_query_counts_activity_source_rows(self) -> None:
        query = build_role_profile_query(1)
        self.assertIn('DependencyType == "Microsoft.ServiceBus"', query)
        self.assertIn('Name startswith "ServiceBusProcessor."', query)

    def test_parse_with_and_without_the_column(self) -> None:
        base = [
            "Role", "RequestRows", "DependencyRows", "Represented", "MaxItemCount",
            "RowsWithNamespace", "Namespace", "SdkVersions", "Components", "LastSeen",
        ]
        row = ["fulfillment", 37, 0, 37, 1, 0, None, '["dotnet10:otel1.12.0:ext1.4.0-d"]', "[]", "2026-11-14T08:21:00Z"]
        for columns, values, expected in [
            (base, row, 0),
            ([*base, "ServiceBusActivityRows"], [*row, 37], 37),
        ]:
            table = LogsTable(
                name="PrimaryResult", columns=columns, columns_types=["string"] * len(columns), rows=[values]
            )
            (profile,) = parse_role_profile_response(LogsQueryResult(tables=[table]))
            self.assertEqual(profile.service_bus_activity_rows, expected)


if __name__ == "__main__":
    unittest.main()
