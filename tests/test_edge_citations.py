"""Edges record every file whose code or configuration is their evidence."""

import unittest

from src.coordinator.revision import head_topology
from src.core.models import DependencyEdge, DiscoveryMethod, KnowledgeGraph, merge_evidence_files
from src.discovery_engine.nodes import _merge_edge
from src.tools.devops_extractor import (
    BoundarySource,
    EndpointReference,
    ReferenceKind,
    ServiceBoundary,
    ServiceScan,
)
from tests.test_config_frameworks import API, CONTEXT, SERVICES, _id
from tests.test_coverage_and_shape import _extractor


def _edge(files: list[str] | None, method: DiscoveryMethod = DiscoveryMethod.STATIC_CODE) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=API,
        target_service_id=_id("rabbitmq"),
        discovery_methods=[method],
        confidence_score=0.7,
        last_observed_at="2026-11-13T00:00:00Z",
        evidence_files=files,
    )


class ScanTests(unittest.TestCase):
    def test_every_file_of_a_reference_is_recorded(self) -> None:
        files: dict[str, str | None] = {
            "/svc/Dockerfile": "FROM python:3.12\n",
            "/svc/a.py": 'BASE = "http://payment:8080/pay"\n',
            "/svc/b.py": 'CALL = "http://payment:8080/pay"\nimport os\nos.getenv("ORDERS_URL")\n',
        }
        (scan,) = _extractor(files).scan_repository("repo-a").items[0].services
        by_endpoint = {r.endpoint: r.evidence_files for r in scan.references}
        self.assertEqual(by_endpoint["http://payment:8080/pay"], ["svc/a.py", "svc/b.py"])
        self.assertEqual(by_endpoint["ORDERS_URL"], ["svc/b.py"])


class EdgeTests(unittest.TestCase):
    def test_paths_are_relative_unique_sorted(self) -> None:
        self.assertEqual(_edge(["/b.py", "a.py", "b.py", "/"]).evidence_files, ["a.py", "b.py"])

    def test_old_snapshots_load_without_files(self) -> None:
        payload = _edge(None).model_dump(exclude={"evidence_files"})
        self.assertIsNone(DependencyEdge.model_validate(payload).evidence_files)

    def test_merge_unions_files(self) -> None:
        self.assertIsNone(merge_evidence_files(None, None))
        self.assertEqual(merge_evidence_files(None, ["a"]), ["a"])
        merged = _merge_edge(_edge(["b.py"]), _edge([], DiscoveryMethod.K8S_MANIFEST))
        self.assertEqual(merged.evidence_files, ["b.py"])
        self.assertEqual(_merge_edge(_edge(["b.py"]), _edge(["a.py"])).evidence_files, ["a.py", "b.py"])


class HeadTests(unittest.TestCase):
    def _head(self, *paths: str) -> ServiceScan:
        return ServiceScan(
            service=ServiceBoundary(name="api", root="src/api", source=BoundarySource.MARKER),
            references=[
                EndpointReference(
                    service_name="api",
                    endpoint="amqp://rabbitmq",
                    file_path=paths[0],
                    kind=ReferenceKind.URL,
                    evidence_files=list(paths),
                )
            ],
        )

    def test_head_files_replace_baseline_files(self) -> None:
        graph = KnowledgeGraph(services=SERVICES, edges=[_edge(["src/api/old.cs"])])
        topology = head_topology(
            graph, CONTEXT.in_namespace("shop"), {"src/api": API}, [self._head("src/api/new.cs")]
        )
        (edge,) = topology.graph.edges
        self.assertEqual(edge.evidence_files, ["src/api/new.cs"])

    def test_removed_code_evidence_drops_files(self) -> None:
        both = _edge(["src/api/old.cs"]).model_copy(
            update={"discovery_methods": [DiscoveryMethod.STATIC_CODE, DiscoveryMethod.RUNTIME_TELEMETRY]}
        )
        graph = KnowledgeGraph(services=SERVICES, edges=[both])
        head = self._head("src/api/x.cs").model_copy(update={"references": []})
        topology = head_topology(graph, CONTEXT.in_namespace("shop"), {"src/api": API}, [head])
        (edge,) = topology.graph.edges
        self.assertEqual((edge.discovery_methods, edge.evidence_files), ([DiscoveryMethod.RUNTIME_TELEMETRY], []))


if __name__ == "__main__":
    unittest.main()
