"""Deterministic grouping keys for evidence issues.

Issues with one cause share a ``root_key`` (see ``EvidenceIssue``), so a
briefing can show "OTEL_EXPORTER_OTLP_ENDPOINT unset in 9 services" as one
question instead of nine lines. Keys are computed where issues are created,
from structured facts, never by parsing their text.
"""

import hashlib
from collections.abc import Iterable

from src.core.service_ids import host_of, parse_env_var_reference


def unresolved_key(reference: str, node: str) -> str:
    """Key for an unresolved entity: an environment variable groups by name
    across services, an address by host, anything else by itself."""
    parsed = parse_env_var_reference(reference) if reference else None
    if parsed is not None:
        return f"env:{parsed[1].lower()}"
    if reference:
        host = host_of(reference)
        if host:
            return f"host:{host}"
    return f"entity:{node}"


def files_key(repository: str, paths: Iterable[str]) -> str:
    """Key for a set of files not read or understood: services sharing the
    exact same files (a shared library's) share it."""
    joined = "\n".join(sorted(set(paths)))
    digest = hashlib.sha256(f"{repository}\n{joined}".encode()).hexdigest()[:16]
    return f"unscanned:{repository}:{digest}"


def identity_key(name: str) -> str:
    return f"identity:{name.lower()}"


def channel_key(channel_id: str) -> str:
    """Key for a message channel's finding: one question per channel."""
    return channel_id
