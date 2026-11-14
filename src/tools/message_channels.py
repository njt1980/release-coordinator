"""Read the message channels a file produces to or consumes from (plan item 4a).

One file in, ``ChannelUse`` records out; no I/O. Only literal channel names
are read: a name computed at runtime (a variable, a ``${placeholder}``, an
interpolated string) is skipped, never guessed, so a channel is never
invented. A producer's channel that no scanned service consumes is what the
coordinator questions (see ``core.messaging``), so producers are read only
from constructs that certainly send.

.NET (``.cs``):

- eShop-style event bus (``IEventBus``): ``AddSubscription<TEvent, THandler>``
  and ``IIntegrationEventHandler<TEvent>`` consume the event type;
  ``new XxxIntegrationEvent(`` produces it (the event is built to be
  published, through the bus or an outbox).
- MassTransit (files naming MassTransit or its publish/send types):
  ``IConsumer<T>`` consumes; ``Publish<T>(``, ``Publish(new T``,
  ``Send<T>(`` and ``Send(new T`` produce.
- Azure Service Bus (files naming ServiceBus): ``CreateSender("q")``
  produces; ``CreateProcessor``/``CreateReceiver`` (and their session
  variants) consume a queue, or a topic when a subscription is named.
- RabbitMQ.Client: ``BasicPublish[Async](exchange, routingKey)`` produces
  to the exchange, or to the queue the routing key names on the default
  (``""``) exchange; ``BasicConsume[Async](queue)`` consumes the queue and
  ``QueueBind[Async](queue, exchange)`` the exchange.

Java/Kotlin (``.java``, ``.kt``):

- ``@RabbitListener(queues = ...)`` (and ``queuesToDeclare = @Queue(...)``)
  consumes queues; its ``@QueueBinding(exchange = @Exchange(...))`` the
  exchange. ``RabbitTemplate``/``AmqpTemplate.convertAndSend`` produces
  (three arguments: the exchange; two: the queue the routing key names).
- ``@KafkaListener(topics = ...)`` consumes topics; ``KafkaTemplate``
  ``.send("topic", ...)`` produces.

Spring configuration (``application*``/``bootstrap*`` ``.yml``,
``.yaml``, ``.properties``): Spring Cloud Stream
``spring.cloud.stream.bindings.<binding>.destination``; a binding named
``*-in-<n>`` or ``input*`` consumes, ``*-out-<n>`` or ``output*`` produces.
The broker is Kafka or RabbitMQ when the same file configures that binder
(``spring.cloud.stream.kafka.*``/``spring.kafka.*`` or
``spring.cloud.stream.rabbit.*``/``spring.rabbitmq.*``), otherwise
SPRING_CLOUD_STREAM, which matches only other Spring Cloud Stream bindings.
Spring AMQP listener containers' ``setQueueNames``/``addQueueNames``
consume queues.

A name may also be a string constant the same file declares (``const
string``, ``static final String``, a field assigned one literal). A file
that both produces to and consumes one channel is left out: that is a
bus's own publish/subscribe plumbing (eShop's ``RabbitMQEventBus``), not a
service's messaging.
"""

import os
import re
from collections.abc import Iterator
from typing import Final

from src.core.models import ChannelBroker, ChannelKind, ChannelRole, ChannelUse
from src.tools.config_readers import ConfigParseError, spring_documents

_CSHARP: Final[frozenset[str]] = frozenset({".cs"})
_JVM: Final[frozenset[str]] = frozenset({".java", ".kt"})
_SPRING_CONFIG: Final[re.Pattern[str]] = re.compile(
    r"^(?:application|bootstrap)(?:-[\w.-]+)?\.(?:ya?ml|properties)$", re.IGNORECASE
)

# A (possibly qualified, possibly generic-free) type name.
_TYPE: Final[str] = r"(?:[A-Za-z_][\w]*\.)*([A-Z][\w]*)"

