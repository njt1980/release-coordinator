"""Shared configuration constants for the release coordinator.

Deployment settings come from environment variables (read once, at import):

- ``RC_DATA_DIR``: root of the data the server keeps (baselines, policies,
  review answers, assessment archive). Unset: the repository root, and no
  assessment archive (assessments live only in memory, as in the pilot).
- ``RC_TRUST_PRINCIPAL_HEADER=1``: the server runs behind Azure Container
  Apps / App Service authentication, which sets the signed-in user's name in
  ``X-MS-CLIENT-PRINCIPAL-NAME`` and strips client-supplied copies. Never
  set it where clients reach the server directly.
- ``RC_ADO_ORG_ALLOWLIST``: comma-separated Azure DevOps organization URLs
  the server may read; unset allows any (local pilot).
- ``RC_DISABLE_RAW_WEBHOOK=1``: refuse ``POST /api/v1/webhooks/pr-assessment``
  (caller-supplied changed files and diff); PR intake reads them from ADO.
- ``RC_FRONTEND_DIST``: directory of the built frontend to serve at ``/``.
- ``RC_SQLITE_VFS``: SQLite VFS for the review database; ``unix-dotfile``
  where the data directory is an SMB share (Azure Files), whose byte-range
  locks SQLite can't take. Unset: SQLite's default locking.
- ``RC_ADO_SCAN_CONCURRENCY``: repositories scanned at once during
  discovery (1 to 8, default 4). With ``RC_DATA_DIR`` set, scanned files
  are also kept in a content-addressed cache there (``cache/ado-blobs``),
  so a rescan reads only files that changed.
"""

import os
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


DATA_DIR_SET: Final[bool] = bool(os.environ.get("RC_DATA_DIR", "").strip())
DATA_ROOT: Final[Path] = (
    Path(os.environ["RC_DATA_DIR"].strip()) if DATA_DIR_SET else REPOSITORY_ROOT
)
TRUST_PRINCIPAL_HEADER: Final[bool] = _flag("RC_TRUST_PRINCIPAL_HEADER")
ADO_ORG_ALLOWLIST: Final[frozenset[str]] = frozenset(
    url.strip().rstrip("/").lower()
    for url in os.environ.get("RC_ADO_ORG_ALLOWLIST", "").split(",")
    if url.strip()
)
DISABLE_RAW_WEBHOOK: Final[bool] = _flag("RC_DISABLE_RAW_WEBHOOK")
FRONTEND_DIST: Final[str] = os.environ.get("RC_FRONTEND_DIST", "").strip()
SQLITE_VFS: Final[str] = os.environ.get("RC_SQLITE_VFS", "").strip()

# Directory (relative to the data root) holding finished PR assessments as
# ``{job_id}.json``; used only when RC_DATA_DIR is set.
ASSESSMENTS_DIR: Final[str] = "assessments"

# Directory (relative to the project root) holding versioned discovery
# baselines as ``{tenant}/{project_id}/{snapshot_id}.json``. Replaces the
# single shared ``knowledge_graph.json`` file, which is no longer read.
BASELINES_DIR: Final[str] = "baselines"

# Directory (relative to the project root) holding per-project release
# policies as ``{tenant}/{project_id}.yaml``. Policies are checked in; a
# project without one is never approved.
POLICIES_DIR: Final[str] = "policies"

# Directory (relative to the project root) and file name of the SQLite
# database holding project dependency review answers (not checked in).
REVIEWS_DIR: Final[str] = "reviews"
REVIEWS_DB_NAME: Final[str] = "reviews.sqlite3"

# Upper bound on a policy's ``max_baseline_age_hours``: a project may demand
# fresher baselines, never older ones.
MAX_BASELINE_AGE_HOURS: Final[int] = 24

# Scope used when a caller does not name a tenant or project.
DEFAULT_TENANT: Final[str] = "default"
DEFAULT_PROJECT_ID: Final[str] = "default"

# Claude model used for Day-Two release risk reasoning. Override with the
# RISK_REASONER_MODEL environment variable.
DEFAULT_RISK_REASONER_MODEL: Final[str] = "claude-opus-5-5"

# Telemetry lookback when the project policy doesn't set
# runtime_lookback_days (or there is no policy), and its upper bound.
DEFAULT_RUNTIME_LOOKBACK_DAYS: Final[int] = 7

MAX_RUNTIME_LOOKBACK_DAYS: Final[int] = 30

# Telemetry callee resolution by request join reads at most this many days
# (joining every dependency row with every request row of a long lookback
# is the most expensive query we run), and leaves out the last minutes,
# because request rows are ingested minutes after the dependency rows that
# caused them. Older edges are still found by their target address.
REQUEST_JOIN_DAYS: Final[int] = 2
REQUEST_JOIN_LAG_MINUTES: Final[int] = 10

# Repositories scanned at once during discovery (RC_ADO_SCAN_CONCURRENCY,
# 1 to 8). Azure DevOps rate-limits per identity, so more is not always faster.
ADO_SCAN_CONCURRENCY: Final[int] = min(
    8, max(1, int(os.environ.get("RC_ADO_SCAN_CONCURRENCY", "").strip() or "4"))
)

# Directory (relative to the data root) of the content-addressed cache of
# repository files (see ``tools.blob_cache``); used only when RC_DATA_DIR is
# set, so local runs never write into the source tree.
ADO_BLOB_CACHE_DIR: Final[str] = "cache/ado-blobs"

# How old the newest live telemetry row may be for live evidence to count.
DEFAULT_MAX_TELEMETRY_AGE_MINUTES: Final[int] = 60
