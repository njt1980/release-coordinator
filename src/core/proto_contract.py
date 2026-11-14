"""Read ``.proto`` files and compare two versions of one (contract evidence).

An in-house reader for the parts of proto3 (and proto2) that decide whether
a change is compatible for the services exchanging the messages: the
package, messages (nested ones by their full name), their fields (number,
name, type, cardinality, oneof), enums and their values, and services with
their RPCs. Options, imports, ``reserved`` and ``extensions`` statements are
read past; ``extend`` blocks are skipped whole.

``diff_protos`` compares a base and a head version. The rules are
conservative: a change is breaking when a consumer or provider built
against one version may fail against the other, on the wire or in its
generated code. Fields are matched by number, enum values by number, RPCs
by name:

- Breaking: a removed file, package, service, RPC, message, enum, field or
  enum value; a changed RPC request/response type or streaming; a field
  whose type, name, repeated-ness or oneof changed; an enum value renamed;
  a new ``required`` field (proto2).
- Compatible: anything added (file, service, RPC, message, enum, field,
  enum value); a field gaining or losing ``optional`` (explicit presence).

Type names are compared as written, without the package prefix or a
leading dot; the same type written two ways reads as a change (breaking,
the safe side).
"""

from __future__ import annotations

import re
from typing import Final, Iterator

from pydantic import BaseModel, Field

from src.core.contract_findings import BREAKING_CHANGES, ContractChangeKind, ContractFinding

__all__ = [
    "BREAKING_CHANGES",
    "ContractChangeKind",
    "ContractFinding",
    "ProtoFile",
    "ProtoParseError",
    "diff_protos",
    "parse_proto",
]


class ProtoParseError(ValueError):
    """A ``.proto`` file this reader can't understand."""


class ProtoField(BaseModel):
    """One message field. ``label`` is ``repeated``, ``optional``,
    ``required``, ``map`` or empty; ``oneof`` the oneof it belongs to."""

    name: str
    number: int
    type: str
    label: str = ""
    oneof: str = ""


class ProtoMessage(BaseModel):
    name: str
    fields: dict[int, ProtoField] = Field(default_factory=dict)


class ProtoEnum(BaseModel):
    name: str
    values: dict[int, str] = Field(default_factory=dict)


class ProtoRpc(BaseModel):
    name: str
    request: str
    response: str
    client_streaming: bool = False
    server_streaming: bool = False


class ProtoService(BaseModel):
    name: str
    rpcs: dict[str, ProtoRpc] = Field(default_factory=dict)


class ProtoFile(BaseModel):
    """What one ``.proto`` file declares; names are fully qualified
    (``package.Outer.Inner``) except RPC and field names."""

    package: str = ""
    messages: dict[str, ProtoMessage] = Field(default_factory=dict)
    enums: dict[str, ProtoEnum] = Field(default_factory=dict)
    services: dict[str, ProtoService] = Field(default_factory=dict)


# --- Reading ---------------------------------------------------------------

_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"""
    (?P<space>\s+)
    | (?P<line_comment>//[^\n]*)
    | (?P<block_comment>/\*.*?\*/)
    | (?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
    | (?P<word>[A-Za-z_.][A-Za-z0-9_.]*)
    | (?P<number>[-+]?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][-+]?\d+)?))
    | (?P<symbol>[{}()\[\]<>;=,:])
    """,
    re.VERBOSE | re.DOTALL,
)
_LABELS: Final[frozenset[str]] = frozenset({"repeated", "optional", "required"})
# Statements read past inside a message or at the top level.
_SKIPPED_STATEMENTS: Final[frozenset[str]] = frozenset(
    {"option", "reserved", "extensions", "import", "syntax", "edition"}
)


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise ProtoParseError(f"unexpected character {text[position]!r} at offset {position}")
        position = match.end()
        kind = match.lastgroup
        if kind in ("space", "line_comment", "block_comment"):
            continue
        tokens.append(match.group())
    return tokens


