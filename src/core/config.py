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

# Upper bound on a policy's ``max_baseline_age_hours``: a project may demand
# fresher baselines, never older ones.
MAX_BASELINE_AGE_HOURS: Final[int] = 24

# Scope used when a caller does not name a tenant or project.
DEFAULT_TENANT: Final[str] = "default"
DEFAULT_PROJECT_ID: Final[str] = "default"

# Claude model used for Day-Two release risk reasoning. Override with the
# RISK_REASONER_MODEL environment variable.
DEFAULT_RISK_REASONER_MODEL: Final[str] = "claude-opus-5-5"
