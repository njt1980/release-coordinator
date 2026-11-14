"""Message channels as typed graph nodes (plan item 4a).

Readers (``tools.message_channels``) find literal produce/consume sites;
``core.messaging`` groups them into channels, links consumers to producers
for the release radius, and questions a changed producer whose channel no
scanned service consumes (blocking by default, advisory by policy).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import BackgroundTasks
from pydantic import ValidationError

from src import webhooks
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.nodes import traverse_blast_radius
from src.coordinator.policy import decide_release
from src.coordinator.sequencing import DeploymentPlan
from src.core.messaging import (
    build_channels,
    channel_links,
    messaging_check,
    replace_service_uses,
    unknown_consumer_issues,
)
from src.core.models import (
    ChannelBroker,
    ChannelKind,
    ChannelRole,
    ChannelUse,
    ContractChange,
    KnowledgeGraph,
    ReasonCode,
    RiskAssessment,
    ServiceChannelUse,
    ServiceNode,
    UnknownConsumersMode,
)
from src.orchestration.briefing import build_briefing
from src.tools.devops_extractor import read_source_file
from src.tools.message_channels import channel_uses, file_constants
from src.tools.policy_store import MessagingPolicy
from tests.benchmarks.discovery.offline_scan import scan_checkout
from tests.fake_devops import FakeDevOps
from tests.test_identity_scope import URN, _k8s, _reconcile, _static
from tests.test_pr_webhook import BASE, HEAD, POLICY_YAML, BaselineFixture, _id, _payload

Role = ChannelRole


def _found(content: str, path: str) -> list[tuple[str, str, str, str]]:
    return [(u.broker.value, u.kind.value, u.name, u.role.value) for u in channel_uses(content, path)]


def _use(
    name: str,
    role: ChannelRole,
    broker: ChannelBroker = ChannelBroker.EVENT_BUS,
    kind: ChannelKind = ChannelKind.MESSAGE_TYPE,
) -> ChannelUse:
    return ChannelUse(broker=broker, kind=kind, name=name, role=role, file_path="a.cs", line=1, via="test")


def _on(service: str, use: ChannelUse) -> ServiceChannelUse:
    return ServiceChannelUse(service_id=service, use=use)


class DotnetReaderTests(unittest.TestCase):
    def test_eshop_event_bus(self) -> None:
        content = (
            "builder.AddRabbitMqEventBus(\"eventbus\")\n"
            "    .AddSubscription<OrderStartedIntegrationEvent, OrderStartedIntegrationEventHandler>();\n"
            "public class H : IIntegrationEventHandler<Events.OrderPaidIntegrationEvent> { }\n"
            "var e = new ProductPriceChangedIntegrationEvent(1, 2m, 3m);\n"
            "// var old = new RetiredIntegrationEvent(1);\n"
            "public static IEventBusBuilder AddSubscription<T, TH>(this IEventBusBuilder b) => b;\n"
        )
        self.assertEqual(
            _found(content, "/src/Basket.API/Extensions.cs"),
            [
                ("eventbus", "message_type", "OrderStartedIntegrationEvent", "consumer"),
                ("eventbus", "message_type", "OrderPaidIntegrationEvent", "consumer"),
                ("eventbus", "message_type", "ProductPriceChangedIntegrationEvent", "producer"),
            ],
        )

    def test_masstransit_needs_its_types_in_the_file(self) -> None:
        content = (
            "public class C : IConsumer<OrderSubmitted> { }\n"
            "await publishEndpoint.Publish<OrderAccepted>(new { Id = 1 });\n"
            "await bus.Publish(new OrderShipped(id));\n"
        )
        self.assertEqual(_found(content, "Orders/Consumer.cs"), [])  # MediatR looks the same
        found = _found("using MassTransit;\n" + content, "Orders/Consumer.cs")
        self.assertEqual(
            found,
            [
                ("masstransit", "message_type", "OrderSubmitted", "consumer"),
                ("masstransit", "message_type", "OrderAccepted", "producer"),
                ("masstransit", "message_type", "OrderShipped", "producer"),
            ],
        )

    def test_service_bus_queues_topics_and_options(self) -> None:
        content = (
            "var client = new ServiceBusClient(cs);\n"
            "var sender = client.CreateSender(\"orders\");\n"
            "var p1 = client.CreateProcessor(\"payments\", new ServiceBusProcessorOptions());\n"
            "var p2 = client.CreateProcessor(\"events\", \"audit\");\n"
            "var p3 = client.CreateProcessor(queueName, options);\n"
            "var r = client.CreateReceiver(topicName: \"alerts\", subscriptionName: \"ops\");\n"
        )
        self.assertEqual(
            _found(content, "Worker.cs"),
            [
                ("servicebus", "queue", "orders", "producer"),
                ("servicebus", "queue", "payments", "consumer"),
                ("servicebus", "topic", "events", "consumer"),
                ("servicebus", "topic", "alerts", "consumer"),
            ],
        )

    def test_rabbitmq_client(self) -> None:
        content = (
            "channel.BasicPublish(exchange: \"\", routingKey: \"tasks\", body: body);\n"
            "await channel.BasicPublishAsync(\"orders-exchange\", \"created\", body);\n"
            "channel.BasicConsume(queue: \"tasks\", autoAck: true, consumer: c);\n"
            "channel.QueueBind(\"my-queue\", \"orders-exchange\", \"created\");\n"
            "channel.BasicPublish(exchange: string.Empty, routingKey: $\"{name}\", body: body);\n"
        )
        # One file producing to and consuming the same channel is bus plumbing.
        self.assertEqual(_found(content, "Rabbit.cs"), [])
        publisher = (
            "channel.BasicPublish(exchange: \"\", routingKey: \"tasks\", body: body);\n"
            "await channel.BasicPublishAsync(\"orders-exchange\", \"created\", body);\n"
        )
        self.assertEqual(
            _found(publisher, "Publisher.cs"),
            [("rabbitmq", "queue", "tasks", "producer"), ("rabbitmq", "exchange", "orders-exchange", "producer")],
        )
        consumer = (
            "channel.BasicConsume(queue: \"tasks\", autoAck: true, consumer: c);\n"
            "channel.QueueBind(\"my-queue\", \"orders-exchange\", \"created\");\n"
        )
        self.assertEqual(
            _found(consumer, "Consumer.cs"),
            [("rabbitmq", "queue", "tasks", "consumer"), ("rabbitmq", "exchange", "orders-exchange", "consumer")],
        )

    def test_constants_and_the_event_bus_plumbing_rule(self) -> None:
        bus = (
            "private const string ExchangeName = \"eshop_event_bus\";\n"
            "await channel.BasicPublishAsync(exchange: ExchangeName, routingKey: routingKey, body: body);\n"
            "await channel.QueueBindAsync(queue: _queueName, exchange: ExchangeName, routingKey: name);\n"
        )
        self.assertEqual(file_constants(bus), {"ExchangeName": "eshop_event_bus"})
        self.assertEqual(_found(bus, "EventBusRabbitMQ/RabbitMQEventBus.cs"), [])
        self.assertEqual(file_constants('string a = "x"; string a = "y";'), {})


class JvmReaderTests(unittest.TestCase):
    def test_spring_amqp_and_kafka(self) -> None:
        content = (
            "@RabbitListener(queues = {\"orders\", \"refunds\"})\n"
            "public void on(Order o) {}\n"
            "@RabbitListener(bindings = @QueueBinding(value = @Queue(\"q\"), exchange = @Exchange(value = \"shop\")))\n"
            "@RabbitListener(queues = \"${queue.name}\")\n"
            "@KafkaListener(topics = \"payments\", groupId = \"g\")\n"
            "private RabbitTemplate rabbitTemplate;\n"
            "private KafkaTemplate<String, String> kafkaTemplate;\n"
            "rabbitTemplate.convertAndSend(\"shipping-task\", shipment);\n"
            "rabbitTemplate.convertAndSend(\"audit-exchange\", \"key\", event);\n"
            "kafkaTemplate.send(\"invoices\", key, value);\n"
        )
        self.assertEqual(
            _found(content, "src/main/java/Shop.java"),
            [
                ("rabbitmq", "queue", "orders", "consumer"),
                ("rabbitmq", "queue", "refunds", "consumer"),
                ("rabbitmq", "exchange", "shop", "consumer"),
                ("kafka", "topic", "payments", "consumer"),
                ("rabbitmq", "queue", "shipping-task", "producer"),
                ("rabbitmq", "exchange", "audit-exchange", "producer"),
                ("kafka", "topic", "invoices", "producer"),
            ],
        )

    def test_listener_container_with_a_field_constant(self) -> None:
        content = (
            "protected final String queueName = \"shipping-task\";\n"
            "SimpleMessageListenerContainer container = new SimpleMessageListenerContainer();\n"
            "container.setQueueNames(this.queueName);\n"
        )
        self.assertEqual(
            _found(content, "QueueMaster.java"), [("rabbitmq", "queue", "shipping-task", "consumer")]
        )

    def test_spring_cloud_stream_bindings(self) -> None:
        yaml_text = (
            "spring:\n  cloud:\n    stream:\n"
            "      bindings:\n"
            "        process-in-0:\n          destination: orders,returns\n"
            "        process-out-0:\n          destination: invoices\n"
            "        other:\n          destination: ignored\n"
            "      kafka:\n        binder:\n          brokers: kafka:9092\n"
        )
        self.assertEqual(
            _found(yaml_text, "src/main/resources/application.yml"),
            [
                ("kafka", "topic", "orders", "consumer"),
                ("kafka", "topic", "returns", "consumer"),
                ("kafka", "topic", "invoices", "producer"),
            ],
        )
        properties = "spring.cloud.stream.bindings.output.destination=events\n"
        self.assertEqual(
            _found(properties, "application-prod.properties"),
            [("spring-cloud-stream", "topic", "events", "producer")],
        )
        self.assertEqual(_found("spring: [", "application.yml"), [])
        self.assertEqual(_found(properties, "other.properties"), [])


class ChannelModelTests(unittest.TestCase):
    def test_build_channels_derives_roles_and_kind(self) -> None:
        sb = ChannelBroker.SERVICE_BUS
        channels = build_channels(
            [
                _on("a", _use("orders", Role.PRODUCER, sb, ChannelKind.QUEUE)),
                _on("b", _use("orders", Role.CONSUMER, sb, ChannelKind.TOPIC)),
                _on("b", _use("orders", Role.CONSUMER, sb, ChannelKind.TOPIC)),
                _on("c", _use("Paid", Role.PRODUCER)),
            ]
        )
        self.assertEqual([c.channel_id for c in channels], ["channel:eventbus/Paid", "channel:servicebus/orders"])
        orders = channels[1]
        self.assertEqual((orders.kind, orders.producers, orders.consumers), (ChannelKind.TOPIC, ["a"], ["b"]))
        self.assertEqual(len(orders.uses), 2)
        self.assertEqual(channel_links(channels), [("b", "a", "channel:servicebus/orders")])

    def test_head_uses_replace_the_changed_services(self) -> None:
        channels = build_channels([_on("a", _use("Paid", Role.PRODUCER)), _on("b", _use("Paid", Role.CONSUMER))])
        replaced = replace_service_uses(channels, ["a"], [_on("a", _use("Shipped", Role.PRODUCER))])
        self.assertEqual(
            [(c.name, c.producers, c.consumers) for c in replaced],
            [("Paid", [], ["b"]), ("Shipped", ["a"], [])],
        )

    def test_a_service_consuming_its_own_messages_is_no_link(self) -> None:
        channels = build_channels([_on("a", _use("Paid", Role.PRODUCER)), _on("a", _use("Paid", Role.CONSUMER))])
        self.assertEqual(channel_links(channels), [])


def _graph(uses: list[ServiceChannelUse], services: list[str]) -> KnowledgeGraph:
    return KnowledgeGraph(
        services=[ServiceNode(service_id=s, name=s.rsplit(":", 1)[-1]) for s in services],
        edges=[],
        channels=build_channels(uses),
    )


ORDERING, BASKET, WEBHOOKS = f"{URN}:shop:ordering", f"{URN}:shop:basket", f"{URN}:shop:webhooks"


class RadiusTests(unittest.TestCase):
    GRAPH = _graph(
        [
            _on(ORDERING, _use("OrderStarted", Role.PRODUCER)),
            _on(BASKET, _use("OrderStarted", Role.CONSUMER)),
            _on(WEBHOOKS, _use("OrderStarted", Role.CONSUMER)),
        ],
        [ORDERING, BASKET, WEBHOOKS],
    )

    def test_a_producer_change_reaches_its_consumers(self) -> None:
        radius = traverse_blast_radius(self.GRAPH, [ORDERING])
        self.assertEqual(sorted(radius.upstream), [BASKET, WEBHOOKS])
        self.assertEqual(radius.downstream, [])
        self.assertEqual(radius.channels, ["channel:eventbus/OrderStarted"])
        self.assertEqual(radius.matched_edges, [])  # channel links are no dependency edges
        self.assertEqual(radius.unresolved, [])
        self.assertEqual(radius.upstream_hops, {BASKET: 1, WEBHOOKS: 1})

    def test_a_consumer_change_reaches_the_producer(self) -> None:
        radius = traverse_blast_radius(self.GRAPH, [BASKET])
        self.assertEqual(radius.downstream, [ORDERING])
        self.assertEqual(radius.upstream, [])

    def test_a_graph_without_channels_is_unchanged(self) -> None:
        graph = self.GRAPH.model_copy(update={"channels": None})
        self.assertEqual(traverse_blast_radius(graph, [ORDERING]).upstream, [])


class CheckTests(unittest.TestCase):
    GRAPH = _graph(
        [_on(ORDERING, _use("OrderStarted", Role.PRODUCER)), _on(BASKET, _use("OrderPaid", Role.CONSUMER))],
        [ORDERING, BASKET],
    )

    def test_unknown_consumers_block_by_default(self) -> None:
        check = messaging_check(self.GRAPH.channels, [ORDERING], [])
        assert check is not None
        self.assertEqual([c.name for c in check.channels], ["OrderStarted"])
        issues = unknown_consumer_issues(check)
        self.assertEqual([i.code for i in issues], [ReasonCode.CHANNEL_CONSUMERS_UNKNOWN])
        self.assertTrue(ReasonCode.CHANNEL_CONSUMERS_UNKNOWN.limits_actionability)
        self.assertEqual(issues[0].root_key, "channel:eventbus/OrderStarted")
        self.assertEqual(check.reason_codes, [])

    def test_advisory_mode_and_external_consumers(self) -> None:
        check = messaging_check(self.GRAPH.channels, [ORDERING], [], UnknownConsumersMode.ADVISORY)
        assert check is not None
        self.assertEqual(unknown_consumer_issues(check), [])
        self.assertEqual(check.reason_codes, [ReasonCode.CHANNEL_CONSUMERS_UNCONFIRMED])
        self.assertFalse(ReasonCode.CHANNEL_CONSUMERS_UNCONFIRMED.limits_actionability)
        accepted = messaging_check(
            self.GRAPH.channels, [ORDERING], [], external_consumers={"channel:eventbus/OrderStarted": "billing reads it"}
        )
        assert accepted is not None
        self.assertEqual(unknown_consumer_issues(accepted), [])
        self.assertEqual(accepted.channels[0].accepted, "billing reads it")

    def test_a_consumer_without_producer_is_context_only(self) -> None:
        check = messaging_check(self.GRAPH.channels, [BASKET], [])
        assert check is not None
        self.assertEqual(unknown_consumer_issues(check), [])
        self.assertIsNone(messaging_check(None, [BASKET], []))

    def test_advisory_code_keeps_the_release_actionable(self) -> None:
        plan = DeploymentPlan(
            change_type=ContractChange.ADDITIVE, sequence=["a"], services_to_deploy=["a"], compatibility_verified=True
        )
        verdict = decide_release(
            [], plan, RiskAssessment(risk_score=0.1, approved=True, justification="ok"),
            change_codes=[ReasonCode.CHANNEL_CONSUMERS_UNCONFIRMED],
        )
        self.assertFalse(verdict.approved)
        self.assertTrue(verdict.actionable)


class PolicyTests(unittest.TestCase):
    def test_messaging_section(self) -> None:
        policy = MessagingPolicy.model_validate(
            {"unknown_consumers": "advisory", "external_consumers": [{"channel": "servicebus/orders", "reason": "ERP"}]}
        )
        self.assertEqual(policy.external_reasons(), {"channel:servicebus/orders": "ERP"})
        self.assertIs(MessagingPolicy().unknown_consumers, UnknownConsumersMode.BLOCK)
        for bad in (
            {"unknown_consumers": "ignore"},
            {"external_consumers": [{"channel": "nats/orders", "reason": "x"}]},
            {"external_consumers": [{"channel": "orders", "reason": "x"}]},
            {"external_consumers": [{"channel": "kafka/a", "reason": "x"}, {"channel": "kafka/a", "reason": "y"}]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                MessagingPolicy.model_validate(bad)


class DiscoveryTests(unittest.TestCase):
    def test_scan_records_channel_uses_per_service(self) -> None:
        files = {
            "src/Ordering/Dockerfile": "FROM dotnet\n",
            "src/Ordering/Ordering.csproj": "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n",
            "src/Ordering/Order.cs": "var e = new OrderStartedIntegrationEvent(id);\n",
            "src/Basket/Dockerfile": "FROM dotnet\n",
            "src/Basket/Basket.csproj": "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n",
            "src/Basket/Program.cs": "b.AddSubscription<OrderStartedIntegrationEvent, OrderStartedHandler>();\n",
        }
        with tempfile.TemporaryDirectory() as root:
            for path, text in files.items():
                target = Path(root, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            scan = scan_checkout("shop", Path(root), "a" * 40)
        found = {s.service.root: [(u.name, u.role.value, u.file_path) for u in s.channel_uses] for s in scan.services}
        self.assertEqual(
            found,
            {
                "src/Basket": [("OrderStartedIntegrationEvent", "consumer", "src/Basket/Program.cs")],
                "src/Ordering": [("OrderStartedIntegrationEvent", "producer", "src/Ordering/Order.cs")],
            },
        )
        self.assertTrue(all(not s.references for s in scan.services))  # never dependency references

    def test_source_file_reads_carry_channels(self) -> None:
        read = read_source_file("@KafkaListener(topics = \"t\")\n", "svc", "/src/main/java/A.java")
        self.assertEqual([(u.name, u.role) for u in read.channels], [("t", Role.CONSUMER)])

    def test_reconcile_resolves_participants_to_merged_services(self) -> None:
        static = _static("ordering", "shop", "src/Ordering")
        graph = _reconcile_with(
            [static, _static("basket", "shop", "src/Basket"), _k8s("shop", "ordering"), _k8s("shop", "basket")],
            [
                _on(static.service_id, _use("OrderStarted", Role.PRODUCER)),
                _on(f"{URN}:unknown:basket", _use("OrderStarted", Role.CONSUMER)),
            ],
        )
        (channel,) = graph["channels"]
        self.assertEqual(channel.producers, [f"{URN}:shop:ordering"])
        self.assertEqual(channel.consumers, [f"{URN}:shop:basket"])
        self.assertEqual(graph["edges"], [])


def _reconcile_with(services: list[ServiceNode], uses: list[ServiceChannelUse]) -> dict[str, Any]:
    from src.discovery_engine import nodes as discovery_nodes

    state: Any = {
        "tenant": "acme",
        "environment": "prod",
        "cluster": "aks-1",
        "target_namespaces": ["shop"],
        "discovered_services": services,
        "static_edges": [],
        "runtime_edges": [],
        "channel_uses": uses,
    }
    graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
    assert isinstance(graph, dict)
    return graph


ORDER_EVENT = ChannelUse(
    broker=ChannelBroker.EVENT_BUS,
    kind=ChannelKind.MESSAGE_TYPE,
    name="OrderCreatedIntegrationEvent",
    role=Role.PRODUCER,
    file_path="src/order-service/events.cs",
    line=3,
    via="new integration event",
)


class EndToEndTests(BaselineFixture):
    """The coordinator on a pull request changing order-service, which
    publishes an integration event."""

    CHANGED = "src/order-service/events.cs"

    def _graph(self, scan: Any = None) -> KnowledgeGraph:
        graph = super()._graph() if scan is None else super()._graph(scan)
        uses = [ServiceChannelUse(service_id=_id("order-service"), use=ORDER_EVENT)]
        if self.consumer:
            uses.append(
                ServiceChannelUse(
                    service_id=_id("payments-api"),
                    use=ORDER_EVENT.model_copy(update={"role": Role.CONSUMER, "file_path": "services/payments/api/h.cs"}),
                )
            )
        return graph.model_copy(update={"channels": build_channels(uses)})

    def _run(self, consumer: bool, policy_extra: str = "") -> Any:
        self.consumer = consumer
        if policy_extra:
            self._write_policy(POLICY_YAML + policy_extra)
        self._write_baseline()
        payload = _payload(changed_files=[self.CHANGED], change_type="additive")
        tasks = BackgroundTasks()
        webhooks.pr_assessment_webhook(payload, tasks)
        devops = FakeDevOps(
            history=[BASE, HEAD],
            changes={(BASE, HEAD): [self.CHANGED]},
            head_channels={"src/order-service": [ORDER_EVENT]},
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

    def test_the_event_reaches_its_consumer(self) -> None:
        assessment = self._run(consumer=True)
        self.assertIn(_id("payments-api"), assessment.deployment_sequence)
        self.assertIn(_id("payments-api"), [s for tier in assessment.evidence.test_scope for s in tier.services])
        self.assertNotIn(ReasonCode.CHANNEL_CONSUMERS_UNKNOWN, assessment.reason_codes)
        messaging = assessment.evidence.messaging
        assert messaging is not None
        self.assertEqual([c.consumers for c in messaging.channels], [[_id("payments-api")]])
        self.assertIn("info:channels", [g.root_key for g in build_briefing(assessment).groups])

    def test_unknown_consumers_block(self) -> None:
        assessment = self._run(consumer=False)
        self.assertIn(ReasonCode.CHANNEL_CONSUMERS_UNKNOWN, assessment.reason_codes)
        # An evidence issue: the release isn't judged and the model isn't consulted.
        self.assertIn(ReasonCode.CHANNEL_CONSUMERS_UNKNOWN, [i.code for i in assessment.issues])
        self.assertIsNone(assessment.advisory_assessment)
        group = next(g for g in build_briefing(assessment).groups if g.root_key.startswith("channel:"))
        self.assertIn("external_consumers", group.policy_draft)

    def test_policy_can_make_it_advisory(self) -> None:
        assessment = self._run(consumer=False, policy_extra="messaging:\n  unknown_consumers: advisory\n")
        self.assertNotIn(ReasonCode.CHANNEL_CONSUMERS_UNKNOWN, assessment.reason_codes)
        self.assertIn(ReasonCode.CHANNEL_CONSUMERS_UNCONFIRMED, assessment.reason_codes)
        self.assertFalse(assessment.approved)
        # Not an evidence issue: the release is judged (the model is consulted).
        self.assertEqual(assessment.issues, [])
        self.assertIsNotNone(assessment.advisory_assessment)
        briefing = build_briefing(assessment)
        self.assertIn("decision:channels", [g.root_key for g in briefing.groups])

    def test_external_consumers_are_accepted(self) -> None:
        assessment = self._run(
            consumer=False,
            policy_extra=(
                "messaging:\n  external_consumers:\n"
                "    - channel: eventbus/OrderCreatedIntegrationEvent\n      reason: Billing project reads it\n"
            ),
        )
        self.assertNotIn(ReasonCode.CHANNEL_CONSUMERS_UNKNOWN, assessment.reason_codes)
        self.assertIn(
            "messaging.external_consumers",
            [e.rule for e in assessment.evidence.exempted_dependencies],
        )


if __name__ == "__main__":
    unittest.main()