class _Reader:
    def __init__(self, text: str) -> None:
        self.tokens = _tokens(text.lstrip("﻿"))
        self.index = 0
        self.file = ProtoFile()

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> str:
        token = self.peek()
        if token is None:
            raise ProtoParseError("unexpected end of file")
        self.index += 1
        return token

    def expect(self, token: str) -> None:
        found = self.next()
        if found != token:
            raise ProtoParseError(f"expected {token!r}, found {found!r}")

    def skip_statement(self) -> None:
        """Skip to the end of the statement (``;``), past any bracketed parts."""
        depth = 0
        while True:
            token = self.next()
            if token in "{[(":
                depth += 1
            elif token in "}])":
                depth -= 1
                if depth == 0 and token == "}":
                    if self.peek() == ";":
                        self.next()
                    return
            elif token == ";" and depth == 0:
                return

    def skip_block(self) -> None:
        """Skip a ``{ ... }`` block (the next token must open it)."""
        self.expect("{")
        depth = 1
        while depth:
            token = self.next()
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1

    def qualified(self, scope: str, name: str) -> str:
        return f"{scope}.{name}" if scope else name

    def read(self) -> ProtoFile:
        while (token := self.peek()) is not None:
            if token == ";":
                self.next()
            elif token == "package":
                self.next()
                self.file.package = self.next()
                self.expect(";")
            elif token in _SKIPPED_STATEMENTS:
                self.skip_statement()
            elif token == "message":
                self.next()
                self.message(self.file.package)
            elif token == "enum":
                self.next()
                self.enum(self.file.package)
            elif token == "service":
                self.next()
                self.service()
            elif token == "extend":
                self.next()
                self.next()
                self.skip_block()
            else:
                raise ProtoParseError(f"unexpected {token!r} at the top level")
        return self.file

    def message(self, scope: str) -> None:
        name = self.qualified(scope, self.next())
        message = ProtoMessage(name=name)
        self.file.messages[name] = message
        self.expect("{")
        self.message_body(message, oneof="")
        if self.peek() == ";":
            self.next()

    def message_body(self, message: ProtoMessage, oneof: str) -> None:
        while (token := self.next()) != "}":
            if token == ";":
                continue
            if token in _SKIPPED_STATEMENTS:
                self.index -= 1
                self.skip_statement()
            elif token == "message" and not oneof:
                self.message(message.name)
            elif token == "enum" and not oneof:
                self.enum(message.name)
            elif token == "extend":
                self.next()
                self.skip_block()
            elif token == "oneof" and not oneof:
                group = self.next()
                self.expect("{")
                self.message_body(message, oneof=group)
            elif token == "map":
                self.expect("<")
                key = self.next()
                self.expect(",")
                value = self.next()
                self.expect(">")
                self.field(message, f"map<{key},{value}>", "map", oneof)
            elif token in _LABELS:
                self.field(message, self.next(), token, oneof)
            else:
                self.field(message, token, "", oneof)

    def field(self, message: ProtoMessage, type_name: str, label: str, oneof: str) -> None:
        name = self.next()
        if name == "group" or type_name == "group":
            raise ProtoParseError("proto2 groups are not supported")
        self.expect("=")
        number_token = self.next()
        try:
            number = int(number_token, 0)
        except ValueError as error:
            raise ProtoParseError(f"field {name} has no number ({number_token!r})") from error
        if self.peek() == "[":
            while self.next() != "]":
                pass
        self.expect(";")
        message.fields[number] = ProtoField(
            name=name, number=number, type=_type_name(type_name, self.file.package), label=label, oneof=oneof
        )

    def enum(self, scope: str) -> None:
        name = self.qualified(scope, self.next())
        enum = ProtoEnum(name=name)
        self.file.enums[name] = enum
        self.expect("{")
        while (token := self.next()) != "}":
            if token == ";":
                continue
            if token in _SKIPPED_STATEMENTS:
                self.index -= 1
                self.skip_statement()
                continue
            self.expect("=")
            number = int(self.next(), 0)
            if self.peek() == "[":
                while self.next() != "]":
                    pass
            self.expect(";")
            enum.values[number] = token
        if self.peek() == ";":
            self.next()

    def service(self) -> None:
        name = self.qualified(self.file.package, self.next())
        service = ProtoService(name=name)
        self.file.services[name] = service
        self.expect("{")
        while (token := self.next()) != "}":
            if token == ";":
                continue
            if token == "option":
                self.index -= 1
                self.skip_statement()
                continue
            if token != "rpc":
                raise ProtoParseError(f"unexpected {token!r} in service {name}")
            rpc_name = self.next()
            client_streaming, request = self.rpc_type()
            self.expect("returns")
            server_streaming, response = self.rpc_type()
            if self.peek() == "{":
                self.skip_block()
            if self.peek() == ";":
                self.next()
            service.rpcs[rpc_name] = ProtoRpc(
                name=rpc_name,
                request=request,
                response=response,
                client_streaming=client_streaming,
                server_streaming=server_streaming,
            )

    def rpc_type(self) -> tuple[bool, str]:
        self.expect("(")
        token = self.next()
        streaming = token == "stream" and self.peek() != ")"
        type_name = self.next() if streaming else token
        self.expect(")")
        return streaming, _type_name(type_name, self.file.package)


