"""Repository files a service's Dockerfile copies into its image.

Monorepos often keep a file several services build from outside every
service directory, a shared ``.proto`` for example, and copy it in with
``COPY ./pb/demo.proto ...`` from a build context at the repository root.
No build-project reference names it, so without this a change to it would
belong to no service. ``copied_files`` resolves a Dockerfile's ``COPY`` and
``ADD`` sources against the repository's files; the scanner then records
the ones outside every service directory as ``SharedFile``s, consumed by
the services whose Dockerfiles copy them (like a shared library's files).

The build context isn't in the Dockerfile, so a source is tried relative to
the repository root and to the Dockerfile's directory. Not followed:
``--from`` copies (from another build stage or image), URLs, variables
(``$X``), and sources that would take in the whole repository or a
directory containing the service itself (``COPY . .`` with a root
context).
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
import shlex
from typing import Final

_INSTRUCTION: Final[re.Pattern[str]] = re.compile(r"^\s*(COPY|ADD)\s+(.*)$", re.IGNORECASE)


def _logical_lines(content: str) -> list[str]:
    """Dockerfile lines with ``\\`` continuations joined and comments dropped."""
    lines: list[str] = []
    current = ""
    for raw in content.splitlines():
        stripped = raw.strip()
        if not current and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        lines.append(current + stripped)
        current = ""
    if current:
        lines.append(current)
    return lines


def copy_sources(content: str) -> list[str]:
    """The source arguments of every ``COPY``/``ADD`` that copies from the build context."""
    sources: list[str] = []
    for line in _logical_lines(content):
        match = _INSTRUCTION.match(line)
        if match is None:
            continue
        rest = match.group(2).strip()
        flags: list[str] = []
        while rest.startswith("--"):
            flag, _, rest = rest.partition(" ")
            flags.append(flag.lower())
            rest = rest.strip()
        if any(flag.startswith("--from") for flag in flags):
            continue
        try:
            arguments = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(arguments, list) or len(arguments) < 2:
            continue
        for source in arguments[:-1]:
            text = str(source)
            if "://" in text or "$" in text:
                continue
            sources.append(text)
    return sources


def _relative(path: str) -> str:
    return path.strip("/")


def _candidates(source: str, dockerfile_directory: str) -> list[str]:
    """The repository paths ``source`` may name (root and Dockerfile-relative contexts)."""
    found: list[str] = []
    for context in ("", dockerfile_directory):
        joined = posixpath.normpath(posixpath.join(context, source.lstrip("/")))
        if joined in (".", "") or joined.startswith(".."):
            continue
        found.append(joined)
    return list(dict.fromkeys(found))


def _contains(directory: str, path: str) -> bool:
    return directory == "" or path == directory or path.startswith(directory + "/")


def copied_files(content: str, dockerfile_path: str, service_root: str, file_paths: list[str]) -> set[str]:
    """Repository files the Dockerfile at ``dockerfile_path`` copies into its image.

    ``file_paths`` are the repository's files (a leading ``/`` is ignored).
    A source matching a file, a directory (every file under it) or a glob
    counts; a source that is, or contains, ``service_root`` is ignored.
    """
    files = sorted({_relative(path) for path in file_paths})
    directory = posixpath.dirname(_relative(dockerfile_path))
    root = _relative(service_root)
    copied: set[str] = set()
    for source in copy_sources(content):
        for candidate in _candidates(source, directory):
            if _contains(candidate, root):
                continue  # the whole repository, or a directory holding the service itself
            if any(character in candidate for character in "*?["):
                copied.update(path for path in files if fnmatch.fnmatch(path, candidate))
            else:
                copied.update(path for path in files if _contains(candidate, path))
    return copied
