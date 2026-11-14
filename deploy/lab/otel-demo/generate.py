"""Generate plain Kubernetes manifests for the OpenTelemetry Demo lab.

The demo publishes Docker Compose files (and a Helm chart elsewhere); the
lab uses plain manifests, as many organizations do. This reads a checkout's
``compose.yaml`` and ``compose.full.yaml`` (Kafka, accounting, fraud
detection) with its ``.env`` and writes one Deployment and one ClusterIP
Service per container, in namespace ``otel-demo``, with the demo's own
environment, so the release coordinator sees the wiring the demo ships.

Left out: the demo's observability stack (its collector is replaced by
``collector.yaml``; Jaeger, Grafana, Prometheus, OpenSearch are not in
these compose files) and ``telemetry-docs``. Images are pinned to
``--version``; the load generator runs one user to stay under the lab's
Application Insights daily cap.

Usage: python deploy/lab/otel-demo/generate.py <checkout> --version 3.1.0 > deploy/lab/otel-demo/demo.yaml
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

NAMESPACE = "otel-demo"
SKIPPED = {"otel-collector", "telemetry-docs"}
OVERRIDES = {"LOCUST_USERS": "1", "LOCUST_SPAWN_RATE": "1"}
# Per-service environment that differs on Kubernetes: Compose resolves a
# service's own name to its container, Kubernetes to the Service's ClusterIP,
# which a process can't bind.
SERVICE_ENV = {
    "kafka": {
        "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
        "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
    },
}
# Ports a container listens on that Compose doesn't publish.
EXTRA_PORTS = {"kafka": [9092]}
# Files the compose services mount, shipped as ConfigMaps:
# service -> (repository file, mount directory, ConfigMap, mount the file alone).
MOUNTED_FILES = {
    "flagd": ("src/flagd/demo.flagd.json", "/etc/flagd", "flagd-config", False),
    "flagd-ui": ("src/flagd/demo.flagd.json", "/app/data", "flagd-config", False),
    "astronomy-db": ("src/postgresql/init.sql", "/docker-entrypoint-initdb.d", "astronomy-db-init", False),
    "product-catalog": ("otel-config.yml", "/", "product-catalog-otel-config", True),
}
_VARIABLE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            # Compose drops an unquoted value's inline comment (" # ...").
            values[key.strip()] = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return values


def expand(text: str, env: dict[str, str]) -> str:
    for _ in range(10):
        new = _VARIABLE.sub(lambda m: env.get(m.group(1), m.group(2) or ""), text)
        if new == text:
            return new
        text = new
    return text


def _memory(limit: str | None) -> str | None:
    if not limit:
        return None
    match = re.fullmatch(r"(\d+)([MG])", str(limit))
    return f"{match.group(1)}{match.group(2)}i" if match else None


def _ports(service: dict[str, Any], env: dict[str, str]) -> list[int]:
    ports: list[int] = []
    for entry in service.get("ports") or []:
        value = expand(str(entry), env).split(":")[-1]
        if value.isdigit():
            ports.append(int(value))
    return list(dict.fromkeys(ports))


def _environment(service: dict[str, Any], env: dict[str, str]) -> dict[str, str]:
    entries = service.get("environment") or []
    if isinstance(entries, dict):
        entries = [f"{k}={v}" for k, v in entries.items()]
    result: dict[str, str] = {}
    for entry in entries:
        if "=" in entry:
            key, value = entry.split("=", 1)
            result[key] = expand(value, env)
        elif entry in env:
            result[entry] = expand(env[entry], env)
    return result


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if key == "environment":
            merged[key] = list(base.get(key) or []) + list(value or [])
        elif key != "depends_on":
            merged[key] = value
    return merged


def manifests(checkout: Path, version: str) -> list[dict[str, Any]]:
    env = read_env(checkout / ".env")
    env.update(OVERRIDES)
    env["DEMO_VERSION"] = version
    services: dict[str, dict[str, Any]] = yaml.safe_load((checkout / "compose.yaml").read_text())["services"]
    for name, extra in yaml.safe_load((checkout / "compose.full.yaml").read_text())["services"].items():
        services[name] = _merge(services.get(name, {}), extra)
    documents: list[dict[str, Any]] = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}]
    written: set[str] = set()
    for name, service in services.items():
        if name in SKIPPED:
            continue
        labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": "opentelemetry-demo"}
        container: dict[str, Any] = {
            "name": name,
            "image": expand(service["image"], env),
            "env": [
                {"name": key, "value": value}
                for key, value in {**_environment(service, env), **SERVICE_ENV.get(name, {})}.items()
            ],
            "resources": {"requests": {"cpu": "10m"}},
        }
        memory = _memory(service.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory"))
        if memory:
            container["resources"]["limits"] = {"memory": memory}
            # Half the limit is reserved, so the demo fits beside the other lab apps.
            amount, unit = int(memory[:-2]), memory[-2:]
            container["resources"]["requests"]["memory"] = f"{max(amount // 2, 1)}{unit}"
        command = service.get("command")
        if command:
            container["args"] = [expand(str(part), env) for part in (shlex.split(command) if isinstance(command, str) else command)]
        ports = list(dict.fromkeys([*_ports(service, env), *EXTRA_PORTS.get(name, [])]))
        if ports:
            container["ports"] = [{"containerPort": port} for port in ports]
        # Kubernetes would inject Docker-link variables for every Service
        # (FLAGD_PORT=tcp://...), overriding the demo's own *_PORT settings.
        pod: dict[str, Any] = {"enableServiceLinks": False, "containers": [container]}
        if name in MOUNTED_FILES:
            path, mount, config_map, single = MOUNTED_FILES[name]
            file_name = Path(path).name
            if config_map not in written:
                written.add(config_map)
                documents.append(
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": config_map, "namespace": NAMESPACE},
                        "data": {file_name: (checkout / path).read_text(encoding="utf-8")},
                    }
                )
            volume_mount: dict[str, Any] = {"name": "files", "mountPath": mount}
            if single:
                volume_mount = {"name": "files", "mountPath": f"{mount.rstrip('/')}/{file_name}", "subPath": file_name}
            container["volumeMounts"] = [volume_mount]
            pod["volumes"] = [{"name": "files", "configMap": {"name": config_map}}]
        elif service.get("volumes"):
            print(f"warning: {name} mounts {service['volumes']} (not shipped)", file=sys.stderr)
        documents.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                    "template": {"metadata": {"labels": labels}, "spec": pod},
                },
            }
        )
        if ports:
            documents.append(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
                    "spec": {
                        "selector": {"app.kubernetes.io/name": name},
                        "ports": [{"name": f"p{port}", "port": port, "targetPort": port} for port in ports],
                    },
                }
            )
    return documents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    print(f"# Generated by deploy/lab/otel-demo/generate.py from OpenTelemetry Demo {arguments.version}; do not edit.")
    print(yaml.safe_dump_all(manifests(arguments.checkout, arguments.version), sort_keys=False))


if __name__ == "__main__":
    main()