def _type_name(written: str, package: str) -> str:
    name = written.lstrip(".")
    if package and name.startswith(package + "."):
        name = name[len(package) + 1 :]
    return name


def parse_proto(text: str) -> ProtoFile:
    """Read one ``.proto`` file.

    Raises:
        ProtoParseError: if the text isn't a ``.proto`` file this reader
            understands.
    """
    try:
        return _Reader(text).read()
    except ProtoParseError:
        raise
    except (ValueError, IndexError) as error:
        raise ProtoParseError(str(error)) from error


# --- Comparing -------------------------------------------------------------


def _short(name: str, package: str) -> str:
    return _type_name(name, package)


def _rpc_signature(rpc: ProtoRpc) -> str:
    request = f"stream {rpc.request}" if rpc.client_streaming else rpc.request
    response = f"stream {rpc.response}" if rpc.server_streaming else rpc.response
    return f"({request}) returns ({response})"


def _diff_services(path: str, base: ProtoFile, head: ProtoFile) -> Iterator[ContractFinding]:
    for name, service in base.services.items():
        short = _short(name, base.package)
        new = head.services.get(name)
        if new is None:
            yield ContractFinding(path=path, symbol=short, change=ContractChangeKind.SERVICE_REMOVED,
                                  detail=f"Service {short} was removed.")
            continue
        for rpc_name, rpc in service.rpcs.items():
            symbol = f"{short}.{rpc_name}"
            after = new.rpcs.get(rpc_name)
            if after is None:
                yield ContractFinding(path=path, symbol=symbol, change=ContractChangeKind.RPC_REMOVED,
                                      detail=f"RPC {symbol} was removed.")
            elif (rpc.request, rpc.response) != (after.request, after.response):
                yield ContractFinding(
                    path=path, symbol=symbol, change=ContractChangeKind.RPC_TYPES_CHANGED,
                    detail=f"RPC {symbol} changed from {_rpc_signature(rpc)} to {_rpc_signature(after)}.",
                )
            elif (rpc.client_streaming, rpc.server_streaming) != (after.client_streaming, after.server_streaming):
                yield ContractFinding(
                    path=path, symbol=symbol, change=ContractChangeKind.RPC_STREAMING_CHANGED,
                    detail=f"RPC {symbol} changed from {_rpc_signature(rpc)} to {_rpc_signature(after)}.",
                )
        for rpc_name in new.rpcs.keys() - service.rpcs.keys():
            yield ContractFinding(path=path, symbol=f"{short}.{rpc_name}", change=ContractChangeKind.RPC_ADDED,
                                  detail=f"RPC {short}.{rpc_name} was added.")
    for name in head.services.keys() - base.services.keys():
        short = _short(name, head.package)
        yield ContractFinding(path=path, symbol=short, change=ContractChangeKind.SERVICE_ADDED,
                              detail=f"Service {short} was added.")


def _cardinality(label: str) -> str:
    return label if label in ("repeated", "map") else "single"


def _diff_field(path: str, symbol: str, before: ProtoField, after: ProtoField) -> Iterator[ContractFinding]:
    if before.name != after.name:
        yield ContractFinding(path=path, symbol=symbol, change=ContractChangeKind.FIELD_RENAMED,
                              detail=f"Field {symbol} = {before.number} was renamed to {after.name}.")
    if before.type != after.type:
        yield ContractFinding(path=path, symbol=symbol, change=ContractChangeKind.FIELD_TYPE_CHANGED,
                              detail=f"Field {symbol} changed type from {before.type} to {after.type}.")
    if _cardinality(before.label) != _cardinality(after.label):
        yield ContractFinding(
            path=path, symbol=symbol, change=ContractChangeKind.FIELD_CARDINALITY_CHANGED,
            detail=f"Field {symbol} changed from {_cardinality(before.label)} to {_cardinality(after.label)}.",
        )
    elif before.label != after.label:
        yield ContractFinding(
            path=path, symbol=symbol, change=ContractChangeKind.FIELD_PRESENCE_CHANGED,
            detail=f"Field {symbol} changed from '{before.label or 'implicit'}' to '{after.label or 'implicit'}'.",
        )
    if before.oneof != after.oneof:
        yield ContractFinding(
            path=path, symbol=symbol, change=ContractChangeKind.FIELD_ONEOF_CHANGED,
            detail=f"Field {symbol} moved from oneof '{before.oneof or '-'}' to '{after.oneof or '-'}'.",
        )