# eShop-style event bus.
_ADD_SUBSCRIPTION: Final[re.Pattern[str]] = re.compile(
    rf"\bAddSubscription\s*<\s*{_TYPE}\s*,\s*[\w.]+\s*>"
)
_EVENT_HANDLER: Final[re.Pattern[str]] = re.compile(
    rf"\bIIntegrationEventHandler\s*<\s*{_TYPE}\s*>"
)
_NEW_INTEGRATION_EVENT: Final[re.Pattern[str]] = re.compile(
    r"\bnew\s+(?:[A-Za-z_][\w]*\.)*([A-Z]\w*IntegrationEvent)\s*[({]"
)

# MassTransit.
_MASSTRANSIT_MARKERS: Final[tuple[str, ...]] = (
    "MassTransit", "IPublishEndpoint", "ISendEndpoint", "ConsumeContext", "IBus ",
)
_MT_CONSUMER: Final[re.Pattern[str]] = re.compile(rf"\bIConsumer\s*<\s*{_TYPE}\s*>")
_MT_GENERIC_SEND: Final[re.Pattern[str]] = re.compile(rf"\.(Publish|Send)\s*<\s*{_TYPE}\s*>\s*\(")
_MT_NEW_SEND: Final[re.Pattern[str]] = re.compile(rf"\.(Publish|Send)\s*\(\s*new\s+{_TYPE}\s*[({{]")

# Azure Service Bus.
_SB_CALL: Final[re.Pattern[str]] = re.compile(
    r"\b(CreateSender|CreateProcessor|CreateReceiver|CreateSessionProcessor|CreateSessionReceiver)\s*\("
)

# RabbitMQ.Client.
_RABBIT_CALL: Final[re.Pattern[str]] = re.compile(
    r"\b(BasicPublish|BasicConsume|QueueBind)(?:Async)?\s*(?:<[^>()]*>)?\s*\("
)

# Spring AMQP / Kafka.
_RABBIT_LISTENER: Final[re.Pattern[str]] = re.compile(r"@RabbitListener\s*\(")
_KAFKA_LISTENER: Final[re.Pattern[str]] = re.compile(r"@KafkaListener\s*\(")
_CONVERT_AND_SEND: Final[re.Pattern[str]] = re.compile(r"\.convertAndSend\s*\(")
_KAFKA_SEND: Final[re.Pattern[str]] = re.compile(r"\.send(?:Default)?\s*\(")
_SET_QUEUE_NAMES: Final[re.Pattern[str]] = re.compile(r"\.(?:setQueueNames|addQueueNames)\s*\(")

_STRING: Final[re.Pattern[str]] = re.compile(r'^@?"((?:[^"\\]|\\.)*)"$')
_NAMED_ARGUMENT: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z_]\w*)\s*[:=](?!=)\s*(.*)$", re.DOTALL)
_STREAM_BINDING: Final[re.Pattern[str]] = re.compile(
    r"^spring\.cloud\.stream\.bindings\.([^.]+)\.destination$"
)
_INPUT_BINDING: Final[re.Pattern[str]] = re.compile(r"(?:-in-\d+$|^input)", re.IGNORECASE)
_OUTPUT_BINDING: Final[re.Pattern[str]] = re.compile(r"(?:-out-\d+$|^output)", re.IGNORECASE)
_PLACEHOLDER: Final[tuple[str, ...]] = ("${", "#{", "{", "}")


# File-local string constants: Java ``static final String X = "..."`` (any
# modifiers, or none for a field), C# ``const string X = "..."`` and
# ``static readonly string X = "..."``.
_CONSTANT: Final[re.Pattern[str]] = re.compile(
    r'\b(?:String|string)\s+([A-Za-z_]\w*)\s*=\s*@?"((?:[^"\\]|\\.)*)"\s*;'
)
_REFERENCE: Final[re.Pattern[str]] = re.compile(r"^(?:this\.|[A-Z]\w*\.)?([A-Za-z_]\w*)$")


def file_constants(content: str) -> dict[str, str]:
    """Names the file assigns exactly one string literal to (a name
    assigned two different values is ambiguous and left out)."""
    values: dict[str, set[str]] = {}
    for match in _CONSTANT.finditer(content):
        values.setdefault(match.group(1), set()).add(match.group(2))
    return {name: next(iter(found)) for name, found in values.items() if len(found) == 1}


