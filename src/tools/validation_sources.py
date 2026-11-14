"""Read the files a validation bundle quotes: the tool version, the stored
discovery benchmark results, and the intended-use statement.
File digests are SHA-256 of the file's bytes.

Each reader returns a typed result that says when its source is missing
or unreadable instead of raising, so a bundle can still be exported and
shows the gap. Paths are relative to ``root`` (the repository root; the
showcase image copies the files to the same paths).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from pydantic import ValidationError

from src.core import config
from src.core.validation import BenchmarkApp, BenchmarkResults, BenchmarkScores, IntendedUse, ToolVersion


def read_tool_version(root: Path = config.REPOSITORY_ROOT) -> ToolVersion:
    """``RC_TOOL_VERSION`` if set, else the git commit checked out at ``root``."""
    if config.TOOL_VERSION:
        return ToolVersion(version=config.TOOL_VERSION, source="environment")
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ToolVersion(version="unknown", source="unknown")
    commit = completed.stdout.strip()
    return ToolVersion(version=commit, source="git") if commit else ToolVersion(version="unknown", source="unknown")


def _splits(apps_dir: Path) -> dict[str, str]:
    splits: dict[str, str] = {}
    if not apps_dir.is_dir():
        return splits
    for path in sorted(apps_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            split = data.get("split", "tuned")  # The benchmark's default split.
            splits[path.stem] = split if isinstance(split, str) else "unknown"
    return splits


def read_benchmark(root: Path = config.REPOSITORY_ROOT) -> BenchmarkResults:
    """The stored benchmark baseline, with each app's split when the app files are present."""
    source = config.BENCHMARK_BASELINE_FILE
    try:
        raw = (root / source).read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        splits = _splits(root / config.BENCHMARK_APPS_DIR)
        apps_raw = data.get("apps") or {}
        if not isinstance(apps_raw, dict):
            raise ValueError("'apps' is not an object")
        apps = [
            BenchmarkApp(
                name=name,
                split=splits.get(name, "unknown"),
                scores=BenchmarkScores.model_validate(scores),
            )
            for name, scores in sorted(apps_raw.items())
        ]
        pooled = BenchmarkScores.model_validate(data["pooled"]) if data.get("pooled") else None
        held_out = (
            BenchmarkScores.model_validate(data["pooled_held_out"]) if data.get("pooled_held_out") else None
        )
    except (OSError, ValueError, ValidationError) as error:
        return BenchmarkResults(available=False, source_file=source, problem=str(error))
    return BenchmarkResults(
        available=True,
        source_file=source,
        sha256=hashlib.sha256(raw).hexdigest(),
        pooled=pooled,
        pooled_held_out=held_out,
        apps=apps,
    )


def read_intended_use(root: Path = config.REPOSITORY_ROOT) -> IntendedUse:
    """The intended-use statement (docs/intended-use.md) as shipped with this build."""
    source = config.INTENDED_USE_FILE
    try:
        raw = (root / source).read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return IntendedUse(available=False, source_file=source, problem=str(error))
    return IntendedUse(available=True, source_file=source, sha256=hashlib.sha256(raw).hexdigest(), text=text)
