"""Tests for deterministic topological deployment sequencing."""

import random
import unittest
from datetime import datetime, timezone

from src.coordinator.sequencing import DeploymentPlan, sequence_deployment
from src.core.models import ContractChange, DependencyEdge, DiscoveryMethod

URN = "urn:release-coord:acme:prod:aks-1:shop"
TARGET = f"{URN}:order-service"
ADDITIVE = ContractChange.ADDITIVE
BREAKING = ContractChange.BREAKING


def _id(name: str) -> str:
    return f"{URN}:{name}"


def _edge(caller: str, callee: str) -> DependencyEdge:
    """``caller -> callee``: caller depends on callee."""
    return DependencyEdge(
        source_service_id=caller,
        target_service_id=callee,
        discovery_methods=[DiscoveryMethod.STATIC_CODE],
        confidence_score=0.7,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# store-front -> api-gateway -> order-service -> rabbitmq -> storage, plus
# store-front -> order-service directly and admin -> order-service.
UPSTREAM = [_id("api-gateway"), _id("store-front"), _id("admin")]
DOWNSTREAM = [_id("rabbitmq"), _id("storage")]
EDGES = [
    _edge(_id("store-front"), _id("api-gateway")),
    _edge(_id("api-gateway"), TARGET),
    _edge(_id("store-front"), TARGET),
    _edge(_id("admin"), TARGET),
    _edge(TARGET, _id("rabbitmq")),
    _edge(_id("rabbitmq"), _id("storage")),
]


def _assert_respects_edges(
    case: unittest.TestCase, plan: DeploymentPlan, edges: list[DependencyEdge]
) -> None:
    position = {service: index for index, service in enumerate(plan.sequence)}
    for edge in edges:
        caller, callee = edge.source_service_id, edge.target_service_id
        if caller not in position or callee not in position or caller == callee:
            continue
        if plan.change_type is ADDITIVE:
            case.assertLess(position[callee], position[caller], f"{callee} before {caller}")
        else:
            case.assertLess(position[caller], position[callee], f"{caller} before {callee}")


class SequenceDeploymentTests(unittest.TestCase):
    def test_additive_deploys_dependencies_then_target_then_consumers(self) -> None:
        plan = sequence_deployment([TARGET], UPSTREAM, DOWNSTREAM, EDGES, ADDITIVE)

        self.assertEqual(
            plan.sequence,
            [
                _id("storage"),
                _id("rabbitmq"),
                TARGET,
                _id("admin"),
                _id("api-gateway"),
                _id("store-front"),
            ],
        )
        self.assertTrue(plan.is_acyclic)
        _assert_respects_edges(self, plan, EDGES)

    def test_breaking_deploys_consumers_then_target_then_dependencies(self) -> None:
        plan = sequence_deployment([TARGET], UPSTREAM, DOWNSTREAM, EDGES, BREAKING)

        self.assertEqual(
            plan.sequence,
            [
                _id("admin"),
                _id("store-front"),
                _id("api-gateway"),
                TARGET,
                _id("rabbitmq"),
                _id("storage"),
            ],
        )
        self.assertTrue(plan.is_acyclic)
        _assert_respects_edges(self, plan, EDGES)

    def test_every_upstream_and_downstream_is_on_the_right_side_of_target(self) -> None:
        for change_type, before, after in [
            (ADDITIVE, DOWNSTREAM, UPSTREAM),
            (BREAKING, UPSTREAM, DOWNSTREAM),
        ]:
            with self.subTest(change_type=change_type):
                sequence = sequence_deployment(
                    [TARGET], UPSTREAM, DOWNSTREAM, EDGES, change_type
                ).sequence
                target_at = sequence.index(TARGET)
                self.assertTrue(all(sequence.index(s) < target_at for s in before))
                self.assertTrue(all(sequence.index(s) > target_at for s in after))

    def test_sequence_is_independent_of_input_order(self) -> None:
        expected = sequence_deployment([TARGET], UPSTREAM, DOWNSTREAM, EDGES, ADDITIVE)
        shuffler = random.Random(7)
        for _ in range(20):
            upstream, downstream, edges = list(UPSTREAM), list(DOWNSTREAM), list(EDGES)
            for items in (upstream, downstream, edges):
                shuffler.shuffle(items)
            plan = sequence_deployment([TARGET], upstream, downstream, edges, ADDITIVE)
            self.assertEqual(plan, expected)

    def test_unconstrained_services_are_grouped_by_phase(self) -> None:
        # Two unrelated targets: store-front calls one, the other calls ledger.
        other = _id("billing")
        edges = [_edge(_id("store-front"), TARGET), _edge(other, _id("ledger"))]
        upstream, downstream = [_id("store-front")], [_id("ledger")]

        additive = sequence_deployment([TARGET, other], upstream, downstream, edges, ADDITIVE)
        breaking = sequence_deployment([TARGET, other], upstream, downstream, edges, BREAKING)

        self.assertEqual(
            additive.sequence, [_id("ledger"), other, TARGET, _id("store-front")]
        )
        self.assertEqual(
            breaking.sequence, [_id("store-front"), other, TARGET, _id("ledger")]
        )

    def test_edges_between_targets_are_ordered(self) -> None:
        # Both changed: order-service calls inventory, so for an additive
        # change inventory goes first; for a breaking one, order-service.
        inventory = _id("inventory")
        edges = [_edge(TARGET, inventory)]

        additive = sequence_deployment([TARGET, inventory], [], [], edges, ADDITIVE)
        breaking = sequence_deployment([TARGET, inventory], [], [], edges, BREAKING)

        self.assertEqual(additive.sequence, [inventory, TARGET])
        self.assertEqual(breaking.sequence, [TARGET, inventory])

    def test_edges_outside_subgraph_self_calls_and_duplicates_are_ignored(self) -> None:
        edges = [
            _edge(TARGET, "unresolved:vault.corp.internal"),
            _edge(TARGET, TARGET),
            _edge(_id("store-front"), TARGET),
            _edge(_id("store-front"), TARGET),
            _edge(_id("unrelated"), _id("store-front")),
        ]

        plan = sequence_deployment([TARGET], [_id("store-front")], [], edges, ADDITIVE)

        self.assertEqual(plan.sequence, [TARGET, _id("store-front")])
        self.assertTrue(plan.is_acyclic)

    def test_cycle_is_reported_and_sequence_still_covers_every_service(self) -> None:
        # order-service -> rabbitmq -> order-service (cycle); store-front
        # calls order-service and is blocked behind it.
        edges = [
            _edge(TARGET, _id("rabbitmq")),
            _edge(_id("rabbitmq"), TARGET),
            _edge(_id("store-front"), TARGET),
        ]
        upstream = [_id("rabbitmq"), _id("store-front")]
        downstream = [_id("rabbitmq")]

        plan = sequence_deployment([TARGET], upstream, downstream, edges, ADDITIVE)

        self.assertFalse(plan.is_acyclic)
        self.assertEqual(
            plan.cyclic_services, sorted([TARGET, _id("rabbitmq"), _id("store-front")])
        )
        self.assertCountEqual(plan.sequence, [TARGET, _id("rabbitmq"), _id("store-front")])
        self.assertEqual(plan.sequence[-1], _id("store-front"))
        self.assertEqual(
            plan, sequence_deployment([TARGET], upstream, downstream, edges, ADDITIVE)
        )

    def test_additive_plan_deploys_only_targets_and_has_no_stale_consumers(self) -> None:
        plan = sequence_deployment([TARGET], UPSTREAM, DOWNSTREAM, EDGES, ADDITIVE)

        self.assertEqual(plan.services_to_deploy, [TARGET])
        self.assertEqual(
            plan.unchanged_services, [s for s in plan.sequence if s != TARGET]
        )
        self.assertEqual(plan.stale_consumers, [])

    def test_breaking_plan_flags_unchanged_consumers_in_sequence_order(self) -> None:
        plan = sequence_deployment([TARGET], UPSTREAM, DOWNSTREAM, EDGES, BREAKING)

        self.assertEqual(plan.services_to_deploy, [TARGET])
        self.assertEqual(
            plan.stale_consumers,
            [_id("admin"), _id("store-front"), _id("api-gateway")],
        )
        self.assertNotIn(_id("rabbitmq"), plan.stale_consumers)

    def test_changed_consumer_is_deployed_not_stale(self) -> None:
        # store-front is changed in the same PR, so it is a target.
        targets = [TARGET, _id("store-front")]
        upstream = [_id("api-gateway"), _id("store-front"), _id("admin")]

        plan = sequence_deployment(targets, upstream, DOWNSTREAM, EDGES, BREAKING)

        self.assertEqual(plan.services_to_deploy, [_id("store-front"), TARGET])
        self.assertEqual(plan.stale_consumers, [_id("admin"), _id("api-gateway")])

    def test_lone_target_and_missing_target(self) -> None:
        plan = sequence_deployment([TARGET], [], [], [], BREAKING)
        self.assertEqual(plan.sequence, [TARGET])
        with self.assertRaises(ValueError):
            sequence_deployment([], [], [], [], ADDITIVE)


if __name__ == "__main__":
    unittest.main()