def _line(content: str, position: int) -> int:
    return content.count("\n", 0, position) + 1


def _in_comment(content: str, position: int) -> bool:
    """Whether ``position`` is on a ``//``, ``/*``/``*`` or ``#`` comment line."""
    start = content.rfind("\n", 0, position) + 1
    prefix = content[start:position].lstrip()
    return prefix.startswith(("//", "/*", "*", "#")) or "//" in prefix.replace("://", "")


def _arguments(content: str, open_paren: int) -> list[str] | None:
    """Top-level arguments of the call whose ``(`` is at ``open_paren``;
    None when the parentheses don't close."""
    depth = 0
    quote: str | None = None
    current: list[str] = []
    arguments: list[str] = []
    position = open_paren
    while position < len(content):
        char = content[position]
        if quote is not None:
            current.append(char)
            if char == "\\":
                position += 1
                if position < len(content):
                    current.append(content[position])
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            if depth > 1:
                current.append(char)
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                tail = "".join(current).strip()
                if tail or arguments:
                    arguments.append(tail)
                return arguments
            current.append(char)
        elif char == "," and depth == 1:
            arguments.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        position += 1
    return None


def _literal(argument: str, constants: dict[str, str] | None = None) -> str | None:
    """The value of a plain string literal argument (``"x"``, ``@"x"``), or
    of a file-local string constant it names (``QUEUE``, ``this.queue``,
    ``Config.QUEUE``); None for anything computed (other variables,
    ``$"..."``, concatenation, ``${placeholders}``)."""
    stripped = argument.strip()
    match = _STRING.match(stripped)
    if match is not None:
        value = match.group(1)
    elif constants and (reference := _REFERENCE.match(stripped)) and reference.group(1) in constants:
        value = constants[reference.group(1)]
    else:
        return None
    return None if any(marker in value for marker in _PLACEHOLDER) else value


def _split_named(arguments: list[str]) -> tuple[list[str], dict[str, str]]:
    """Positional arguments and ``name: value`` (C#) or ``name = value``
    (annotation) arguments."""
    positional: list[str] = []
    named: dict[str, str] = {}
    for argument in arguments:
        match = _NAMED_ARGUMENT.match(argument)
        if match is not None and not argument.lstrip().startswith(('"', "@")):
            named[match.group(1)] = match.group(2).strip()
        else:
            positional.append(argument)
    return positional, named


def _argument(
    positional: list[str], named: dict[str, str], index: int, *names: str
) -> str | None:
    for name in names:
        if name in named:
            return named[name]
    return positional[index] if index < len(positional) else None


def _literals(value: str | None, constants: dict[str, str] | None = None) -> list[str]:
    """String values of an annotation value: ``"a"``, ``{"a", "b"}`` or
    constants (see ``_literal``)."""
    if value is None:
        return []
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        items = _arguments(value, 0) or []
        return [literal for item in items if (literal := _literal(item, constants)) is not None]
    literal = _literal(value, constants)
    return [literal] if literal is not None else []


def _use(
    broker: ChannelBroker,
    kind: ChannelKind,
    name: str,
    role: ChannelRole,
    path: str,
    line: int,
    via: str,
) -> ChannelUse:
    return ChannelUse(
        broker=broker, kind=kind, name=name, role=role, file_path=path, line=line, via=via
    )


def _matches(pattern: re.Pattern[str], content: str) -> Iterator[re.Match[str]]:
    return (m for m in pattern.finditer(content) if not _in_comment(content, m.start()))


def _event_bus_uses(content: str, path: str) -> list[ChannelUse]:
    uses: list[ChannelUse] = []
    for pattern, role, via in (
        (_ADD_SUBSCRIPTION, ChannelRole.CONSUMER, "AddSubscription<T, H>"),
        (_EVENT_HANDLER, ChannelRole.CONSUMER, "IIntegrationEventHandler<T>"),
        (_NEW_INTEGRATION_EVENT, ChannelRole.PRODUCER, "new integration event"),
    ):
        for match in _matches(pattern, content):
            if _is_type_parameter(match.group(1)):
                continue
            uses.append(
                _use(
                    ChannelBroker.EVENT_BUS, ChannelKind.MESSAGE_TYPE, match.group(1), role,
                    path, _line(content, match.start()), via,
                )
            )
    return uses


