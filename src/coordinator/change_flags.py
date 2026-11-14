"""Advisory change flags for one pull request (plan item 4e).

Some changes need a person's attention whatever the dependency graph says:
a database migration can't be rolled back with the deployment, and a
configuration or feature-flag change behaves differently per environment.
This module recognises them from the pull request's changed files:

- Database migrations (DB_MIGRATION): EF Core ``Migrations/*.cs`` (not the
  generated ``*.Designer.cs``), Flyway ``db/migration/V|U|R*__*.sql|java``,
  Liquibase changelogs (``db/changelog/**``, ``*changelog*.xml|yaml|yml|json|sql``).
- Configuration keys (CONFIG_KEYS_CHANGED): ``appsettings*.json`` (.NET),
  ``application*`` / ``bootstrap*`` ``.yml|.yaml|.properties`` (Java), and
  ``.env`` / ``.env.*`` files (Node).
- Feature flags (FEATURE_FLAGS_CHANGED): keys in a feature-flag section of
  those files: ``FeatureManagement`` / ``feature_management`` (.NET);
  ``features``, ``feature``, ``feature-flags``, ``feature-toggles``,
  ``togglz``, ``ff4j`` (Java); ``FEATURE_*`` / ``FF_*`` variables (Node).

A configuration file is read at the base and the head and its key paths
compared (``core.config_keys``); only key names are kept, never values.
When it can't be read (no source control, or a read failed), the keys
come from the diff's changed lines and the flag says it isn't exact.

The flags are advisory: their reason codes don't limit actionability, but
like CONTRACT_BREAKING they keep the release from being approved
automatically; a person decides.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from src.core.config_keys import (
    ConfigFormat,
    ConfigParseError,
    KeyDigests,
    diff_keys,
    keys_in_lines,
    read_keys,
    separator,
)
from src.core.models import ChangeFlag, ChangeFlagKind, ChangeFlags

# Reads a repository file at a commit; None when it doesn't exist there.
FileReader = Callable[[str, str], str | None]

_EF_MIGRATION: Final = re.compile(r"(^|/)migrations/[^/]+\.cs$")
_FLYWAY: Final = re.compile(r"(^|/)db/migration/(.+/)?[vur][^/]*__[^/]+\.(sql|java|kt)$")
_LIQUIBASE_DIR: Final = re.compile(r"(^|/)db/changelog/.+\.(xml|ya?ml|json|sql)$")
_LIQUIBASE_FILE: Final = re.compile(r"(^|/)[^/]*changelog[^/]*\.(xml|ya?ml|json|sql)$")
_APPSETTINGS: Final = re.compile(r"(^|/)appsettings(\.[^/]+)?\.json$")
_SPRING: Final = re.compile(r"(^|/)(application|bootstrap)(-[^/.]+)?\.(ya?ml|properties)$")
_DOTENV: Final = re.compile(r"(^|/)\.env(\.[^/]+)?$")

_DOTNET_FLAG_SECTIONS: Final[frozenset[str]] = frozenset({"featuremanagement", "feature_management"})
_JAVA_FLAG_SECTIONS: Final[frozenset[str]] = frozenset(
    {"features", "feature", "feature-flags", "feature-toggles", "featureflags", "togglz", "ff4j"}
)
_ENV_FLAG_PREFIXES: Final[tuple[str, ...]] = ("FEATURE_", "FF_")
_LISTED: Final[int] = 8


def migration_stack(path: str) -> str | None:
    """The stack whose migration tool ``path`` belongs to, or None."""
    lower = path.lower()
    if _EF_MIGRATION.search(lower) and not lower.endswith(".designer.cs"):
        return "dotnet"
    if _FLYWAY.search(lower) or _LIQUIBASE_DIR.search(lower) or _LIQUIBASE_FILE.search(lower):
        return "java"
    return None


def config_format(path: str) -> tuple[ConfigFormat, str] | None:
    """The format and stack of a recognised configuration file, or None."""
    lower = path.lower()
    if _APPSETTINGS.search(lower):
        return ConfigFormat.JSON, "dotnet"
    match = _SPRING.search(lower)
    if match:
        return (ConfigFormat.PROPERTIES if match.group(4) == "properties" else ConfigFormat.YAML), "java"
    if _DOTENV.search(lower):
        return ConfigFormat.DOTENV, "node"
    return None


def is_feature_flag_key(key: str, fmt: ConfigFormat) -> bool:
    """Whether ``key`` (a key path in ``fmt``) sits in a feature-flag section."""
    if fmt is ConfigFormat.DOTENV:
        return key.upper().startswith(_ENV_FLAG_PREFIXES)
    path = key.split("/", 1)[1] if key.startswith("#") and "/" in key else key
    first = path.split(separator(fmt), 1)[0].lower()
    if fmt is ConfigFormat.JSON:
        return first in _DOTNET_FLAG_SECTIONS
    return first in _JAVA_FLAG_SECTIONS


@dataclass
class DiffFile:
    """One file's section of a unified diff."""

    added_file: bool = False
    plus: list[str] = field(default_factory=list)
    minus: list[str] = field(default_factory=list)


