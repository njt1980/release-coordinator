"""Shared configuration constants for the release coordinator."""

from typing import Final

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

# How old the newest live telemetry row may be for live evidence to count.
DEFAULT_MAX_TELEMETRY_AGE_MINUTES: Final[int] = 60
