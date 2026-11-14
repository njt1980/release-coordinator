"""Pinned, cached checkouts of the benchmark's public repositories.

Each repository is fetched once at its exact commit (shallow, no history)
into ``<cache>/<name>@<sha>``; later runs reuse it offline. Nothing is
vendored into this repository, and checked-out content is only read, never
built or executed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .models import RepoPin

DEFAULT_CACHE = Path(
    os.environ.get("RC_BENCH_CACHE", Path.home() / ".cache" / "rc-bench")
)


class CheckoutError(RuntimeError):
    """A pinned repository is not in the cache and could not be fetched."""


def checkout_dir(pin: RepoPin, cache: Path = DEFAULT_CACHE) -> Path:
    return cache / f"{pin.name}@{pin.sha}"


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600, check=False
    )
    if result.returncode != 0:
        raise CheckoutError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _is_ready(target: Path, sha: str) -> bool:
    if not (target / ".git").is_dir():
        return False
    try:
        return _git(["rev-parse", "HEAD"], target) == sha
    except CheckoutError:
        return False


def ensure_checkout(pin: RepoPin, cache: Path = DEFAULT_CACHE, offline: bool = False) -> Path:
    """Return a checkout of ``pin``, fetching it unless it is already cached.

    Raises:
        CheckoutError: the checkout is missing and ``offline`` is set, or
            fetching the exact commit failed.
    """
    target = checkout_dir(pin, cache)
    if _is_ready(target, pin.sha):
        return target
    if offline:
        raise CheckoutError(f"{pin.name}@{pin.sha[:12]} is not cached and --offline is set")
    target.mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet"], target)
    _git(["fetch", "--quiet", "--depth", "1", pin.url, pin.sha], target)
    _git(["-c", "advice.detachedHead=false", "checkout", "--quiet", "FETCH_HEAD"], target)
    if not _is_ready(target, pin.sha):
        raise CheckoutError(f"{pin.name}: checked out commit is not {pin.sha}")
    return target