_TYPE_PARAMETERS: Final[frozenset[str]] = frozenset(
    {"TMessage", "TEvent", "TCommand", "TRequest", "TConsumer", "TIntegrationEvent"}
)


def _is_type_parameter(name: str) -> bool:
    """``T``, ``TMessage``: a generic parameter, not a message type."""
    return len(name) == 1 or name in _TYPE_PARAMETERS


def _masstransit_uses(content: str, path: str) -> list[ChannelUse]:
    if not any(marker in content for marker in _MASSTRANSIT_MARKERS):
        return []
    uses: list[ChannelUse] = []
    for match in _matches(_MT_CONSUMER, content):
        if not _is_type_parameter(match.group(1)):
            uses.append(
                _use(
                    ChannelBroker.MASSTRANSIT, ChannelKind.MESSAGE_TYPE, match.group(1),
                    ChannelRole.CONSUMER, path, _line(content, match.start()), "IConsumer<T>",
                )
            )
    for pattern in (_MT_GENERIC_SEND, _MT_NEW_SEND):
        for match in _matches(pattern, content):
            if not _is_type_parameter(match.group(2)):
                uses.append(
                    _use(
                        ChannelBroker.MASSTRANSIT, ChannelKind.MESSAGE_TYPE, match.group(2),
                        ChannelRole.PRODUCER, path, _line(content, match.start()), match.group(1),
                    )
                )
    return uses


def _service_bus_uses(content: str, path: str) -> list[ChannelUse]:
    constants = file_constants(content)
    if "ServiceBus" not in content:
        return []
    uses: list[ChannelUse] = []
    for match in _matches(_SB_CALL, content):
        arguments = _arguments(content, match.end() - 1)
        if not arguments:
            continue
        positional, named = _split_named(arguments)
        method = match.group(1)
        line = _line(content, match.start())
        if method == "CreateSender":
            name = _literal(_argument(positional, named, 0, "queueOrTopicName") or "", constants)
            if name is not None:
                uses.append(
                    _use(ChannelBroker.SERVICE_BUS, ChannelKind.QUEUE, name, ChannelRole.PRODUCER, path, line, method)
                )
            continue
        kind = ChannelKind.QUEUE
        if "queueName" in named:
            name = _literal(named["queueName"], constants)
        else:
            name = _literal(_argument(positional, named, 0, "topicName") or "", constants)
            second = _argument(positional, named, 1, "subscriptionName")
            if second is not None and not _is_options(second):
                kind = ChannelKind.TOPIC
        if name is not None:
            uses.append(
                _use(ChannelBroker.SERVICE_BUS, kind, name, ChannelRole.CONSUMER, path, line, method)
            )
    return uses


def _is_options(argument: str) -> bool:
    """A processor or receiver options argument, not a subscription name."""
    stripped = argument.strip()
    return stripped.startswith("new ") or "options" in stripped.lower()


def _rabbit_client_uses(content: str, path: str) -> list[ChannelUse]:
    constants = file_constants(content)
    uses: list[ChannelUse] = []
    for match in _matches(_RABBIT_CALL, content):
        arguments = _arguments(content, match.end() - 1)
        if not arguments:
            continue
        positional, named = _split_named(arguments)
        method = match.group(1)
        line = _line(content, match.start())
        if method == "BasicPublish":
            exchange_argument = _argument(positional, named, 0, "exchange")
            exchange = (
                "" if exchange_argument is not None and exchange_argument.strip() == "string.Empty"
                else _literal(exchange_argument or "", constants)
            )
            if exchange is None:
                continue
            if exchange:
                uses.append(
                    _use(ChannelBroker.RABBITMQ, ChannelKind.EXCHANGE, exchange, ChannelRole.PRODUCER, path, line, method)
                )
                continue
            queue = _literal(_argument(positional, named, 1, "routingKey") or "", constants)
            if queue:
                uses.append(
                    _use(ChannelBroker.RABBITMQ, ChannelKind.QUEUE, queue, ChannelRole.PRODUCER, path, line, method)
                )
        elif method == "BasicConsume":
            queue = _literal(_argument(positional, named, 0, "queue") or "", constants)
            if queue:
                uses.append(
                    _use(ChannelBroker.RABBITMQ, ChannelKind.QUEUE, queue, ChannelRole.CONSUMER, path, line, method)
                )
        else:  # QueueBind(queue, exchange, routingKey)
            exchange = _literal(_argument(positional, named, 1, "exchange") or "", constants)
            if exchange:
                uses.append(
                    _use(ChannelBroker.RABBITMQ, ChannelKind.EXCHANGE, exchange, ChannelRole.CONSUMER, path, line, method)
                )
    return uses