_DIFF_HEADER: Final = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def parse_unified_diff(diff: str) -> dict[str, DiffFile]:
    """Split a unified diff by file (the head path), with its changed lines."""
    files: dict[str, DiffFile] = {}
    current: DiffFile | None = None
    for line in diff.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            current = files.setdefault(header.group(2).lstrip("/"), DiffFile())
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target != "/dev/null":
                path = target[2:] if target.startswith("b/") else target
                current = files.setdefault(path.lstrip("/"), current or DiffFile())
            continue
        if current is None:
            continue
        if line.startswith("--- "):
            if line[4:].strip() == "/dev/null":
                current.added_file = True
        elif line.startswith("new file mode"):
            current.added_file = True
        elif line.startswith("+"):
            current.plus.append(line[1:])
        elif line.startswith("-"):
            current.minus.append(line[1:])
    return files


def _listed(keys: Iterable[str]) -> str:
    items = list(keys)
    shown = ", ".join(items[:_LISTED])
    return shown + (f" and {len(items) - _LISTED} more" if len(items) > _LISTED else "")


def _key_flag(
    kind: ChangeFlagKind,
    path: str,
    stack: str,
    added: list[str],
    removed: list[str],
    changed: list[str],
    exact: bool,
    file_added: bool,
) -> ChangeFlag | None:
    if not (added or removed or changed):
        return None
    what = "feature-flag keys" if kind is ChangeFlagKind.FEATURE_FLAG else "configuration keys"
    parts = [
        f"{label} {_listed(keys)}"
        for label, keys in (("added", added), ("removed", removed), ("changed", changed))
        if keys
    ]
    detail = f"{path}: {what} " + "; ".join(parts)
    if not exact:
        detail += " (from the diff's changed lines; nesting unknown)"
    return ChangeFlag(
        kind=kind,
        path=path,
        stack=stack,
        file_added=file_added,
        added_keys=added,
        removed_keys=removed,
        changed_keys=changed,
        exact=exact,
        detail=detail,
    )


def _config_flags(
    path: str,
    fmt: ConfigFormat,
    stack: str,
    base: KeyDigests,
    head: KeyDigests,
    exact: bool,
    file_added: bool,
) -> list[ChangeFlag]:
    delta = diff_keys(base, head)
    flags: list[ChangeFlag] = []
    for kind, wanted in ((ChangeFlagKind.FEATURE_FLAG, True), (ChangeFlagKind.CONFIG, False)):
        def pick(keys: list[str]) -> list[str]:
            return [key for key in keys if is_feature_flag_key(key, fmt) is wanted]

        flag = _key_flag(
            kind, path, stack, pick(delta.added), pick(delta.removed), pick(delta.changed), exact, file_added
        )
        if flag is not None:
            flags.append(flag)
    return flags


def _from_diff(path: str, fmt: ConfigFormat, stack: str, section: DiffFile | None) -> list[ChangeFlag]:
    """Flags from the diff's changed lines: each key on a line counts as changed."""
    if section is None:
        return []
    plus = keys_in_lines(section.plus, fmt)
    minus = keys_in_lines(section.minus, fmt)
    base = {key: "-" for key in minus}
    head = {key: "+" for key in plus}
    return _config_flags(path, fmt, stack, base, head, False, section.added_file)


def evaluate_change_flags(
    changed_files: Iterable[str],
    diff: str,
    read: FileReader | None,
    base_sha: str,
    head_sha: str,
) -> ChangeFlags:
    """Return the change flags for a pull request.

    ``read`` reads a file at a commit (None without source control); a
    configuration file it can't read or parse falls back to ``diff``.
    """
    sections = parse_unified_diff(diff)
    flags: list[ChangeFlag] = []
    unreadable: dict[str, str] = {}
    for raw in changed_files:
        path = raw.lstrip("/")
        section = sections.get(path)
        stack = migration_stack(path)
        if stack is not None:
            added = section.added_file if section is not None else False
            if read is not None and base_sha and section is None:
                try:
                    added = read(path, base_sha) is None
                except Exception:  # a failed read: added-ness stays unknown
                    added = False
            flags.append(
                ChangeFlag(
                    kind=ChangeFlagKind.MIGRATION,
                    path=path,
                    stack=stack,
                    file_added=added,
                    detail=f"{path}: {'new' if added else 'changed'} database migration",
                )
            )
            continue
        known = config_format(path)
        if known is None:
            continue
        fmt, stack = known
        if read is None or not (base_sha and head_sha):
            flags.extend(_from_diff(path, fmt, stack, section))
            continue
        try:
            base_text = read(path, base_sha)
            head_text = read(path, head_sha)
            base = read_keys(base_text, fmt) if base_text is not None else {}
            head = read_keys(head_text, fmt) if head_text is not None else {}
        except ConfigParseError as error:
            unreadable[path] = f"not understood: {error}"
            flags.extend(_from_diff(path, fmt, stack, section))
            continue
        except Exception as error:  # a failed read, as the source client raised it
            unreadable[path] = f"couldn't be read: {error}"
            flags.extend(_from_diff(path, fmt, stack, section))
            continue
        flags.extend(_config_flags(path, fmt, stack, base, head, True, base_text is None))
    return ChangeFlags(flags=flags, unreadable=unreadable)
