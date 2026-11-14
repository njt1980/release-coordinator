"""Versioned, per-project storage for discovery baselines.

Each successful discovery run is written as an immutable snapshot at
``{root}/{tenant}/{project_id}/{snapshot_id}.json``. Snapshot IDs start with a
UTC timestamp, so lexical order is chronological, and end with a random
suffix, so concurrent runs never write the same file. Writes go to a hidden
temporary file that is atomically renamed into place, so readers never see a
half-written snapshot. Readers pick the newest snapshot that parses and
records a complete discovery; older good snapshots are never overwritten.
Every service FQURI in a snapshot must name the snapshot's tenant. New
snapshots must record their scan context (environment, cluster and scanned
revisions); snapshots written before that was recorded still load, and the
release coordinator refuses to approve against them.
"""

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter

from src.core import config
from src.core.models import (
    DiscoveryIncompleteError,
    KnowledgeGraph,
    RunEvent,
    ensure_complete_baseline,
)

logger = logging.getLogger(__name__)

# One path segment: no separators, no leading dot, so ``..`` cannot escape.
PATH_SEGMENT_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SNAPSHOT_SUFFIX: Final[str] = ".json"
_RUN_LOG_DIR: Final[str] = "run-logs"
_RUN_LOG_ADAPTER: Final[TypeAdapter[list[RunEvent]]] = TypeAdapter(list[RunEvent])
_SNAPSHOT_TIME_FORMAT: Final[str] = "%Y%m%dT%H%M%S%fZ"


class BaselineScope(BaseModel):
    """The tenant and project a baseline belongs to."""

    tenant: str = Field(default=config.DEFAULT_TENANT, pattern=PATH_SEGMENT_PATTERN)
    project_id: str = Field(
        default=config.DEFAULT_PROJECT_ID, pattern=PATH_SEGMENT_PATTERN
    )


class StoredBaseline(BaseModel):
    """A complete baseline snapshot together with where it was stored."""

    scope: BaselineScope
    snapshot_id: str = Field(pattern=PATH_SEGMENT_PATTERN)
    graph: KnowledgeGraph

    @property
    def demo_mode(self) -> bool:
        """Whether this baseline relies on mock fixture evidence."""
        return self.graph.uses_mock_data

    @property
    def created_at(self) -> datetime | None:
        """When the snapshot was written (see ``snapshot_created_at``)."""
        return snapshot_created_at(self.snapshot_id)


def snapshot_created_at(snapshot_id: str) -> datetime | None:
    """Return the UTC time encoded in ``snapshot_id``, or None.

    IDs from ``new_snapshot_id`` start with that time; any other ID (for
    example a hand-named file) has no known age and returns None.
    """
    stamp = snapshot_id.split("-", 1)[0]
    try:
        moment = datetime.strptime(stamp, _SNAPSHOT_TIME_FORMAT)
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc)


def ensure_scope_identities(scope: BaselineScope, graph: KnowledgeGraph) -> None:
    """Raise unless every service FQURI in ``graph`` belongs to ``scope``'s tenant.

    Raises:
        ValueError: naming the services whose tenant segment differs.
    """
    foreign = [
        service.service_id
        for service in graph.services
        if service.identity.tenant != scope.tenant
    ]
    if foreign:
        raise ValueError(
            f"services do not belong to tenant {scope.tenant!r}: {foreign}"
        )