def _annotation_uses(content: str, path: str) -> list[ChannelUse]:
    constants = file_constants(content)
    uses: list[ChannelUse] = []
    for match in _matches(_RABBIT_LISTENER, content):
        arguments = _arguments(content, match.end() - 1) or []
        positional, named = _split_named(arguments)
        line = _line(content, match.start())
        queues = _literals(named.get("queues"), constants) + _literals(positional[0] if positional else None, constants)
        for declared in _literals_in(named.get("queuesToDeclare"), "Queue", constants):
            queues.append(declared)
        for queue in dict.fromkeys(queues):
            uses.append(
                _use(ChannelBroker.RABBITMQ, ChannelKind.QUEUE, queue, ChannelRole.CONSUMER, path, line, "@RabbitListener")
            )
        for exchange in dict.fromkeys(_literals_in(named.get("bindings"), "Exchange", constants)):
            uses.append(
                _use(ChannelBroker.RABBITMQ, ChannelKind.EXCHANGE, exchange, ChannelRole.CONSUMER, path, line, "@RabbitListener")
            )
    for match in _matches(_KAFKA_LISTENER, content):
        arguments = _arguments(content, match.end() - 1) or []
        _, named = _split_named(arguments)
        line = _line(content, match.start())
        for topic in dict.fromkeys(_literals(named.get("topics"), constants)):
            uses.append(
                _use(ChannelBroker.KAFKA, ChannelKind.TOPIC, topic, ChannelRole.CONSUMER, path, line, "@KafkaListener")
            )
    return uses


def _literals_in(value: str | None, annotation: str, constants: dict[str, str]) -> list[str]:
    """Names in ``@Annotation("x")`` / ``@Annotation(value = "x")`` /
    ``@Annotation(name = "x")`` nested anywhere in ``value``."""
    if not value:
        return []
    found: list[str] = []
    for match in re.finditer(rf"@{annotation}\s*\(", value):
        arguments = _arguments(value, match.end() - 1) or []
        positional, named = _split_named(arguments)
        candidate = named.get("value") or named.get("name") or (positional[0] if positional else None)
        found.extend(_literals(candidate, constants))
    return found


def _template_uses(content: str, path: str) -> list[ChannelUse]:
    constants = file_constants(content)
    uses: list[ChannelUse] = []
    if "RabbitTemplate" in content or "AmqpTemplate" in content:
        for match in _matches(_CONVERT_AND_SEND, content):
            arguments = _arguments(content, match.end() - 1) or []
            line = _line(content, match.start())
            if len(arguments) >= 3:
                exchange = _literal(arguments[0], constants)
                if exchange:
                    uses.append(
                        _use(ChannelBroker.RABBITMQ, ChannelKind.EXCHANGE, exchange, ChannelRole.PRODUCER, path, line, "convertAndSend")
                    )
                elif exchange == "" and (queue := _literal(arguments[1], constants)):
                    uses.append(
                        _use(ChannelBroker.RABBITMQ, ChannelKind.QUEUE, queue, ChannelRole.PRODUCER, path, line, "convertAndSend")
                    )
            elif len(arguments) == 2 and (queue := _literal(arguments[0], constants)):
                uses.append(
                    _use(ChannelBroker.RABBITMQ, ChannelKind.QUEUE, queue, ChannelRole.PRODUCER, path, line, "convertAndSend")
                )
    if "MessageListenerContainer" in content:
        for match in _matches(_SET_QUEUE_NAMES, content):
            for argument in _arguments(content, match.end() - 1) or []:
                queue = _literal(argument, constants)
                if queue:
                    uses.append(
                        _use(ChannelBroker.RABBITMQ, ChannelKind.QUEUE, queue, ChannelRole.CONSUMER, path, _line(content, match.start()), "setQueueNames")
                    )
    if "KafkaTemplate" in content:
        for match in _matches(_KAFKA_SEND, content):
            arguments = _arguments(content, match.end() - 1) or []
            if len(arguments) >= 2 and (topic := _literal(arguments[0], constants)):
                uses.append(
                    _use(ChannelBroker.KAFKA, ChannelKind.TOPIC, topic, ChannelRole.PRODUCER, path, _line(content, match.start()), "KafkaTemplate.send")
                )
    return uses


