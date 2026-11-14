"""Canonical hashing shared by the append-only record stores."""

import hashlib
import json


def canonical_json(value: object) -> str:
    """JSON with sorted keys and no whitespace: the form every hash is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: object) -> str:
    """SHA-256 (hex) of ``value``'s canonical JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
