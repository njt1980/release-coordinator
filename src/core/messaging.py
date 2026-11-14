"""Message channels as typed graph nodes (plan item 4a).

Services that talk through a broker never call each other, so the
dependency edges (caller -> callee) don't connect a producer to its
consumers: both only reach the broker. Channels close that gap. Each queue,
topic, exchange or message type the code declares literally (see
``tools.message_channels``) becomes a ``MessageChannel`` node whose
producers and consumers are derived from the services' uses.

For the release radius a consumer depends on the producers of every channel
it reads: a producer's change reaches its consumers (upstream, as callers
of a changed callee are), and a consumer's change reaches the producers it
relies on (downstream). These channel links are walked by the radius only;
they are never dependency edges, so they don't order deployments (an
asynchronous hop tolerates either order, and event flows are routinely
circular) and live telemetry isn't expected to show them as calls.

A changed service that produces to a channel no scanned service consumes
leaves the radius incomplete: whoever reads it isn't in it. That is
CHANNEL_CONSUMERS_UNKNOWN (blocking) unless the policy declares the
channel's consumers external, or makes the finding advisory
(CHANNEL_CONSUMERS_UNCONFIRMED).
"""

from collections.abc import Iterable, Mapping
from typing import Final

from src.core.issue_keys import channel_key
from src.core.models import (
    ChannelKind,
    ChannelReach,
    ChannelRole,
    EvidenceIssue,
    IssueSubject,
    IssueSubjectKind,
    MessageChannel,
    MessagingCheck,
    ReasonCode,
    ServiceChannelUse,
    UnknownConsumersMode,
    channel_id,
)

# When uses disagree on a channel's kind, the most specific wins: a Service
# Bus sender names a queue or a topic alike, a subscription only a topic.
_KIND_PRIORITY: Final[tuple[ChannelKind, ...]] = (
    ChannelKind.TOPIC,
    ChannelKind.EXCHANGE,
    ChannelKind.QUEUE,
    ChannelKind.MESSAGE_TYPE,
)


def build_channels(uses: Iterable[ServiceChannelUse]) -> list[MessageChannel]:
    """Group uses into channels by broker and name, sorted by channel ID.

    Identical uses (same service, site and role) are kept once.
    """
    grouped: dict[str, list[ServiceChannelUse]] = {}
    for item in uses:
        key = channel_id(item.use.broker, item.use.name)
        bucket = grouped.setdefault(key, [])
        if item not in bucket:
            bucket.append(item)
    channels: list[MessageChannel] = []
    for key in sorted(grouped):
        items = sorted(
            grouped[key],
            key=lambda i: (i.service_id, i.use.role.value, i.use.file_path, i.use.line),
        )
        kinds = {item.use.kind for item in items}
        first = items[0].use
        channels.append(
            MessageChannel(
                channel_id=key,
                broker=first.broker,
                kind=next(kind for kind in _KIND_PRIORITY if kind in kinds),
                name=first.name,
                producers=sorted({i.service_id for i in items if i.use.role is ChannelRole.PRODUCER}),
                consumers=sorted({i.service_id for i in items if i.use.role is ChannelRole.CONSUMER}),
                uses=items,
            )
        )
    return channels


def replace_service_uses(
    channels: Iterable[MessageChannel],
    service_ids: Iterable[str],
    uses: Iterable[ServiceChannelUse],
) -> list[MessageChannel]:
    """Channels with every use of ``service_ids`` replaced by ``uses``
    (a pull request's head read of its changed services)."""
    replaced = set(service_ids)
    kept = [item for channel in channels for item in channel.uses if item.service_id not in replaced]
    return build_channels([*kept, *uses])


def channel_links(channels: Iterable[MessageChannel]) -> list[tuple[str, str, str]]:
    """``(consumer, producer, channel ID)`` for every pair a channel connects;
    a service consuming its own messages is no link."""
    links: list[tuple[str, str, str]] = []
    for channel in channels:
        for consumer in channel.consumers:
            for producer in channel.producers:
                if consumer != producer:
                    links.append((consumer, producer, channel.channel_id))
    return links


def messaging_check(
    channels: Iterable[MessageChannel] | None,
    targets: Iterable[str],
    radius: Iterable[str],
    mode: UnknownConsumersMode = UnknownConsumersMode.BLOCK,
    external_consumers: Mapping[str, str] | None = None,
) -> MessagingCheck | None:
    """The channels the release's services (``targets`` and ``radius``)
    produce to or consume from; None for a baseline predating channels.

    ``external_consumers`` maps channel IDs to the policy's reason their
    consumers live outside the scanned code.
    """
    if channels is None:
        return None
    changed = set(targets)
    members = changed | set(radius)
    accepted = dict(external_consumers or {})
    reached: list[ChannelReach] = []
    for channel in channels:
        if not members.intersection(channel.producers) and not members.intersection(channel.consumers):
            continue
        changed_producers = sorted(changed.intersection(channel.producers))
        unknown = bool(changed_producers) and not channel.consumers
        reached.append(
            ChannelReach(
                channel_id=channel.channel_id,
                broker=channel.broker,
                kind=channel.kind,
                name=channel.name,
                producers=list(channel.producers),
                consumers=list(channel.consumers),
                changed_producers=changed_producers,
                consumers_unknown=unknown,
                accepted=accepted.get(channel.channel_id, "") if unknown else "",
            )
        )
    return MessagingCheck(unknown_consumers=mode, channels=reached)


def _kind_label(reach: ChannelReach) -> str:
    return reach.kind.value.replace("_", " ")


def unknown_consumer_issues(check: MessagingCheck | None) -> list[EvidenceIssue]:
    """CHANNEL_CONSUMERS_UNKNOWN per channel with unknown consumers, in
    block mode only (advisory mode reports ``check.reason_codes``)."""
    if check is None or check.unknown_consumers is not UnknownConsumersMode.BLOCK:
        return []
    return [
        EvidenceIssue(
            code=ReasonCode.CHANNEL_CONSUMERS_UNKNOWN,
            detail=(
                f"{', '.join(reach.changed_producers)} publishes to {reach.broker.value} "
                f"{_kind_label(reach)} {reach.name}, but no scanned service consumes it, so "
                "whoever reads it is missing from the release radius."
            ),
            subjects=[
                *(IssueSubject(kind=IssueSubjectKind.SERVICE, id=s) for s in reach.changed_producers),
                IssueSubject(kind=IssueSubjectKind.ENTITY, id=reach.channel_id),
            ],
            root_key=channel_key(reach.channel_id),
        )
        for reach in check.unknown
    ]
