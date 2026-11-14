"""List a broker's queues and topics with read-only management APIs.

Azure Service Bus only: for each namespace the project policy names
(``messaging.service_bus_namespaces``), the Azure Resource Manager REST API
lists its queues, its topics and each topic's subscriptions (GET only;
the identity needs Reader on the namespace). The requests go through
``azure-core``'s pipeline with ``azure-identity``'s credential, both already
installed (``azure-identity`` depends on ``azure-core``); no Service Bus
management SDK is added.

The result is advisory context for the channels the code declares
(``core.messaging``): it never adds a producer, a consumer or an edge, and
a failed read is recorded, never raised.
"""

from __future__ import annotations

import logging
from urllib.parse import quote
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Final

from src.core.models import BrokerEntity, BrokerInventory, ChannelBroker, ChannelKind, ServiceBusNamespace


logger = logging.getLogger(__name__)

ARM_ENDPOINT: Final[str] = "https://management.azure.com"
ARM_SCOPE: Final[str] = "https://management.azure.com/.default"
SERVICE_BUS_API_VERSION: Final[str] = "2021-11-01"
# At most this many pages per list (ARM pages hold up to 100 entities).
_MAX_PAGES: Final[int] = 50

# GETs an absolute ARM URL and returns its JSON body; raises on failure.
JsonFetcher = Callable[[str], Mapping[str, Any]]


def namespace_url(namespace: ServiceBusNamespace) -> str:
    return (
        f"{ARM_ENDPOINT}/subscriptions/{namespace.subscription_id}"
        f"/resourceGroups/{namespace.resource_group}"
        f"/providers/Microsoft.ServiceBus/namespaces/{namespace.namespace}"
    )


def _list(fetch: JsonFetcher, url: str) -> Iterator[Mapping[str, Any]]:
    """Every item of an ARM list, following ``nextLink`` (bounded)."""
    next_url: str | None = url
    for _ in range(_MAX_PAGES):
        if not next_url:
            return
        body = fetch(next_url)
        for item in body.get("value") or []:
            if isinstance(item, Mapping):
                yield item
        link = body.get("nextLink")
        next_url = str(link) if isinstance(link, str) and link.startswith(ARM_ENDPOINT + "/") else None
    if next_url:
        raise RuntimeError(f"more than {_MAX_PAGES} pages at {url}")


def _names(items: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({str(item["name"]).strip() for item in items if str(item.get("name") or "").strip()})


def read_service_bus(namespace: ServiceBusNamespace, fetch: JsonFetcher) -> list[BrokerEntity]:
    """The namespace's queues, and its topics with their subscriptions.

    Raises whatever ``fetch`` raises.
    """
    base = namespace_url(namespace)
    version = f"api-version={SERVICE_BUS_API_VERSION}"
    entities = [
        BrokerEntity(
            broker=ChannelBroker.SERVICE_BUS, kind=ChannelKind.QUEUE, name=name, namespace=namespace.namespace
        )
        for name in _names(_list(fetch, f"{base}/queues?{version}"))
    ]
    for topic in _names(_list(fetch, f"{base}/topics?{version}")):
        subscriptions = _names(_list(fetch, f"{base}/topics/{quote(topic, safe='')}/subscriptions?{version}"))
        entities.append(
            BrokerEntity(
                broker=ChannelBroker.SERVICE_BUS, kind=ChannelKind.TOPIC, name=topic,
                namespace=namespace.namespace, subscriptions=subscriptions,
            )
        )
    return entities


def read_broker_inventory(
    namespaces: Iterable[ServiceBusNamespace], fetch: JsonFetcher | None = None
) -> BrokerInventory:
    """Every namespace's entities; a namespace that can't be read is an error
    entry. ``fetch`` defaults to an ARM client with ``DefaultAzureCredential``."""
    wanted = list(namespaces)
    inventory = BrokerInventory()
    if not wanted:
        return inventory
    if fetch is None:
        try:
            fetch = arm_fetcher()
        except Exception as error:  # credential or client setup
            inventory.errors.append(f"Azure Resource Manager client unavailable: {error}")
            return inventory
    for namespace in wanted:
        try:
            inventory.entities.extend(read_service_bus(namespace, fetch))
        except Exception as error:
            logger.warning("Cannot list Service Bus namespace %s: %s", namespace.namespace, error)
            inventory.errors.append(f"servicebus/{namespace.namespace}: {error}")
    inventory.entities.sort(key=lambda e: (e.broker.value, e.namespace, e.kind.value, e.name.lower()))
    return inventory


def arm_fetcher() -> JsonFetcher:
    """A read-only ARM GET through azure-core's pipeline (bearer token, retries)."""
    from azure.core import PipelineClient
    from azure.core.pipeline.policies import BearerTokenCredentialPolicy, RetryPolicy
    from azure.core.rest import HttpRequest
    from azure.identity import DefaultAzureCredential

    client: PipelineClient[Any, Any] = PipelineClient(
        base_url=ARM_ENDPOINT,
        policies=[BearerTokenCredentialPolicy(DefaultAzureCredential(), ARM_SCOPE), RetryPolicy()],
    )

    def fetch(url: str) -> Mapping[str, Any]:
        if not url.startswith(ARM_ENDPOINT + "/"):
            raise ValueError(f"not an Azure Resource Manager URL: {url}")
        response = client.send_request(HttpRequest("GET", url))
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, Mapping):
            raise ValueError("response is not a JSON object")
        return body

    return fetch
