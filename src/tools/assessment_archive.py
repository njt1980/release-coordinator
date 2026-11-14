"""Finished PR assessments kept on disk, so they outlive the server process.

Each record is one JSON file ``{root}/{job_id}.json``
written to a hidden temporary file and renamed into place (readers never
see half a record). The caller owns the record's schema: this module
stores and returns text, and the caller validates it when read back.
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Final

from src.core import config

logger = logging.getLogger(__name__)

_JOB_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SUFFIX: Final[str] = ".json"


class AssessmentArchive:
    """Directory of finished assessment records."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, job_id: str, record_json: str) -> None:
        """Write (or replace) the record of ``job_id``.

        Raises:
            ValueError: if ``job_id`` isn't a safe file name.
            OSError: if the file can't be written.
        """
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError(f"unsafe job ID: {job_id!r}")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.root, prefix=".", suffix=".tmp", delete=False
        ) as handle:
            handle.write(record_json)
            temp_path = Path(handle.name)
        os.replace(temp_path, self.root / f"{job_id}{_SUFFIX}")

    def load_all(self) -> list[tuple[str, str]]:
        """Every record as ``(job_id, json text)``, oldest first; unreadable
        files are logged and left out."""
        if not self.root.is_dir():
            return []
        paths = sorted(
            (p for p in self.root.glob(f"*{_SUFFIX}") if _JOB_ID_PATTERN.fullmatch(p.stem)),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        records: list[tuple[str, str]] = []
        for path in paths:
            try:
                records.append((path.stem, path.read_text(encoding="utf-8")))
            except OSError as error:
                logger.warning("Assessment record %s unreadable: %s", path, error)
        return records


def default_archive() -> AssessmentArchive | None:
    """The archive under ``RC_DATA_DIR``, or None when it isn't set (the
    local pilot keeps assessments in memory only)."""
    if not config.DATA_DIR_SET:
        return None
    return AssessmentArchive(config.DATA_ROOT / config.ASSESSMENTS_DIR)
