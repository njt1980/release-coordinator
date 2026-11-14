"""Read OpenAPI documents and compare two versions of one (contract v2).

An in-house reader for the parts of OpenAPI 3.x and Swagger 2.0 that decide
whether a change is compatible for the clients of an HTTP API: each
operation (method and path), its parameters, its request body and its
responses, with their schemas resolved through local ``$ref`` pointers
(``#/components/...``, ``#/definitions/...``). ``allOf`` is merged;
``oneOf``/``anyOf`` and references to other files are opaque (not
compared). Schemas are read to a fixed depth; a recursive schema stops at
its first repetition.

``diff_openapi`` compares a base and a head version. Operations are matched
by method and path, path parameter names ignored (``/orders/{id}`` and
``/orders/{orderId}`` are the same operation); an operation that moved to
another path keeping its ``operationId`` is a rename. The rules take the
client's side:

- Breaking: a removed or renamed operation; a new required parameter or
  one that became required; a request body that became required; a new
  required request field or one that became required; a removed success
  (2xx or ``default``) response; a response field removed or no longer
  required; a type change (``type``, or ``format`` when both versions name
  one); an enum in a request or parameter losing values (narrowed).
- Compatible: added operations, optional parameters and fields, response
  fields and responses; removed parameters and request fields (the server
  stops reading them); response enums losing values; enums gaining values
  (reported, since clients that switch over response values may need an
  update).
"""

from __future__ import annotations

import json
import posixpath
import re
from enum import Enum
from typing import Any, Final, Iterator, Optional

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from src.core.contract_findings import ContractChangeKind, ContractFinding

_OPENAPI_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:openapi|swagger)(?:[._-][\w.-]*)?|[\w.-]+[._-](?:openapi|swagger))\.(?:ya?ml|json)$",
    re.IGNORECASE,
)
_METHODS: Final[tuple[str, ...]] = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_PATH_PARAMETER: Final[re.Pattern[str]] = re.compile(r"\{[^}/]*\}")
_MAX_DEPTH: Final[int] = 8


class OpenApiParseError(ValueError):
    """A file this reader can't understand as an OpenAPI document."""


def is_openapi_file(path: str) -> bool:
    """Whether ``path`` is named like an OpenAPI document (``openapi.yaml``,
    ``swagger.json``, ``orders.openapi.yml``, ``openapi-v2.json``...)."""
    return bool(_OPENAPI_NAME.match(posixpath.basename(path.replace("\\", "/"))))


class Direction(str, Enum):
    """Which way data flows through a schema: REQUEST from the client,
    RESPONSE to it. Compatibility rules mirror each other."""

    REQUEST = "request"
    RESPONSE = "response"


class SchemaShape(BaseModel):
    """What a schema allows, as far as compatibility goes.

    ``type`` is ``type`` (``string``, ``object``...; empty when unknown or
    opaque), ``format`` its format, ``enum`` the allowed values as strings
    (None when unrestricted), ``properties`` and ``required`` an object's
    fields, ``items`` an array's element. ``opaque`` names why the schema
    isn't compared (``oneOf``, an external ``$ref``, a recursion).
    """

    type: str = ""
    format: str = ""
    enum: Optional[list[str]] = None
    properties: dict[str, "SchemaShape"] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    items: Optional["SchemaShape"] = None
    opaque: str = ""


class OpenApiParameter(BaseModel):
    name: str
    location: str
    required: bool = False
    schema_shape: SchemaShape = Field(default_factory=SchemaShape)


class OpenApiOperation(BaseModel):
    """One operation. ``parameters`` are keyed ``<in>:<name>`` (path
    parameters ``path:#<position>``); ``request`` is the body's schema
    (None without a body); ``responses`` map status codes to schemas."""

    method: str
    path: str
    operation_id: str = ""
    parameters: dict[str, OpenApiParameter] = Field(default_factory=dict)
    request: Optional[SchemaShape] = None
    request_required: bool = False
    responses: dict[str, Optional[SchemaShape]] = Field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"


