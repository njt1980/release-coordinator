"""Emulate the Kubernetes source from an app's own deployment files.

Live discovery reads each workload's env from the Kubernetes API. Offline,
the benchmark reads the same values from the app's manifests or compose
files: literal values and ConfigMap references are readable, anything else
(Secrets, unknown ConfigMaps, env files) is opaque, as on a real cluster.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .models import EnvSource, EnvSourceKind

_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_0-9]+)(?::?-([^}]*))?\}")


class _ComposeLoader(yaml.SafeLoader):
    """Safe YAML that reads Compose merge tags (``!override``, ``!reset``) as plain values."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_ComposeLoader.add_multi_constructor("!", _untagged)


class WorkloadEnv(BaseModel):
    variables: dict[str, str] = Field(default_factory=dict)
    opaque: list[str] = Field(default_factory=list)
    has_opaque_sources: bool = False
    # Name -> the ConfigMap it is only imported from as a whole (envFrom).
    bulk: dict[str, str] = Field(default_factory=dict)

    def merge(self, other: WorkloadEnv) -> None:
        self.bulk = {k: v for k, v in self.bulk.items() if k not in other.variables} | other.bulk
        self.variables.update(other.variables)
        self.opaque.extend(other.opaque)
        self.has_opaque_sources = self.has_opaque_sources or other.has_opaque_sources


def _yaml_documents(paths: list[Path]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in paths:
        files = sorted(path.rglob("*.y*ml")) if path.is_dir() else [path]
        for file in files:
            try:
                loaded = list(yaml.safe_load_all(file.read_text(encoding="utf-8")))
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
            documents.extend(doc for doc in loaded if isinstance(doc, dict))
    return documents


def _config_maps(documents: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    return {
        doc["metadata"]["name"]: {k: str(v) for k, v in (doc.get("data") or {}).items()}
        for doc in documents
        if doc.get("kind") == "ConfigMap"
    }


def _container_env(container: dict[str, Any], maps: dict[str, dict[str, str]], env: WorkloadEnv) -> None:
    for source in container.get("envFrom") or []:
        name = (source.get("configMapRef") or {}).get("name")
        if name in maps:
            env.variables.update(maps[name])
            env.bulk.update({variable: name for variable in maps[name]})
        else:
            env.has_opaque_sources = True
    for entry in container.get("env") or []:
        env.bulk.pop(entry["name"], None)
        if "value" in entry:
            env.variables[entry["name"]] = str(entry["value"])
            continue
        ref = (entry.get("valueFrom") or {}).get("configMapKeyRef") or {}
        value = maps.get(ref.get("name", ""), {}).get(ref.get("key", ""))
        if value is None:
            env.opaque.append(entry["name"])
        else:
            env.variables[entry["name"]] = value


def k8s_envs(paths: list[Path]) -> dict[str, WorkloadEnv]:
    """Env per workload name from Kubernetes manifests."""
    documents = _yaml_documents(paths)
    maps = _config_maps(documents)
    envs: dict[str, WorkloadEnv] = {}
    for doc in documents:
        if doc.get("kind") not in _WORKLOAD_KINDS:
            continue
        spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
        env = envs.setdefault(doc["metadata"]["name"], WorkloadEnv())
        for container in spec.get("containers") or []:
            _container_env(container, maps, env)
    return envs


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.split(" #")[0].strip()
    return values


def substitute(value: str, defaults: dict[str, str]) -> str:
    """Expand compose ``${VAR}`` / ``${VAR:-default}`` placeholders (bounded depth)."""
    for _ in range(6):
        value = _PLACEHOLDER.sub(lambda m: defaults.get(m.group(1), m.group(2) or ""), value)
    return value


def _compose_items(raw: object) -> list[tuple[str, str | None]]:
    if isinstance(raw, dict):
        return [(str(k), None if v is None else str(v)) for k, v in raw.items()]
    items: list[tuple[str, str | None]] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        key, sep, value = str(entry).partition("=")
        items.append((key, value if sep else None))
    return items


def compose_envs(paths: list[Path], dotenv: Path | None) -> dict[str, WorkloadEnv]:
    """Env per compose service, with ``.env`` defaults substituted."""
    defaults = read_dotenv(dotenv) if dotenv else {}
    defaults = {k: substitute(v, defaults) for k, v in defaults.items()}
    envs: dict[str, WorkloadEnv] = {}
    for path in paths:
        services = (yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}).get("services") or {}
        for name, service in services.items():
            env = envs.setdefault(name, WorkloadEnv())
            if service.get("env_file"):
                env.has_opaque_sources = True
            for key, value in _compose_items(service.get("environment")):
                if value is not None:
                    env.variables[key] = substitute(value, defaults).split("    #")[0].strip()
                elif key in defaults:
                    env.variables[key] = defaults[key]
                else:
                    env.opaque.append(key)
    return envs


def load_envs(
    sources: list[EnvSource], resolve: Callable[[str], Path]
) -> dict[str, WorkloadEnv]:
    """Merge every env source; ``resolve`` maps a source path to a local path."""
    merged: dict[str, WorkloadEnv] = {}
    for source in sources:
        paths = [resolve(p) for p in source.paths]
        if source.type is EnvSourceKind.K8S:
            found = k8s_envs(paths)
        else:
            found = compose_envs(paths, resolve(source.dotenv) if source.dotenv else None)
        for name, env in found.items():
            merged.setdefault(name, WorkloadEnv()).merge(env)
    return merged
