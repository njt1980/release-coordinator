"""Tests for read-only broker introspection (Service Bus via ARM) and how it
feeds message channels: advisory context only, never edges or consumers."""

import unittest
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from src.core.messaging import broker_only_entities, unknown_consumer_issues
from src.core.models import (
    BrokerEntity,
    BrokerInventory,
    ChannelBroker,
    ChannelKind,
    ChannelReach,
    MessageChannel,
    MessagingCheck,
    ServiceBusNamespace,
    UnknownConsumersMode,
    channel_id,
)
from src.tools.broker_inventory import namespace_url, read_broker_inventory
from src.tools.policy_store import MessagingPolicy

NS = ServiceBusNamespace(
    subscription_id="00000000-0000-0000-0000-000000000001", resource_group="rg-lab", namespace="shopbus01"
)
BASE = namespace_url(NS)
V = "api-version=2021-11-01"


def _fetcher(pages: dict[str, Mapping[str, Any]]) -> Any:
    asked: list[str] = []

    def fetch(url: str) -> Mapping[str, Any]:
        asked.append(url)
        if url not in pages:
            raise RuntimeError(f"403 for {url}")
        return pages[url]

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


class ReadTests(unittest.TestCase):
    def test_queues_topics_subscriptions_and_paging(self) -> None:
        fetch = _fetcher({
            f"{BASE}/queues?{V}": {"value": [{"name": "orders"}], "nextLink": f"{BASE}/queues?{V}&skip=1"},
            f"{BASE}/queues?{V}&skip=1": {"value": [{"name": "Payments"}]},
            f"{BASE}/topics?{V}": {"value": [{"name": "order-events"}]},
            f"{BASE}/topics/order-events/subscriptions?{V}": {"value": [{"name": "billing"}, {"name": "audit"}]},
        })
        inventory = read_broker_inventory([NS], fetch)
        self.assertEqual(inventory.errors, [])
        self.assertEqual(
            [(e.kind, e.name, e.subscriptions) for e in inventory.entities],
            [
                (ChannelKind.QUEUE, "orders", []),
                (ChannelKind.QUEUE, "Payments", []),
                (ChannelKind.TOPIC, "order-events", ["audit", "billing"]),
            ],
        )
        self.assertTrue(all(url.startswith("https://management.azure.com/") for url in fetch.asked))

    def test_a_failed_namespace_is_an_error_not_a_raise(self) -> None:
        inventory = read_broker_inventory([NS], _fetcher({}))
        self.assertEqual(inventory.entities, [])
        self.assertEqual(len(inventory.errors), 1)
        self.assertIn("servicebus/shopbus01", inventory.errors[0])

    def test_foreign_next_links_are_not_followed(self) -> None:
        fetch = _fetcher({
            f"{BASE}/queues?{V}": {"value": [{"name": "q1"}], "nextLink": "https://evil.example/steal"},
            f"{BASE}/topics?{V}": {"value": []},
        })
        inventory = read_broker_inventory([NS], fetch)
        self.assertEqual([e.name for e in inventory.entities], ["q1"])
        self.assertNotIn("https://evil.example/steal", fetch.asked)

    def test_policy_validates_the_namespace(self) -> None:
        policy = MessagingPolicy.model_validate(
            {"service_bus_namespaces": [{"subscription_id": NS.subscription_id, "resource_group": "rg-lab",
                                         "namespace": "shopbus01"}]}
        )
        self.assertEqual(policy.service_bus_namespaces, [NS])
        with self.assertRaises(ValidationError):
            ServiceBusNamespace(subscription_id="nope", resource_group="rg", namespace="shopbus01")
        with self.assertRaises(ValidationError):
            ServiceBusNamespace(subscription_id=NS.subscription_id, resource_group="rg", namespace="a/../b")


def _entity(kind: ChannelKind, name: str, subscriptions: list[str] | None = None) -> BrokerEntity:
    return BrokerEntity(broker=ChannelBroker.SERVICE_BUS, kind=kind, name=name, namespace="shopbus01",
                        subscriptions=subscriptions or [])


class ChannelFeedTests(unittest.TestCase):
    INVENTORY = BrokerInventory(entities=[
        _entity(ChannelKind.QUEUE, "Orders"),
        _entity(ChannelKind.TOPIC, "order-events", ["billing"]),
        _entity(ChannelKind.TOPIC, "unused"),
        _entity(ChannelKind.QUEUE, "legacy"),
    ])

    def test_broker_only_entities(self) -> None:
        channels = [MessageChannel(channel_id=channel_id(ChannelBroker.SERVICE_BUS, "orders"),
                                   broker=ChannelBroker.SERVICE_BUS, kind=ChannelKind.QUEUE, name="orders")]
        self.assertEqual([e.name for e in broker_only_entities(channels, self.INVENTORY)],
                         ["order-events", "unused", "legacy"])
        self.assertEqual(broker_only_entities(channels, None), [])

    def _check(self, name: str, kind: ChannelKind = ChannelKind.TOPIC) -> MessagingCheck:
        return MessagingCheck(
            unknown_consumers=UnknownConsumersMode.BLOCK,
            channels=[ChannelReach(
                channel_id=channel_id(ChannelBroker.SERVICE_BUS, name), broker=ChannelBroker.SERVICE_BUS,
                kind=kind, name=name, producers=["p"], consumers=[], changed_producers=["p"],
                consumers_unknown=True,
            )],
        )

    def test_issue_detail_names_broker_subscriptions_but_still_blocks(self) -> None:
        (issue,) = unknown_consumer_issues(self._check("order-events"), self.INVENTORY)
        self.assertIn("subscription(s) billing", issue.detail)
        (issue,) = unknown_consumer_issues(self._check("unused"), self.INVENTORY)
        self.assertIn("no subscription", issue.detail)
        (issue,) = unknown_consumer_issues(self._check("orders", ChannelKind.QUEUE), self.INVENTORY)
        self.assertIn("who reads it isn't recorded there", issue.detail)
        (issue,) = unknown_consumer_issues(self._check("order-events"))
        self.assertNotIn("broker lists", issue.detail)


if __name__ == "__main__":
    unittest.main()