class OpenApiDocument(BaseModel):
    """The operations of one document, keyed ``<METHOD> <path>`` with path
    parameter names removed (``GET /orders/{}``)."""

    version: str
    operations: dict[str, OpenApiOperation] = Field(default_factory=dict)


# --- Reading ---------------------------------------------------------------


def _load(text: str, path: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if path.lower().endswith(".json") else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as error:
        raise OpenApiParseError(f"not valid {'JSON' if path.lower().endswith('.json') else 'YAML'}: {error}") from error
    if not isinstance(data, dict):
        raise OpenApiParseError("the document is not a mapping")
    if "openapi" not in data and "swagger" not in data:
        raise OpenApiParseError("no 'openapi' or 'swagger' version field")
    if not isinstance(data.get("paths", {}), dict):
        raise OpenApiParseError("'paths' is not a mapping")
    return data


class _Resolver:
    """Resolves local ``$ref`` pointers and builds ``SchemaShape`` objects."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def pointer(self, ref: str) -> Any:
        node: Any = self.document
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise OpenApiParseError(f"unresolved $ref {ref}")
            node = node[part]
        return node

    def deref(self, node: Any) -> Any:
        """Follow ``$ref`` chains of a parameter, body or response object."""
        seen: set[str] = set()
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/"):
            ref = node["$ref"]
            if ref in seen:
                raise OpenApiParseError(f"circular $ref {ref}")
            seen.add(ref)
            node = self.pointer(ref)
        return node

    def shape(self, schema: Any, seen: frozenset[str] = frozenset(), depth: int = 0) -> SchemaShape:
        if not isinstance(schema, dict):
            return SchemaShape()
        ref = schema.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/"):
                return SchemaShape(opaque=f"external $ref {ref}")
            if ref in seen or depth >= _MAX_DEPTH:
                return SchemaShape(opaque=f"recursive $ref {ref}")
            return self.shape(self.pointer(ref), seen | {ref}, depth + 1)
        if depth >= _MAX_DEPTH:
            return SchemaShape(opaque="nested too deep")
        for combinator in ("oneOf", "anyOf"):
            if combinator in schema:
                return SchemaShape(opaque=combinator)
        if isinstance(schema.get("allOf"), list):
            merged = SchemaShape(type="object")
            for part in schema["allOf"]:
                piece = self.shape(part, seen, depth + 1)
                if piece.opaque:
                    return SchemaShape(opaque=f"allOf with {piece.opaque}")
                merged.properties.update(piece.properties)
                merged.required = sorted({*merged.required, *piece.required})
            own = {key: value for key, value in schema.items() if key != "allOf"}
            if own.get("properties") or own.get("required"):
                piece = self.shape({**own, "type": "object"}, seen, depth + 1)
                merged.properties.update(piece.properties)
                merged.required = sorted({*merged.required, *piece.required})
            return merged
        written = schema.get("type", "")
        if isinstance(written, list):  # OpenAPI 3.1: ["string", "null"]
            written = "|".join(sorted(str(t) for t in written if t != "null"))
        kind = str(written or ("object" if "properties" in schema else ""))
        enum = schema.get("enum")
        raw_properties = schema.get("properties")
        properties: dict[Any, Any] = raw_properties if isinstance(raw_properties, dict) else {}
        raw_required = schema.get("required")
        required: list[Any] = raw_required if isinstance(raw_required, list) else []
        return SchemaShape(
            type=kind,
            format=str(schema.get("format", "") or ""),
            enum=sorted(str(value) for value in enum if value is not None) if isinstance(enum, list) else None,
            properties={str(name): self.shape(value, seen, depth + 1) for name, value in properties.items()},
            required=sorted(str(name) for name in required),
            items=self.shape(schema["items"], seen, depth + 1) if isinstance(schema.get("items"), dict) else None,
        )


def _operation_key(method: str, path: str) -> str:
    return f"{method.upper()} {_PATH_PARAMETER.sub('{}', path)}"


def _media_schema(content: Any) -> Any:
    """The JSON media type's schema of an OpenAPI 3 ``content`` map
    (``application/json`` first, then any ``*json``, then the first)."""
    if not isinstance(content, dict) or not content:
        return None
    for media in ("application/json", *(m for m in content if "json" in m.lower()), *content):
        if isinstance(content.get(media), dict):
            return content[media].get("schema")
    return None


def _parameter(resolver: _Resolver, raw: Any, swagger: bool) -> OpenApiParameter | None:
    node = resolver.deref(raw)
    if not isinstance(node, dict) or "name" not in node or "in" not in node:
        return None
    location = str(node["in"])
    if swagger and "schema" not in node:  # Swagger 2: type/format/enum on the parameter itself
        schema: Any = {key: node[key] for key in ("type", "format", "enum", "items") if key in node}
    else:
        schema = node.get("schema")
        if schema is None and isinstance(node.get("content"), dict):
            schema = _media_schema(node["content"])
    return OpenApiParameter(
        name=str(node["name"]),
        location=location,
        required=bool(node.get("required", False)) or location == "path",
        schema_shape=resolver.shape(schema),
    )


def _operation(
    resolver: _Resolver, method: str, path: str, raw: dict[str, Any], shared: list[Any], swagger: bool
) -> OpenApiOperation:
    operation = OpenApiOperation(method=method, path=path, operation_id=str(raw.get("operationId", "") or ""))
    by_key: dict[tuple[str, str], OpenApiParameter] = {}
    for item in [*shared, *(raw.get("parameters") or [])]:
        parameter = _parameter(resolver, item, swagger)
        if parameter is not None:
            by_key[(parameter.location, parameter.name)] = parameter
    path_names = [name[1:-1] for name in _PATH_PARAMETER.findall(path)]
    for (location, name), parameter in by_key.items():
        if location == "body":
            operation.request = parameter.schema_shape
            operation.request_required = parameter.required
            continue
        if location == "path" and name in path_names:
            operation.parameters[f"path:#{path_names.index(name)}"] = parameter
        else:
            operation.parameters[f"{location}:{name}"] = parameter
    body = resolver.deref(raw.get("requestBody"))
    if isinstance(body, dict):
        operation.request = resolver.shape(_media_schema(body.get("content")))
        operation.request_required = bool(body.get("required", False))
    raw_responses = raw.get("responses")
    responses: dict[Any, Any] = raw_responses if isinstance(raw_responses, dict) else {}
    for status, response in responses.items():
        node = resolver.deref(response)
        schema = None
        if isinstance(node, dict):
            schema = node.get("schema") if swagger else _media_schema(node.get("content"))
        operation.responses[str(status)] = resolver.shape(schema) if schema is not None else None
    return operation


def parse_openapi(text: str, path: str = "openapi.yaml") -> OpenApiDocument:
    """Read an OpenAPI 3.x or Swagger 2.0 document (YAML, or JSON by suffix)."""
    data = _load(text, path)
    swagger = "swagger" in data and "openapi" not in data
    resolver = _Resolver(data)
    document = OpenApiDocument(version=str(data.get("openapi") or data.get("swagger")))
    for route, item in (data.get("paths") or {}).items():
        item = resolver.deref(item)
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in _METHODS:
            if isinstance(item.get(method), dict):
                operation = _operation(resolver, method, str(route), item[method], shared, swagger)
                document.operations[_operation_key(method, str(route))] = operation
    return document


# --- Comparing -------------------------------------------------------------


def _type_label(shape: SchemaShape) -> str:
    return f"{shape.type}/{shape.format}" if shape.format else shape.type


def _type_changed(before: SchemaShape, after: SchemaShape) -> bool:
    if not before.type or not after.type:
        return False
    if before.type != after.type:
        return True
    return bool(before.format and after.format and before.format != after.format)


def _diff_shape(
    path: str, symbol: str, before: SchemaShape, after: SchemaShape, direction: Direction
) -> Iterator[ContractFinding]:
    """Differences inside one schema; ``symbol`` names where it sits."""
    if before.opaque or after.opaque:
        return
    if _type_changed(before, after):
        yield ContractFinding(
            path=path, symbol=symbol, change=ContractChangeKind.SCHEMA_TYPE_CHANGED,
            detail=f"{symbol} changed type from {_type_label(before)} to {_type_label(after)}.",
        )
        return
    if after.enum is not None:
        removed = sorted(set(before.enum or []) - set(after.enum)) if before.enum is not None else []
        added = sorted(set(after.enum) - set(before.enum or [])) if before.enum is not None else []
        if direction is Direction.REQUEST and (removed or before.enum is None):
            what = f"no longer accepts {', '.join(removed)}" if removed else f"now accepts only {', '.join(after.enum)}"
            yield ContractFinding(
                path=path, symbol=symbol, change=ContractChangeKind.ENUM_NARROWED, detail=f"{symbol} {what}."
            )
        if added:
            yield ContractFinding(
                path=path, symbol=symbol, change=ContractChangeKind.ENUM_WIDENED,
                detail=f"{symbol} gained the value(s) {', '.join(added)}.",
            )
    yield from _diff_properties(path, symbol, before, after, direction)
    if before.items is not None and after.items is not None:
        yield from _diff_shape(path, f"{symbol}[]", before.items, after.items, direction)


def _diff_properties(
    path: str, symbol: str, before: SchemaShape, after: SchemaShape, direction: Direction
) -> Iterator[ContractFinding]:
    was_required, now_required = set(before.required), set(after.required)
    for name, shape in after.properties.items():
        field = f"{symbol}.{name}"
        if name not in before.properties:
            if direction is Direction.REQUEST:
                required = name in now_required
                yield ContractFinding(
                    path=path, symbol=field,
                    change=ContractChangeKind.REQUEST_FIELD_REQUIRED_ADDED if required else ContractChangeKind.REQUEST_FIELD_ADDED,
                    detail=f"Request field {field} was added{' as required' if required else ''}.",
                )
            else:
                yield ContractFinding(
                    path=path, symbol=field, change=ContractChangeKind.RESPONSE_FIELD_ADDED,
                    detail=f"Response field {field} was added.",
                )
            continue
        if direction is Direction.REQUEST and name in now_required and name not in was_required:
            yield ContractFinding(
                path=path, symbol=field, change=ContractChangeKind.REQUEST_FIELD_REQUIRED_ADDED,
                detail=f"Request field {field} became required.",
            )
        if direction is Direction.RESPONSE and name in was_required and name not in now_required:
            yield ContractFinding(
                path=path, symbol=field, change=ContractChangeKind.RESPONSE_FIELD_REMOVED,
                detail=f"Response field {field} is no longer required, so clients may stop receiving it.",
            )
        yield from _diff_shape(path, field, before.properties[name], shape, direction)
    if direction is Direction.RESPONSE:
        for name in before.properties:
            if name not in after.properties:
                yield ContractFinding(
                    path=path, symbol=f"{symbol}.{name}", change=ContractChangeKind.RESPONSE_FIELD_REMOVED,
                    detail=f"Response field {symbol}.{name} was removed.",
                )


def _success(status: str) -> bool:
    return status.startswith("2") or status == "default"


def _diff_operation(path: str, before: OpenApiOperation, after: OpenApiOperation) -> Iterator[ContractFinding]:
    label = after.label
    for key, parameter in after.parameters.items():
        old = before.parameters.get(key)
        where = f"{label} {parameter.location} parameter {parameter.name}"
        if old is None or (parameter.required and not old.required):
            if old is None and key.startswith("path:"):
                continue  # a new path segment changes the operation's key, so it can't happen here
            required = parameter.required
            yield ContractFinding(
                path=path, symbol=where,
                change=ContractChangeKind.PARAMETER_REQUIRED_ADDED if required else ContractChangeKind.PARAMETER_ADDED,
                detail=(
                    f"{where} became required." if old is not None
                    else f"{where} was added{' as required' if required else ''}."
                ),
            )
        if old is not None:
            yield from _diff_shape(path, where, old.schema_shape, parameter.schema_shape, Direction.REQUEST)
    for key, parameter in before.parameters.items():
        if key not in after.parameters:
            yield ContractFinding(
                path=path, symbol=f"{label} {parameter.location} parameter {parameter.name}",
                change=ContractChangeKind.PARAMETER_REMOVED,
                detail=f"{label} {parameter.location} parameter {parameter.name} was removed.",
            )
    if after.request is not None and after.request_required and (before.request is None or not before.request_required):
        yield ContractFinding(
            path=path, symbol=f"{label} request body", change=ContractChangeKind.REQUEST_BODY_REQUIRED,
            detail=f"{label} now requires a request body.",
        )
    if before.request is not None and after.request is not None:
        yield from _diff_shape(path, f"{label} request", before.request, after.request, Direction.REQUEST)
    for status, shape in before.responses.items():
        if status not in after.responses:
            if _success(status):
                yield ContractFinding(
                    path=path, symbol=f"{label} {status}", change=ContractChangeKind.RESPONSE_REMOVED,
                    detail=f"{label} no longer returns its {status} response.",
                )
            continue
        new = after.responses[status]
        if shape is not None and new is not None:
            yield from _diff_shape(path, f"{label} {status} response", shape, new, Direction.RESPONSE)
    for status in after.responses:
        if status not in before.responses:
            yield ContractFinding(
                path=path, symbol=f"{label} {status}", change=ContractChangeKind.RESPONSE_ADDED,
                detail=f"{label} gained a {status} response.",
            )


def diff_openapi(path: str, base: OpenApiDocument | None, head: OpenApiDocument | None) -> list[ContractFinding]:
    """Every difference between ``base`` and ``head`` of the document at
    ``path``; ``base`` None is a new file, ``head`` None a removed one."""
    if base is None and head is None:
        return []
    if base is None:
        return [ContractFinding(path=path, change=ContractChangeKind.FILE_ADDED, detail=f"{path} was added.")]
    if head is None:
        return [ContractFinding(path=path, change=ContractChangeKind.FILE_REMOVED, detail=f"{path} was removed.")]
    findings: list[ContractFinding] = []
    added = {key: op for key, op in head.operations.items() if key not in base.operations}
    renamed_to: set[str] = set()
    for key, operation in base.operations.items():
        if key in head.operations:
            findings.extend(_diff_operation(path, operation, head.operations[key]))
            continue
        target = next(
            (k for k, op in added.items() if operation.operation_id and op.operation_id == operation.operation_id),
            None,
        )
        if target is not None:
            renamed_to.add(target)
            findings.append(
                ContractFinding(
                    path=path, symbol=operation.label, change=ContractChangeKind.OPERATION_RENAMED,
                    detail=f"{operation.label} ({operation.operation_id}) moved to {added[target].label}.",
                )
            )
        else:
            findings.append(
                ContractFinding(
                    path=path, symbol=operation.label, change=ContractChangeKind.OPERATION_REMOVED,
                    detail=f"{operation.label} was removed.",
                )
            )
    for key, operation in added.items():
        if key not in renamed_to:
            findings.append(
                ContractFinding(
                    path=path, symbol=operation.label, change=ContractChangeKind.OPERATION_ADDED,
                    detail=f"{operation.label} was added.",
                )
            )
    return findings
