"""Shared configuration constants for the release coordinator."""

from typing import Final

KNOWLEDGE_GRAPH_PATH: Final[str] = "knowledge_graph.json"

# Claude model used for Day-Two release risk reasoning. Override with the
# RISK_REASONER_MODEL environment variable.
DEFAULT_RISK_REASONER_MODEL: Final[str] = "claude-opus-5-5"
