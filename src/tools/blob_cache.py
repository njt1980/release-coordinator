"""Content-addressed cache of repository files, keyed by git object id.

A git blob id is the hash of the file's content, so a cached entry can
never be stale: a changed file has a new id. Re-scanning a repository then
reads only the files that changed since the last scan, which keeps daily
baseline refreshes of large estates within Azure DevOps rate limits.

The cache is an optimization only: any read or write problem is logged and
treated as a miss, never as a scan failure.
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Git object ids: SHA-1 (40 hex) or SHA-256 (64 hex) repositories.
_OBJECT_ID: Final[re.Pattern[str]] = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# Larger files are read but not kept: source and config files rarely are.
MAX_CACHED_BYTES: Final[int] = 2 * 1024 * 1024


def is_object_id(value: object) -> bool:
    """Whether ``value`` is a lower-case git object id."""
    return isinstance(value, str) and bool(_OBJECT_ID.match(value))


class BlobCache:
    """File contents stored under ``root/<id[:2]>/<id>`` as UTF-8 text."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, object_id: str) -> Path:
        return self._root / object_id[:2] / object_id

    def get(self, object_id: str) -> str | None:
        """Return the cached content of a blob, or None on a miss."""
        if not is_object_id(object_id):
            return None
        try:
            return self._path(object_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as error:
            logger.warning("Blob cache read failed for %s: %s", object_id, error)
            return None

    def put(self, object_id: str, content: str) -> None:
        """Store a blob's content; ignored for invalid ids and large files."""
        if not is_object_id(object_id):
            return
        data = content.encode("utf-8")
        if len(data) > MAX_CACHED_BYTES:
            return
        target = self._path(object_id)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename, so a concurrent reader never sees a partial file.
            descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                os.replace(temporary, target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as error:
            logger.warning("Blob cache write failed for %s: %s", object_id, error)