def _stream_broker(flat: dict[str, str]) -> tuple[ChannelBroker, ChannelKind]:
    keys = list(flat)
    if any(k.startswith(("spring.cloud.stream.kafka.", "spring.kafka.")) for k in keys):
        return ChannelBroker.KAFKA, ChannelKind.TOPIC
    if any(k.startswith(("spring.cloud.stream.rabbit.", "spring.rabbitmq.")) for k in keys):
        return ChannelBroker.RABBITMQ, ChannelKind.EXCHANGE
    return ChannelBroker.SPRING_CLOUD_STREAM, ChannelKind.TOPIC


def _key_line(content: str, binding: str) -> int:
    position = content.find(binding)
    return _line(content, position) if position >= 0 else 1


def _stream_uses(content: str, path: str) -> list[ChannelUse]:
    try:
        documents = spring_documents(content, path, None)
    except ConfigParseError:
        return []
    uses: list[ChannelUse] = []
    for _, flat in documents:
        broker, kind = _stream_broker(flat)
        for key, value in flat.items():
            match = _STREAM_BINDING.match(key)
            if match is None:
                continue
            binding = match.group(1)
            if _INPUT_BINDING.search(binding):
                role = ChannelRole.CONSUMER
            elif _OUTPUT_BINDING.search(binding):
                role = ChannelRole.PRODUCER
            else:
                continue
            for destination in value.split(","):
                name = destination.strip()
                if name and not any(marker in name for marker in _PLACEHOLDER):
                    uses.append(
                        _use(broker, kind, name, role, path, _key_line(content, binding), f"binding {binding}")
                    )
    return uses


def channel_uses(content: str, path: str, spring_config: bool = False) -> list[ChannelUse]:
    """Every literal channel use in one file, in file order (see module doc).

    ``spring_config`` reads the file as Spring configuration whatever its
    name (a file a config server serves to a client, ``<app>.yml``).
    """
    relative = path.lstrip("/")
    filename = os.path.basename(relative)
    suffix = os.path.splitext(filename)[1].lower()
    if suffix in _CSHARP:
        uses = [
            *_event_bus_uses(content, relative),
            *_masstransit_uses(content, relative),
            *_service_bus_uses(content, relative),
            *_rabbit_client_uses(content, relative),
        ]
    elif suffix in _JVM:
        uses = [*_annotation_uses(content, relative), *_template_uses(content, relative)]
    elif spring_config or _SPRING_CONFIG.match(filename):
        uses = _stream_uses(content, relative)
    else:
        return []
    roles: dict[tuple[ChannelBroker, str], set[ChannelRole]] = {}
    for use in uses:
        roles.setdefault((use.broker, use.name), set()).add(use.role)
    # A file that both produces to and consumes the same channel is the bus's
    # own plumbing (a generic publish and subscribe over one exchange, as an
    # event-bus library has), not a service's messaging.
    kept = [use for use in uses if len(roles[(use.broker, use.name)]) == 1]
    return sorted(dict.fromkeys(kept), key=lambda u: (u.line, u.role.value, u.broker.value, u.name))
