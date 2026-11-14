"""Files Dockerfiles copy in from outside every service (shared files)."""

import tempfile
import unittest
from pathlib import Path

from src.coordinator.file_mapping import map_changed_files
from src.core.models import KnowledgeGraph, RepositoryCoverage, ServiceNode, SharedFile
from src.tools.docker_copies import copied_files, copy_sources
from tests.benchmarks.discovery.offline_scan import scan_checkout

FILES = ["pb/demo.proto", "pb/extra.proto", "src/a/Dockerfile", "src/a/main.go", "shared/x.py", "shared/y.py", "README.md"]


class CopySourcesTests(unittest.TestCase):
    def test_instructions(self) -> None:
        content = (
            "FROM golang AS build\n"
            "# COPY ignored/comment .\n"
            "COPY ./pb/demo.proto proto/demo.proto\n"
            'COPY ["/pb/extra.proto", "x"]\n'
            "COPY --chown=app:app shared/x.py \\\n     shared/y.py /app/\n"
            "COPY --from=build /out /out\n"
            "ADD https://example.com/a.tgz /tmp/\n"
            "COPY $SRC /app\n"
        )
        self.assertEqual(
            copy_sources(content), ["./pb/demo.proto", "/pb/extra.proto", "shared/x.py", "shared/y.py"]
        )

    def test_resolution_against_both_contexts(self) -> None:
        root_context = copied_files("COPY ./pb/ ./proto\n", "src/a/Dockerfile", "src/a", FILES)
        self.assertEqual(root_context, {"pb/demo.proto", "pb/extra.proto"})
        own_context = copied_files("COPY ../../shared/*.py /app/\n", "src/a/Dockerfile", "src/a", FILES)
        self.assertEqual(own_context, {"shared/x.py", "shared/y.py"})

    def test_the_whole_repository_or_the_service_itself_is_ignored(self) -> None:
        for line in ("COPY . .\n", "COPY ./src /app\n", "COPY src/a /app\n"):
            with self.subTest(line=line):
                self.assertEqual(copied_files(line, "src/a/Dockerfile", "src/a", FILES), set())


class SharedFileScanTests(unittest.TestCase):
    def test_a_copied_file_outside_every_service_is_shared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "pb/demo.proto": 'syntax = "proto3";\n',
                "src/cart/Dockerfile": "FROM x\nCOPY ./pb/demo.proto proto/\n",
                "src/cart/main.go": "package main\n",
                "src/ad/Dockerfile": "FROM x\nCOPY ./pb/ ./proto\nCOPY ./src/cart/main.go x\n",
                "src/ad/main.go": "package main\n",
                "src/quote/Dockerfile": "FROM x\nCOPY . .\n",
            }
            for path, text in files.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            scan = scan_checkout("shop", root, "a" * 40)
        # cart's main.go belongs to cart, so ad copying it doesn't share it.
        self.assertEqual(
            scan.shared_files,
            [SharedFile(path="pb/demo.proto", consumers=["src/ad", "src/cart"],
                        copied_by=["src/ad/Dockerfile", "src/cart/Dockerfile"])],
        )
        self.assertEqual(scan.coverage().shared_files, scan.shared_files)


class MappingTests(unittest.TestCase):
    def test_a_change_to_a_shared_file_targets_its_consumers(self) -> None:
        urn = "urn:release-coord:acme:prod:aks-1:shop"
        graph = KnowledgeGraph(
            services=[
                ServiceNode(service_id=f"{urn}:{n}", name=n, metadata={"repository": "Shop", "service_root": f"src/{n}"})
                for n in ("cart", "ad", "quote")
            ],
            edges=[],
            static_coverage=[
                RepositoryCoverage(
                    repository="Shop", commit_sha="a" * 40,
                    shared_files=[SharedFile(path="pb/demo.proto", consumers=["src/ad", "src/cart", "src/gone"])],
                )
            ],
        )
        mapping = map_changed_files(graph, "shop", ["pb/demo.proto", "pb/other.proto"])
        self.assertEqual(mapping.target_services, [f"{urn}:ad", f"{urn}:cart"])
        # src/gone isn't a registered service, so the file is also reported; other.proto has no owner.
        self.assertEqual(mapping.unmapped_files, ["pb/demo.proto", "pb/other.proto"])


if __name__ == "__main__":
    unittest.main()
