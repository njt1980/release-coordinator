"""What a contract comparison reports, shared by every contract language.

The readers (``core.proto_contract`` for ``.proto`` files,
``core.openapi_contract`` for OpenAPI documents, ``core.message_contract``
for message payload types) each compare a base and a head version and
report ``ContractFinding`` objects. A finding is breaking when its kind is in
``BREAKING_CHANGES``: a consumer or provider built against one version may
fail against the other.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from pydantic import BaseModel, Field


class ContractChangeKind(str, Enum):
    """One kind of difference between two versions of a contract.

    Protocol Buffers: FILE_* to ENUM_VALUE_RENAMED. Message payload types
    reuse MESSAGE_ADDED/REMOVED and FIELD_ADDED/REMOVED/TYPE_CHANGED (a
    property is a field). OpenAPI: OPERATION_* to ENUM_WIDENED.
    """

    FILE_ADDED = "FILE_ADDED"
    FILE_REMOVED = "FILE_REMOVED"
    PACKAGE_CHANGED = "PACKAGE_CHANGED"
    SERVICE_ADDED = "SERVICE_ADDED"
    SERVICE_REMOVED = "SERVICE_REMOVED"
    RPC_ADDED = "RPC_ADDED"
    RPC_REMOVED = "RPC_REMOVED"
    RPC_TYPES_CHANGED = "RPC_TYPES_CHANGED"
    RPC_STREAMING_CHANGED = "RPC_STREAMING_CHANGED"
    MESSAGE_ADDED = "MESSAGE_ADDED"
    MESSAGE_REMOVED = "MESSAGE_REMOVED"
    FIELD_ADDED = "FIELD_ADDED"
    FIELD_REQUIRED_ADDED = "FIELD_REQUIRED_ADDED"
    FIELD_REMOVED = "FIELD_REMOVED"
    FIELD_TYPE_CHANGED = "FIELD_TYPE_CHANGED"
    FIELD_RENAMED = "FIELD_RENAMED"
    FIELD_CARDINALITY_CHANGED = "FIELD_CARDINALITY_CHANGED"
    FIELD_PRESENCE_CHANGED = "FIELD_PRESENCE_CHANGED"
    FIELD_ONEOF_CHANGED = "FIELD_ONEOF_CHANGED"
    ENUM_ADDED = "ENUM_ADDED"
    ENUM_REMOVED = "ENUM_REMOVED"
    ENUM_VALUE_ADDED = "ENUM_VALUE_ADDED"
    ENUM_VALUE_REMOVED = "ENUM_VALUE_REMOVED"
    ENUM_VALUE_RENAMED = "ENUM_VALUE_RENAMED"
    # OpenAPI
    OPERATION_ADDED = "OPERATION_ADDED"
    OPERATION_REMOVED = "OPERATION_REMOVED"
    OPERATION_RENAMED = "OPERATION_RENAMED"
    PARAMETER_ADDED = "PARAMETER_ADDED"
    PARAMETER_REQUIRED_ADDED = "PARAMETER_REQUIRED_ADDED"
    PARAMETER_REMOVED = "PARAMETER_REMOVED"
    REQUEST_BODY_REQUIRED = "REQUEST_BODY_REQUIRED"
    REQUEST_FIELD_ADDED = "REQUEST_FIELD_ADDED"
    REQUEST_FIELD_REQUIRED_ADDED = "REQUEST_FIELD_REQUIRED_ADDED"
    RESPONSE_ADDED = "RESPONSE_ADDED"
    RESPONSE_REMOVED = "RESPONSE_REMOVED"
    RESPONSE_FIELD_ADDED = "RESPONSE_FIELD_ADDED"
    RESPONSE_FIELD_REMOVED = "RESPONSE_FIELD_REMOVED"
    SCHEMA_TYPE_CHANGED = "SCHEMA_TYPE_CHANGED"
    ENUM_NARROWED = "ENUM_NARROWED"
    ENUM_WIDENED = "ENUM_WIDENED"


BREAKING_CHANGES: Final[frozenset[ContractChangeKind]] = frozenset(
    {
        ContractChangeKind.FILE_REMOVED,
        ContractChangeKind.PACKAGE_CHANGED,
        ContractChangeKind.SERVICE_REMOVED,
        ContractChangeKind.RPC_REMOVED,
        ContractChangeKind.RPC_TYPES_CHANGED,
        ContractChangeKind.RPC_STREAMING_CHANGED,
        ContractChangeKind.MESSAGE_REMOVED,
        ContractChangeKind.FIELD_REQUIRED_ADDED,
        ContractChangeKind.FIELD_REMOVED,
        ContractChangeKind.FIELD_TYPE_CHANGED,
        ContractChangeKind.FIELD_RENAMED,
        ContractChangeKind.FIELD_CARDINALITY_CHANGED,
        ContractChangeKind.FIELD_ONEOF_CHANGED,
        ContractChangeKind.ENUM_REMOVED,
        ContractChangeKind.ENUM_VALUE_REMOVED,
        ContractChangeKind.ENUM_VALUE_RENAMED,
        ContractChangeKind.OPERATION_REMOVED,
        ContractChangeKind.OPERATION_RENAMED,
        ContractChangeKind.PARAMETER_REQUIRED_ADDED,
        ContractChangeKind.REQUEST_BODY_REQUIRED,
        ContractChangeKind.REQUEST_FIELD_REQUIRED_ADDED,
        ContractChangeKind.RESPONSE_REMOVED,
        ContractChangeKind.RESPONSE_FIELD_REMOVED,
        ContractChangeKind.SCHEMA_TYPE_CHANGED,
        ContractChangeKind.ENUM_NARROWED,
    }
)


class ContractFinding(BaseModel):
    """One difference between the base and head of a contract file.

    ``path`` is the repository-relative file, ``symbol`` what changed
    (``Message.field``, ``Service.Rpc``, ``GET /orders/{}``...), ``detail``
    the sentence a person reads.
    """

    path: str = Field(min_length=1)
    symbol: str = ""
    change: ContractChangeKind
    detail: str = Field(min_length=1)

    @property
    def breaking(self) -> bool:
        return self.change in BREAKING_CHANGES