def new_snapshot_id(now: datetime | None = None) -> str:
    """Return a sortable, collision-resistant snapshot ID."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{moment.strftime(_SNAPSHOT_TIME_FORMAT)}-{uuid4().hex[:8]}"


class BaselineStore:
    """Filesystem store of versioned baselines, isolated per tenant/project."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def project_dir(self, scope: BaselineScope) -> Path:
        """Return the directory holding ``scope``'s snapshots."""
        return self._root / scope.tenant / scope.project_id

    def write(self, scope: BaselineScope, graph: KnowledgeGraph) -> StoredBaseline:
        """Persist ``graph`` as a new snapshot and return it.

        Raises:
            DiscoveryIncompleteError: if ``graph`` does not record a complete
                discovery; nothing is written.
            ValueError: if a service FQURI names another tenant, or the
                graph does not record its scan context; nothing is written.
        """
        ensure_complete_baseline(graph)
        ensure_scope_identities(scope, graph)
        if graph.scan is None:
            raise ValueError("baseline must record its scan context")
        stored = StoredBaseline(scope=scope, snapshot_id=new_snapshot_id(), graph=graph)
        directory = self.project_dir(scope)
        directory.mkdir(parents=True, exist_ok=True)
        payload = graph.model_dump_json(indent=2)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{stored.snapshot_id}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        try:
            os.replace(temp_path, directory / f"{stored.snapshot_id}{_SNAPSHOT_SUFFIX}")
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise
        return stored

    def read(self, scope: BaselineScope, snapshot_id: str) -> StoredBaseline:
        """Load one snapshot, requiring it to record a complete discovery.

        Raises:
            FileNotFoundError: if the snapshot does not exist.
            ValueError: if ``snapshot_id`` is not a single safe path segment.
            pydantic.ValidationError: if the file does not hold a valid
                knowledge graph (including pre-FQURI snapshots).
            DiscoveryIncompleteError: if the snapshot's discovery is incomplete.
        """
        if not re.fullmatch(PATH_SEGMENT_PATTERN, snapshot_id):
            raise ValueError(f"invalid snapshot ID: {snapshot_id!r}")
        path = self.project_dir(scope) / f"{snapshot_id}{_SNAPSHOT_SUFFIX}"
        graph = KnowledgeGraph.model_validate_json(path.read_text(encoding="utf-8"))
        ensure_complete_baseline(graph)
        ensure_scope_identities(scope, graph)
        return StoredBaseline(scope=scope, snapshot_id=snapshot_id, graph=graph)

    def run_log_path(self, scope: BaselineScope, snapshot_id: str) -> Path:
        """Where the log of the run that built ``snapshot_id`` is kept.

        A subdirectory, so the snapshot listing (``*.json`` in the project
        directory) never mistakes a log for a snapshot.

        Raises:
            ValueError: if ``snapshot_id`` is not a single safe path segment.
        """
        if not re.fullmatch(PATH_SEGMENT_PATTERN, snapshot_id):
            raise ValueError(f"invalid snapshot ID: {snapshot_id!r}")
        return self.project_dir(scope) / _RUN_LOG_DIR / f"{snapshot_id}{_SNAPSHOT_SUFFIX}"

    def write_run_log(
        self, scope: BaselineScope, snapshot_id: str, events: list[RunEvent]
    ) -> None:
        """Keep the onboarding run's log next to the snapshot it built."""
        path = self.run_log_path(scope, snapshot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _RUN_LOG_ADAPTER.dump_json(events, indent=2).decode("utf-8")
        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)

    def read_run_log(self, scope: BaselineScope, snapshot_id: str) -> list[RunEvent] | None:
        """Return the log of the run that built ``snapshot_id``, if kept."""
        path = self.run_log_path(scope, snapshot_id)
        if not path.is_file():
            return None
        return _RUN_LOG_ADAPTER.validate_json(path.read_text(encoding="utf-8"))

    def snapshot_ids(self, scope: BaselineScope) -> list[str]:
        """Return the scope's snapshot IDs, newest first."""
        directory = self.project_dir(scope)
        if not directory.is_dir():
            return []
        return sorted(
            (
                path.name[: -len(_SNAPSHOT_SUFFIX)]
                for path in directory.glob(f"*{_SNAPSHOT_SUFFIX}")
                if not path.name.startswith(".")
            ),
            reverse=True,
        )

    def latest(self, scope: BaselineScope) -> StoredBaseline | None:
        """Return the newest complete snapshot for ``scope``, or None.

        Snapshots that cannot be read, fail validation, or record incomplete
        discovery are logged and skipped, so a bad file never hides an older
        good baseline.
        """
        for snapshot_id in self.snapshot_ids(scope):
            try:
                return self.read(scope, snapshot_id)
            except (OSError, ValueError, DiscoveryIncompleteError) as error:
                logger.warning(
                    "Skipping unusable baseline snapshot %s/%s/%s: %s",
                    scope.tenant,
                    scope.project_id,
                    snapshot_id,
                    error,
                )
        return None


DEFAULT_BASELINES_ROOT: Final[Path] = (
    config.DATA_ROOT / config.BASELINES_DIR
)


def default_store() -> BaselineStore:
    """Return the store rooted at the project's ``baselines`` directory."""
    return BaselineStore(DEFAULT_BASELINES_ROOT)
