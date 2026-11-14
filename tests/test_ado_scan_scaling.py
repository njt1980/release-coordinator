"""Azure DevOps scans that stay within rate limits at estate scale (T1b).

Throttled requests are retried, the project's repositories are listed once
per extractor, unchanged files come from the content-addressed blob cache,
and parallel repository scans merge in configured order.
"""

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from azure.devops.connection import Connection
from msrest.authentication import BasicTokenAuthentication

from src.core.models import DiscoveryResult, ServiceNode
from src.discovery_engine import nodes as discovery_nodes
from src.tools.blob_cache import MAX_CACHED_BYTES, BlobCache
from src.tools.devops_extractor import (
    DevOpsExtractor,
    RepositoryScan,
    ScannedRevision,
    configure_throttling_retries,
)
from tests.test_static_code_extraction import (
    _devops_state,
    _extractor_with_files,
    _repository_result,
    _scan,
)

FILES = {
    "/src/orders/package.json": "{}",
    "/src/orders/index.js": "fetch('http://payments:8080/pay')",
}


def _object_id(content: str) -> str:
    return hashlib.sha1(content.encode()).hexdigest()


def _with_object_ids(extractor: DevOpsExtractor, files: dict[str, str]) -> MagicMock:
    git_client: MagicMock = extractor._get_clients()[1]  # type: ignore[operator]
    git_client.get_items.return_value = [
        SimpleNamespace(path=path, is_folder=False, object_id=_object_id(content))
        for path, content in files.items()
    ]
    return git_client


class ThrottlingRetryTests(unittest.TestCase):
    def test_throttled_and_unavailable_requests_are_retried_honouring_retry_after(self) -> None:
        connection = Connection(
            base_url="https://dev.azure.com/org",
            creds=BasicTokenAuthentication({"access_token": "token"}),
        )
        configure_throttling_retries(connection)
        policy = connection._config.retry_policy()
        self.assertTrue(policy.is_retry("GET", 429, has_retry_after=True))
        self.assertTrue(policy.is_retry("GET", 503, has_retry_after=False))
        self.assertTrue(policy.respect_retry_after_header)
        self.assertGreaterEqual(policy.total, 5)
        # A client error that isn't throttling still fails at once.
        self.assertFalse(policy.is_retry("GET", 404, has_retry_after=False))


class RepositoryListingTests(unittest.TestCase):
    def test_repositories_are_listed_once_per_extractor(self) -> None:
        extractor = _extractor_with_files(FILES)
        git_client: MagicMock = extractor._get_clients()[1]  # type: ignore[operator]
        first = extractor.scan_repository("repo-a")
        extractor.scan_repository("repo-a")
        extractor.read_pipeline_variables("repo-a", first.items[0].revision)
        self.assertEqual(git_client.get_repositories.call_count, 1)
        self.assertEqual(extractor.scan_repository("missing").errors, ["missing: repository was not found"])


class BlobCacheTests(unittest.TestCase):
    def test_round_trip_and_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = BlobCache(Path(root))
            object_id = _object_id("hello")
            self.assertIsNone(cache.get(object_id))
            cache.put(object_id, "hello")
            self.assertEqual(cache.get(object_id), "hello")
            # Not an object id: never stored, never a path outside the cache.
            cache.put("../escape", "x")
            self.assertIsNone(cache.get("../escape"))
            self.assertFalse((Path(root).parent / "escape").exists())
            big = "x" * (MAX_CACHED_BYTES + 1)
            cache.put(_object_id(big), big)
            self.assertIsNone(cache.get(_object_id(big)))

    def test_unwritable_cache_is_a_miss_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            blocker = Path(root) / "file"
            blocker.write_text("not a directory")
            cache = BlobCache(blocker)
            cache.put(_object_id("x"), "x")
            self.assertIsNone(cache.get(_object_id("x")))

    def test_unchanged_files_are_not_fetched_again(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            first = _extractor_with_files(FILES)
            first._blob_cache = BlobCache(Path(root))
            first_client = _with_object_ids(first, FILES)
            expected = first.scan_repository("repo-a")
            self.assertGreater(first_client.get_item_content.call_count, 0)

            changed = {**FILES, "/src/orders/index.js": "fetch('http://ledger:8080/x')"}
            second = _extractor_with_files(changed)
            second._blob_cache = BlobCache(Path(root))
            second_client = _with_object_ids(second, changed)
            result = second.scan_repository("repo-a")

            fetched = [call.args[1] for call in second_client.get_item_content.call_args_list]
            self.assertEqual(fetched, ["/src/orders/index.js"])
            self.assertEqual(
                [r.endpoint for r in result.items[0].services[0].references],
                ["http://ledger:8080/x"],
            )
            self.assertEqual(
                [r.endpoint for r in expected.items[0].services[0].references],
                ["http://payments:8080/pay"],
            )


class ParallelScanTests(unittest.TestCase):
    def test_results_merge_in_configured_order_whatever_finishes_first(self) -> None:
        repo_b_done = threading.Event()

        def scan(repo: str) -> DiscoveryResult[RepositoryScan]:
            if repo == "repo-a":
                # repo-a finishes last; its services must still come first.
                self.assertTrue(repo_b_done.wait(5))
                return _repository_result(_scan("orders"))
            result = _repository_result(_scan("payments"))
            repo_b_done.set()
            return result

        with patch.object(discovery_nodes, "DevOpsExtractor") as devops_cls, patch.object(
            discovery_nodes.config, "ADO_SCAN_CONCURRENCY", 2
        ):
            devops_cls.return_value.scan_repository.side_effect = scan
            result = discovery_nodes.extract_static_code_baseline(_devops_state())

        services = result["discovered_services"]
        assert isinstance(services, list)
        self.assertEqual(
            [(s.name, s.metadata["repository"]) for s in services if isinstance(s, ServiceNode)],
            [("orders", "repo-a"), ("payments", "repo-b")],
        )
        revisions = result["scanned_revisions"]
        assert isinstance(revisions, list)
        self.assertTrue(all(isinstance(r, ScannedRevision) for r in revisions))


if __name__ == "__main__":
    unittest.main()
