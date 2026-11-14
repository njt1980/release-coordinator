"""What the cluster runs for each service: images and the deploying pipeline (T1c)."""

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from src.core.models import (
    DeployedWorkload,
    DeployingPipeline,
    ServiceNode,
    merge_deployed_workloads,
)
from src.tools.k8s_extractor import KubernetesExtractor
from tests.test_coverage_and_shape import CONTEXT, _id, _items, _meta, _service

PIPELINE_ANNOTATIONS = {
    "azure-pipelines/org": "https://dev.azure.com/contoso",
    "azure-pipelines/project": "Shop",
    "azure-pipelines/pipeline": "orders-cd",
    "azure-pipelines/pipelineId": "42",
    "azure-pipelines/execution": "1234",
    "azure-pipelines/executionuri": "https://dev.azure.com/contoso/Shop/_build/results?buildId=1234",
    "azure-pipelines/jobName": "Deploy",
    "unrelated/annotation": "x",
}


def _workload(
    name: str,
    labels: dict[str, str],
    images: list[str],
    annotations: dict[str, str] | None = None,
    template_annotations: dict[str, str] | None = None,
) -> Any:
    def meta(meta_name: str, meta_labels: dict[str, str] | None, values: dict[str, str] | None) -> Any:
        base = _meta(meta_name, meta_labels)
        base.annotations = values
        return base

    spec = SimpleNamespace(
        containers=[SimpleNamespace(name=f"c{i}", image=image, env=None, env_from=None) for i, image in enumerate(images)]
    )
    template = SimpleNamespace(metadata=meta("", labels, template_annotations), spec=spec)
    return SimpleNamespace(metadata=meta(name, None, annotations), spec=SimpleNamespace(template=template))


def _extractor(deployments: list[Any], services: list[Any]) -> KubernetesExtractor:
    core, apps, batch = MagicMock(), MagicMock(), MagicMock()
    core.list_namespaced_service.return_value = _items(*services)
    core.list_namespaced_config_map.return_value = _items()
    core.list_namespaced_pod.return_value = _items()
    apps.list_namespaced_deployment.return_value = _items(*deployments)
    for empty in (
        apps.list_namespaced_stateful_set,
        apps.list_namespaced_daemon_set,
        apps.list_namespaced_replica_set,
        batch.list_namespaced_cron_job,
        batch.list_namespaced_job,
    ):
        empty.return_value = _items()
    extractor = KubernetesExtractor.__new__(KubernetesExtractor)
    extractor._core_v1_api = core  # type: ignore[assignment]
    extractor._apps_v1_api = apps  # type: ignore[assignment]
    extractor._batch_v1_api = batch  # type: ignore[assignment]
    extractor._api_server = "https://aks-1.example.azmk8s.io:443"
    return extractor


class DeployedWorkloadTests(unittest.TestCase):
    def setUp(self) -> None:
        extractor = _extractor(
            [
                _workload(
                    "orders",
                    {"app": "orders"},
                    ["acr.io/orders:1.4.2", "acr.io/sidecar:2"],
                    annotations=PIPELINE_ANNOTATIONS,
                ),
                # Pipeline annotations on the pod template only still count.
                _workload(
                    "orders-canary",
                    {"app": "orders"},
                    ["acr.io/orders:1.5.0-rc1"],
                    template_annotations={"azure-pipelines/pipeline": "orders-canary"},
                ),
                # Deployed by something else (Helm, Flux, kubectl): images only.
                _workload("worker", {"app": "worker"}, ["acr.io/worker@sha256:abc"]),
            ],
            [_service("orders", {"app": "orders"}), _service("legacy", None)],
        )
        (topology,) = extractor.get_namespace_topology("shop", CONTEXT).items
        self.services = {s.service_id: s for s in topology.services}

    def test_service_records_every_selected_workload_with_images_and_pipeline(self) -> None:
        self.assertEqual(
            self.services[_id("orders")].deployed_workloads,
            [
                DeployedWorkload(
                    workload="deployment/orders",
                    images=["acr.io/orders:1.4.2", "acr.io/sidecar:2"],
                    pipeline=DeployingPipeline(
                        organization="https://dev.azure.com/contoso",
                        project="Shop",
                        pipeline="orders-cd",
                        pipeline_id="42",
                        run_id="1234",
                        run_url="https://dev.azure.com/contoso/Shop/_build/results?buildId=1234",
                        job="Deploy",
                    ),
                ),
                DeployedWorkload(
                    workload="deployment/orders-canary",
                    images=["acr.io/orders:1.5.0-rc1"],
                    pipeline=DeployingPipeline(pipeline="orders-canary"),
                ),
            ],
        )

    def test_unexposed_workload_records_images_without_pipeline(self) -> None:
        self.assertEqual(
            self.services[_id("worker")].deployed_workloads,
            [DeployedWorkload(workload="deployment/worker", images=["acr.io/worker@sha256:abc"])],
        )

    def test_service_without_workloads_records_nothing(self) -> None:
        self.assertIsNone(self.services[_id("legacy")].deployed_workloads)


class MergeTests(unittest.TestCase):
    def test_union_by_workload_first_wins(self) -> None:
        old = DeployedWorkload(workload="deployment/orders", images=["orders:1"])
        new = DeployedWorkload(workload="deployment/orders", images=["orders:2"])
        other = DeployedWorkload(workload="cronjob/nightly", images=["nightly:1"])
        self.assertEqual(merge_deployed_workloads([new], [old, other]), [other, new])
        self.assertIsNone(merge_deployed_workloads(None, []))

    def test_old_snapshots_without_the_field_still_load(self) -> None:
        node = ServiceNode.model_validate(
            {"service_id": _id("orders"), "name": "orders", "metadata": {}}
        )
        self.assertIsNone(node.deployed_workloads)


if __name__ == "__main__":
    unittest.main()
