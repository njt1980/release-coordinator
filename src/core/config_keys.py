"""Flatten service configuration files into key paths (plan item 4e).

Each reader turns one file's text into ``{key path: value digest}``. Only a
SHA-256 digest of each value is kept, so a changed value is noticed without
holding the value (it may be a secret). Formats:

- JSON (``appsettings*.json``): nested keys joined with ``:`` as .NET
  configuration does; ``//`` and ``/* */`` comments and trailing commas are
  accepted, as the .NET JSON configuration provider accepts them.
- YAML (``application*.yml``): nested keys joined with ``.`` as Spring does;
  documents after the first are prefixed ``#<n>/`` (profile documents).
- Java properties: ``key=value``, ``key: value`` or ``key value`` lines.
- ``.env`` files: ``KEY=VALUE`` lines, optionally ``export``-ed.

Arrays are indexed (``Servers:0``). Malformed input raises ``ConfigParseError``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final

import yaml  # type: ignore[import-untyped]


class ConfigFormat(str, Enum):
    JSON = "json"
    YAML = "yaml"
    PROPERTIES = "properties"
    DOTENV = "dotenv"


class ConfigParseError(ValueError):
    """The configuration file couldn't be understood."""


KeyDigests = dict[str, str]

_SEPARATORS: Final[dict[ConfigFormat, str]] = {
    ConfigFormat.JSON: ":",
    ConfigFormat.YAML: ".",
    ConfigFormat.PROPERTIES: ".",
    ConfigFormat.DOTENV: "",
}


def separator(fmt: ConfigFormat) -> str:
    """The separator between nested key segments in ``fmt``'s key paths."""
    return _SEPARATORS[fmt]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _flatten(value: object, prefix: str, sep: str, out: KeyDigests) -> None:
    if isinstance(value, Mapping) and value:
        for key, child in value.items():
            _flatten(child, f"{prefix}{sep}{key}" if prefix else str(key), sep, out)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            _flatten(child, f"{prefix}{sep}{index}" if prefix else str(index), sep, out)
    elif prefix:
        out[prefix] = _digest(value)


def strip_json_comments(text: str) -> str:
    """Remove comments and trailing commas outside JSON strings."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
        elif char == '"':
            in_string = True
            out.append(char)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # trailing comma
                continue
            out.append(char)
            i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _json_keys(text: str) -> KeyDigests:
    try:
        data = json.loads(strip_json_comments(text.lstrip("\ufeff")))
    except json.JSONDecodeError as error:
        raise ConfigParseError(f"invalid JSON: {error}") from error
    out: KeyDigests = {}
    _flatten(data, "", ":", out)
    return out


def _yaml_keys(text: str) -> KeyDigests:
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as error:
        raise ConfigParseError(f"invalid YAML: {error}") from error
    out: KeyDigests = {}
    for index, document in enumerate(d for d in documents if d is not None):
        keys: KeyDigests = {}
        _flatten(document, "", ".", keys)
        out.update({(f"#{index}/{key}" if index else key): digest for key, digest in keys.items()})
    return out


_PROPERTY_LINE: Final = re.compile(r"^\s*((?:\\.|[^=:\s\\])+)\s*(?:[=:]\s*|\s+)?(.*)$")
_DOTENV_LINE: Final = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*)$")


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = pending + raw.lstrip() if pending else raw
        if line.endswith("\\") and not line.endswith("\\\\"):
            pending = line[:-1]
            continue
        pending = ""
        lines.append(line)
    if pending:
        lines.append(pending)
    return lines


def _properties_keys(text: str) -> KeyDigests:
    out: KeyDigests = {}
    for line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped[0] in "#!":
            continue
        match = _PROPERTY_LINE.match(line)
        if match:
            out[match.group(1).replace("\\", "")] = _digest(match.group(2).strip())
    return out


def _dotenv_keys(text: str) -> KeyDigests:
    out: KeyDigests = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOTENV_LINE.match(line)
        if match:
            out[match.group(1)] = _digest(match.group(2).strip().strip("'\""))
    return out


_READERS: Final = {
    ConfigFormat.JSON: _json_keys,
    ConfigFormat.YAML: _yaml_keys,
    ConfigFormat.PROPERTIES: _properties_keys,
    ConfigFormat.DOTENV: _dotenv_keys,
}


def read_keys(text: str, fmt: ConfigFormat) -> KeyDigests:
    """Return ``{key path: value digest}`` for one configuration file.

    Raises:
        ConfigParseError: if a JSON or YAML file is malformed.
    """
    return _READERS[fmt](text)


@dataclass(frozen=True)
class KeyDiff:
    """Added, removed and changed key paths between two files."""

    added: list[str]
    removed: list[str]
    changed: list[str]

    @property
    def keys(self) -> list[str]:
        return [*self.added, *self.removed, *self.changed]


def diff_keys(base: KeyDigests, head: KeyDigests) -> KeyDiff:
    return KeyDiff(
        added=sorted(set(head) - set(base)),
        removed=sorted(set(base) - set(head)),
        changed=sorted(key for key in set(base) & set(head) if base[key] != head[key]),
    )


_JSON_KEY: Final = re.compile(r'"((?:\\.|[^"\\])+)"\s*:')
_YAML_KEY: Final = re.compile(r"^\s*(?:-\s+)?([A-Za-z0-9_.\-\[\]]+)\s*:(?:\s|$)")


def keys_in_lines(lines: list[str], fmt: ConfigFormat) -> set[str]:
    """Key names on changed diff lines (no nesting known: a fallback only)."""
    found: set[str] = set()
    for line in lines:
        if fmt is ConfigFormat.JSON:
            found.update(_JSON_KEY.findall(line))
        elif fmt is ConfigFormat.YAML:
            match = _YAML_KEY.match(line)
            if match:
                found.add(match.group(1))
        elif fmt is ConfigFormat.PROPERTIES:
            found.update(_properties_keys(line))
        else:
            found.update(_dotenv_keys(line))
    return found