def _diff_messages(path: str, base: ProtoFile, head: ProtoFile) -> Iterator[ContractFinding]:
    for name, message in base.messages.items():
        short = _short(name, base.package)
        new = head.messages.get(name)
        if new is None:
            yield ContractFinding(path=path, symbol=short, change=ContractChangeKind.MESSAGE_REMOVED,
                                  detail=f"Message {short} was removed.")
            continue
        for number, field in message.fields.items():
            symbol = f"{short}.{field.name}"
            after = new.fields.get(number)
            if after is None:
                yield ContractFinding(path=path, symbol=symbol, change=ContractChangeKind.FIELD_REMOVED,
                                      detail=f"Field {symbol} = {number} was removed.")
            else:
                yield from _diff_field(path, symbol, field, after)
        for number in sorted(new.fields.keys() - message.fields.keys()):
            field = new.fields[number]
            required = field.label == "required"
            yield ContractFinding(
                path=path, symbol=f"{short}.{field.name}",
                change=ContractChangeKind.FIELD_REQUIRED_ADDED if required else ContractChangeKind.FIELD_ADDED,
                detail=f"{'Required field' if required else 'Field'} {short}.{field.name} = {number} was added.",
            )
    for name in head.messages.keys() - base.messages.keys():
        short = _short(name, head.package)
        yield ContractFinding(path=path, symbol=short, change=ContractChangeKind.MESSAGE_ADDED,
                              detail=f"Message {short} was added.")


def _diff_enums(path: str, base: ProtoFile, head: ProtoFile) -> Iterator[ContractFinding]:
    for name, enum in base.enums.items():
        short = _short(name, base.package)
        new = head.enums.get(name)
        if new is None:
            yield ContractFinding(path=path, symbol=short, change=ContractChangeKind.ENUM_REMOVED,
                                  detail=f"Enum {short} was removed.")
            continue
        for number, value in enum.values.items():
            after = new.values.get(number)
            if after is None:
                yield ContractFinding(path=path, symbol=f"{short}.{value}", change=ContractChangeKind.ENUM_VALUE_REMOVED,
                                      detail=f"Enum value {short}.{value} = {number} was removed.")
            elif after != value:
                yield ContractFinding(path=path, symbol=f"{short}.{value}", change=ContractChangeKind.ENUM_VALUE_RENAMED,
                                      detail=f"Enum value {short}.{value} = {number} was renamed to {after}.")
        for number in sorted(new.values.keys() - enum.values.keys()):
            yield ContractFinding(path=path, symbol=f"{short}.{new.values[number]}",
                                  change=ContractChangeKind.ENUM_VALUE_ADDED,
                                  detail=f"Enum value {short}.{new.values[number]} = {number} was added.")
    for name in head.enums.keys() - base.enums.keys():
        short = _short(name, head.package)
        yield ContractFinding(path=path, symbol=short, change=ContractChangeKind.ENUM_ADDED,
                              detail=f"Enum {short} was added.")


def diff_protos(path: str, base: ProtoFile | None, head: ProtoFile | None) -> list[ContractFinding]:
    """Every difference between ``base`` and ``head`` of the file at ``path``.

    ``base`` None is a new file, ``head`` None a removed one.
    """
    if base is None and head is None:
        return []
    if base is None:
        return [ContractFinding(path=path, change=ContractChangeKind.FILE_ADDED, detail=f"{path} was added.")]
    if head is None:
        return [ContractFinding(path=path, change=ContractChangeKind.FILE_REMOVED, detail=f"{path} was removed.")]
    if base.package != head.package:
        return [
            ContractFinding(
                path=path, symbol=head.package, change=ContractChangeKind.PACKAGE_CHANGED,
                detail=f"Package changed from '{base.package}' to '{head.package}': every type's full name changes.",
            )
        ]
    return [*_diff_services(path, base, head), *_diff_messages(path, base, head), *_diff_enums(path, base, head)]
