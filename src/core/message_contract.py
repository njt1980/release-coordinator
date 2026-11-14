"""Compare two versions of a message payload type (contract v2, item 4a).

A type-routed bus (eShop ``IEventBus``, MassTransit) delivers a message by
its type's name, so a ``message_type`` channel's payload is the class or
record of that name (see ``core.messaging``). The type's properties are its
wire contract: consumers deserialize what the producer serialized. Types
are read from source by ``tools.message_schemas``; this module only
compares them.

- Breaking: the type removed; a property removed; a property's declared
  type changed (as written, whitespace ignored: ``string`` to ``string?``
  counts).
- Compatible: the type added; a property added (consumers ignore unknown
  JSON members by default).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.core.contract_findings import ContractChangeKind, ContractFinding


class PayloadType(BaseModel):
    """A class or record's serialized members: property or field name to
    its declared type (whitespace removed). ``language`` is the source
    language (``csharp``, ``java``)."""

    name: str = Field(min_length=1)
    language: str = ""
    properties: dict[str, str] = Field(default_factory=dict)


def diff_payload(path: str, name: str, base: PayloadType | None, head: PayloadType | None) -> list[ContractFinding]:
    """Every difference between the base and head of message type ``name``
    declared in ``path``; None is a type absent at that commit."""
    if base is None and head is None:
        return []
    if base is None:
        return [ContractFinding(path=path, symbol=name, change=ContractChangeKind.MESSAGE_ADDED,
                                detail=f"Message type {name} was added.")]
    if head is None:
        return [ContractFinding(path=path, symbol=name, change=ContractChangeKind.MESSAGE_REMOVED,
                                detail=f"Message type {name} was removed; its consumers can no longer receive it.")]
    findings: list[ContractFinding] = []
    for prop, written in base.properties.items():
        symbol = f"{name}.{prop}"
        if prop not in head.properties:
            findings.append(ContractFinding(path=path, symbol=symbol, change=ContractChangeKind.FIELD_REMOVED,
                                            detail=f"Message property {symbol} was removed."))
        elif head.properties[prop] != written:
            findings.append(ContractFinding(
                path=path, symbol=symbol, change=ContractChangeKind.FIELD_TYPE_CHANGED,
                detail=f"Message property {symbol} changed type from {written} to {head.properties[prop]}.",
            ))
    for prop in head.properties:
        if prop not in base.properties:
            findings.append(ContractFinding(path=path, symbol=f"{name}.{prop}", change=ContractChangeKind.FIELD_ADDED,
                                            detail=f"Message property {name}.{prop} was added."))
    return findings
