"""Declared identity links from build files and runtime facts (step 4b).

Build files (Compose, skaffold, Azure Pipelines Docker@2, .NET Aspire
AppHost) and runtime facts (images, identity labels, OTEL_SERVICE_NAME)
tie a source-code service to its runtime name. Identity only: they never
create a dependency edge.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.core.identity_links import (
    IdentityFacts,
    LinkEvidenceKind,
    image_repository,
    link_identities,
    source_repository,
)
from src.core.models import (
    IDENTITY_LINK_METADATA_KEY,
    IDENTITY_UNLINKED_METADATA_KEY,
    DeployedWorkload,
    IdentityDeclaration,
    IdentityDeclarationKind,
)
from src.tools.declared_identity import (
    BuildDeclaration,
    DeclarationFileKind,
    attribute_declarations,
    declaration_file_kind,
    read_declarations,
)
from src.tools.k8s_extractor import PodTemplate, deployed_workloads
from tests.benchmarks.discovery.offline_scan import scan_checkout
from tests.test_identity_scope import URN, _k8s, _reconcile, _static

Kind = IdentityDeclarationKind


def _values(found: list[BuildDeclaration]) -> list[tuple[str, str, str]]:
    return [(d.kind.value, d.value, d.build_directory) for d in found]


class ImageRepositoryTests(unittest.TestCase):
    def test_registry_tag_and_digest_are_dropped(self) -> None:
        self.assertEqual(image_repository("myacr.azurecr.io/team/Orders:1.2"), "team/orders")
        self.assertEqual(image_repository("orders@sha256:abc"), "orders")
        self.assertEqual(image_repository("localhost:5000/orders:dev"), "orders")
        self.assertEqual(image_repository("${REGISTRY:-eshop}/ordering.api:${TAG:-latest}"), "ordering.api")
        self.assertIsNone(image_repository("${IMAGE}"))
        self.assertIsNone(image_repository("team/${NAME}"))


class FileKindTests(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertIs(declaration_file_kind("/docker-compose.override.yml"), DeclarationFileKind.COMPOSE)
        self.assertIs(declaration_file_kind("/deploy/skaffold.yaml"), DeclarationFileKind.SKAFFOLD)
        self.assertIs(declaration_file_kind("/azure-pipelines.ci.yml"), DeclarationFileKind.PIPELINE)
        self.assertIs(declaration_file_kind("/.pipelines/build.yaml"), DeclarationFileKind.PIPELINE)
        self.assertIs(
            declaration_file_kind("/src/AppHost/Program.cs", {"src/AppHost"}), DeclarationFileKind.ASPIRE
        )
        self.assertIsNone(declaration_file_kind("/src/Orders/Program.cs", {"src/AppHost"}))
        self.assertIsNone(declaration_file_kind("/node_modules/x/docker-compose.yml"))
        self.assertIsNone(declaration_file_kind("/src/orders/values.yaml"))


class ComposeTests(unittest.TestCase):
    def test_build_directory_name_and_image(self) -> None:
        content = """
services:
  ordering-api:
    image: ${REGISTRY:-eshop}/ordering.api:${TAG:-latest}
    build:
      context: .
      dockerfile: src/Ordering.API/Dockerfile
  basket:
    build: ../basket
  redis:
    image: redis:7
"""
        found = read_declarations("/deploy/docker-compose.yml", content, DeclarationFileKind.COMPOSE)
        self.assertEqual(
            _values(found),
            [
                ("COMPOSE_SERVICE", "ordering-api", "deploy/src/Ordering.API"),
                ("COMPOSE_IMAGE", "ordering.api", "deploy/src/Ordering.API"),
                ("COMPOSE_SERVICE", "basket", "basket"),
            ],
        )
        self.assertEqual(found[0].file_path, "deploy/docker-compose.yml")

    def test_invalid_yaml_and_escaping_paths_declare_nothing(self) -> None:
        self.assertEqual(read_declarations("/compose.yml", "services: [", DeclarationFileKind.COMPOSE), [])
        content = "services:\n  x:\n    build: ../../outside\n"
        self.assertEqual(read_declarations("/compose.yml", content, DeclarationFileKind.COMPOSE), [])


class SkaffoldTests(unittest.TestCase):
    def test_artifacts_including_profiles(self) -> None:
        content = """
apiVersion: skaffold/v4beta6
kind: Config
build:
  artifacts:
    - image: cartservice
      context: src/cartservice/src
    - image: frontend
      context: src/frontend
      docker:
        dockerfile: Dockerfile
profiles:
  - name: ci
    build:
      artifacts:
        - image: gcr.io/demo/loadgenerator
          context: src/loadgenerator
"""
        found = read_declarations("/skaffold.yaml", content, DeclarationFileKind.SKAFFOLD)
        self.assertEqual(
            _values(found),
            [
                ("SKAFFOLD_ARTIFACT", "cartservice", "src/cartservice/src"),
                ("SKAFFOLD_ARTIFACT", "frontend", "src/frontend"),
                ("SKAFFOLD_ARTIFACT", "demo/loadgenerator", "src/loadgenerator"),
            ],
        )


class PipelineTests(unittest.TestCase):
    def test_docker_tasks_with_file_variables(self) -> None:
        content = """
variables:
  imageRepository: 'orders-api'
stages:
- stage: Build
  jobs:
  - job: Build
    steps:
    - task: Docker@2
      inputs:
        command: buildAndPush
        repository: $(imageRepository)
        dockerfile: $(Build.SourcesDirectory)/src/Orders/Dockerfile
    - task: Docker@2
      inputs:
        repository: payments
        Dockerfile: '**/Dockerfile'
        buildContext: src/Payments
    - task: Docker@2
      inputs:
        repository: $(fromAGroup)
        Dockerfile: src/Other/Dockerfile
    - task: Docker@2
      inputs:
        command: login
"""
        found = read_declarations("/azure-pipelines.yml", content, DeclarationFileKind.PIPELINE)
        self.assertEqual(
            _values(found),
            [
                ("PIPELINE_DOCKER_TASK", "orders-api", "src/Orders"),
                ("PIPELINE_DOCKER_TASK", "payments", "src/Payments"),
            ],
        )

    def test_list_form_variables(self) -> None:
        content = """
variables:
- name: repo
  value: web
- group: shared
steps:
- task: Docker@2
  inputs:
    repository: team/$(repo)
    Dockerfile: web/Dockerfile
"""
        found = read_declarations("/pipelines/ci.yml", content, DeclarationFileKind.PIPELINE)
        self.assertEqual(_values(found), [("PIPELINE_DOCKER_TASK", "team/web", "web")])


class AspireTests(unittest.TestCase):
    FILES = [
        "/src/eShop.AppHost/eShop.AppHost.csproj",
        "/src/eShop.AppHost/Program.cs",
        "/src/Ordering.API/Ordering.API.csproj",
        "/src/Basket.API/Basket.API.csproj",
        "/src/Web-App/Web-App.csproj",
    ]

    def test_resources_resolve_to_directories(self) -> None:
        content = """
var builder = DistributedApplication.CreateBuilder(args);
var redis = builder.AddRedis("redis");
var ordering = builder.AddProject<Projects.Ordering_API>("ordering-api")
    .WithReference(redis);
builder.AddProject("basket-api", "../Basket.API/Basket.API.csproj");
builder.AddProject<Projects.Web_App>("webapp");
// builder.AddProject<Projects.Basket_API>("old-basket");
builder.AddNpmApp("storefront", "../storefront");
builder.AddProject<Projects.Missing>("missing");
"""
        found = read_declarations(
            "/src/eShop.AppHost/Program.cs", content, DeclarationFileKind.ASPIRE, self.FILES
        )
        self.assertEqual(
            _values(found),
            [
                ("ASPIRE_RESOURCE", "ordering-api", "src/Ordering.API"),
                ("ASPIRE_RESOURCE", "webapp", "src/Web-App"),
                ("ASPIRE_RESOURCE", "basket-api", "src/Basket.API"),
                ("ASPIRE_RESOURCE", "storefront", "src/storefront"),
            ],
        )


class AttributionTests(unittest.TestCase):
    def test_deepest_enclosing_root_wins_and_outsiders_drop(self) -> None:
        def declared(directory: str, value: str) -> BuildDeclaration:
            return BuildDeclaration(
                kind=Kind.COMPOSE_SERVICE, value=value, file_path="compose.yml", build_directory=directory
            )

        attributed = attribute_declarations(
            [
                declared("src/orders/api", "orders"),
                declared("src/web", "web"),
                declared("tools", "x"),
                declared("src/orders/docker/orders-db", "orders-db"),
            ],
            ["src/orders", "src"],
            {"docker"},
        )
        self.assertEqual(
            attributed,
            {
                "src/orders": [IdentityDeclaration(kind=Kind.COMPOSE_SERVICE, value="orders", file_path="compose.yml")],
                "src": [IdentityDeclaration(kind=Kind.COMPOSE_SERVICE, value="web", file_path="compose.yml")],
            },
        )


class RuntimeFactTests(unittest.TestCase):
    @staticmethod
    def _template(labels: dict[str, str], *env: SimpleNamespace) -> PodTemplate:
        container = SimpleNamespace(image="ghcr.io/x/cart:1", env=list(env))
        return PodTemplate(labels, "deployment/cart", SimpleNamespace(containers=[container]))

    def test_labels_and_otel_names(self) -> None:
        label_ref = SimpleNamespace(
            name="OTEL_SERVICE_NAME",
            value=None,
            value_from=SimpleNamespace(
                field_ref=SimpleNamespace(field_path="metadata.labels['app.kubernetes.io/component']")
            ),
        )
        (workload,) = deployed_workloads(
            [self._template({"app.kubernetes.io/component": "cart", "tier": "x"}, label_ref)]
        ) or []
        self.assertEqual(workload.identity_labels, {"app.kubernetes.io/component": "cart"})
        self.assertEqual(workload.otel_service_names, ["cart"])

    def test_resource_attributes_and_unexpanded_values(self) -> None:
        attributes = SimpleNamespace(
            name="OTEL_RESOURCE_ATTRIBUTES", value="service.namespace=shop,service.name=cart-svc", value_from=None
        )
        (workload,) = deployed_workloads([self._template({}, attributes)]) or []
        self.assertEqual(workload.otel_service_names, ["cart-svc"])
        unexpanded = SimpleNamespace(name="OTEL_SERVICE_NAME", value="$(POD_NAME)", value_from=None)
        (workload,) = deployed_workloads([self._template({}, unexpanded)]) or []
        self.assertEqual(workload.otel_service_names, [])


class DeclaredLinkTests(unittest.TestCase):
    def test_each_kind_of_fact_links(self) -> None:
        result = link_identities(
            ["ordering", "basketapi", "cart"],
            ["orders-svc", "basket", "cart-v2"],
            source_facts={
                "ordering": IdentityFacts(image_repositories=frozenset({"ordering.api"})),
                "basketapi": IdentityFacts(names=frozenset({"basket"})),
            },
            runtime_facts={
                "orders-svc": IdentityFacts(image_repositories=frozenset({"eshop/ordering.api"})),
                "cart-v2": IdentityFacts(names=frozenset({"cart"})),
            },
        )
        self.assertEqual(
            [(l.source_name, l.runtime_name, l.evidence, l.detail) for l in result.links],
            [
                ("basketapi", "basket", LinkEvidenceKind.DECLARED_NAME, "basket"),
                ("cart", "cart-v2", LinkEvidenceKind.RUNTIME_DECLARED_NAME, "cart"),
                ("ordering", "orders-svc", LinkEvidenceKind.IMAGE_REPOSITORY, "ordering.api"),
            ],
        )
        self.assertEqual(result.unlinked, {})

    def test_conflicting_or_shared_candidates_link_nothing(self) -> None:
        # One source's facts point at two runtime names.
        result = link_identities(
            ["web"],
            ["frontend", "storefront"],
            source_facts={"web": IdentityFacts(names=frozenset({"frontend", "storefront"}))},
        )
        self.assertEqual(result.links, [])
        self.assertEqual(result.unlinked["web"][:2], ["frontend", "storefront"])
        # Two sources' facts point at one runtime name (a shared image repository).
        result = link_identities(
            ["a", "b"],
            ["demo"],
            source_facts={
                "a": IdentityFacts(image_repositories=frozenset({"x/demo"})),
                "b": IdentityFacts(image_repositories=frozenset({"x/demo"})),
            },
            runtime_facts={"demo": IdentityFacts(image_repositories=frozenset({"x/demo"}))},
        )
        self.assertEqual(result.links, [])
        self.assertEqual(result.unlinked, {"a": ["demo"], "b": ["demo"]})

    def test_canonical_links_are_taken_first(self) -> None:
        result = link_identities(
            ["orders", "legacy"],
            ["orders"],
            source_facts={"legacy": IdentityFacts(names=frozenset({"orders"}))},
        )
        self.assertEqual([(l.source_name, l.evidence) for l in result.links], [("orders", LinkEvidenceKind.CANONICAL_EQUAL)])
        self.assertIn("legacy", result.unlinked)


class ReconcileDeclaredTests(unittest.TestCase):
    def test_image_declaration_merges_without_creating_edges(self) -> None:
        source = _static("ordering", "eShop", "src/Ordering.API").model_copy(
            update={
                "identity_declarations": [
                    IdentityDeclaration(
                        kind=Kind.PIPELINE_DOCKER_TASK, value="ordering-api", file_path="azure-pipelines.yml"
                    )
                ]
            }
        )
        runtime = _k8s("shop", "orders").model_copy(
            update={
                "deployed_workloads": [
                    DeployedWorkload(workload="deployment/orders", images=["acr.io/ordering-api:42"])
                ]
            }
        )
        graph = _reconcile([source, runtime, _k8s("shop", "basket")])
        services = {s.service_id: s for s in graph["services"]}
        merged = services[f"{URN}:shop:orders"]
        self.assertEqual(
            merged.metadata[IDENTITY_LINK_METADATA_KEY],
            "IMAGE_REPOSITORY: ordering = orders (via ordering-api)",
        )
        self.assertNotIn(IDENTITY_UNLINKED_METADATA_KEY, merged.metadata)
        self.assertEqual(merged.identity_declarations, source.identity_declarations)
        self.assertEqual(graph["edges"], [])


class OciProvenanceTests(unittest.TestCase):
    """OCI image provenance: Dockerfile title labels, workload annotations,
    and the links they make (identity only)."""

    def test_dockerfile_title_label(self) -> None:
        content = (
            "FROM mcr.microsoft.com/dotnet/aspnet:8.0\n"
            "# LABEL org.opencontainers.image.title=commented\n"
            'LABEL org.opencontainers.image.title="order-service" \\\n'
            "      org.opencontainers.image.source=https://github.com/acme/shop\n"
            "LABEL org.opencontainers.image.title=$NAME\n"
            "LABEL org.opencontainers.image.title legacy-form\n"
        )
        self.assertEqual(declaration_file_kind("src/Orders/Dockerfile"), DeclarationFileKind.DOCKERFILE)
        self.assertEqual(declaration_file_kind("src/Orders/api.Dockerfile"), DeclarationFileKind.DOCKERFILE)
        self.assertIsNone(declaration_file_kind("src/Orders/docker/notes.txt"))
        found = read_declarations("src/Orders/Dockerfile", content, DeclarationFileKind.DOCKERFILE)
        self.assertEqual(
            _values(found),
            [
                ("DOCKERFILE_OCI_TITLE", "order-service", "src/Orders"),
                ("DOCKERFILE_OCI_TITLE", "legacy-form", "src/Orders"),
            ],
        )

    def test_workload_annotations(self) -> None:
        template = PodTemplate(
            {}, "deployment/orders",
            SimpleNamespace(containers=[SimpleNamespace(image="acr.io/orders:1", env=[])]),
            annotations={
                "org.opencontainers.image.title": "ordering",
                "org.opencontainers.image.source": "https://dev.azure.com/o/p/_git/Ordering",
                "org.opencontainers.image.revision": "abc123",
                "unrelated": "x",
            },
        )
        (workload,) = deployed_workloads([template]) or []
        self.assertEqual(
            workload.image_provenance,
            {
                "org.opencontainers.image.title": "ordering",
                "org.opencontainers.image.source": "https://dev.azure.com/o/p/_git/Ordering",
                "org.opencontainers.image.revision": "abc123",
            },
        )

    def test_source_repository_names(self) -> None:
        self.assertEqual(source_repository("https://github.com/acme/Orders.git"), "orders")
        self.assertEqual(source_repository("https://dev.azure.com/o/p/_git/Orders"), "orders")
        self.assertEqual(source_repository("git@github.com:acme/orders.git"), "orders")
        self.assertEqual(source_repository("Orders"), "orders")
        self.assertIsNone(source_repository("https://github.com/acme/${REPO}"))

    def test_source_repository_links_only_when_unique(self) -> None:
        result = link_identities(
            ["ordering"],
            ["orders-svc", "basket"],
            source_facts={"ordering": IdentityFacts(source_repositories=frozenset({"ordering"}))},
            runtime_facts={"orders-svc": IdentityFacts(source_repositories=frozenset({"ordering"}))},
        )
        self.assertEqual(
            [(l.runtime_name, l.evidence) for l in result.links],
            [("orders-svc", LinkEvidenceKind.IMAGE_SOURCE_REPOSITORY)],
        )
        # A repository holding two unlinked services points at the runtime from both.
        result = link_identities(
            ["a", "b"],
            ["demo"],
            source_facts={n: IdentityFacts(source_repositories=frozenset({"mono"})) for n in ("a", "b")},
            runtime_facts={"demo": IdentityFacts(source_repositories=frozenset({"mono"}))},
        )
        self.assertEqual(result.links, [])

    def test_reconcile_links_by_annotation_without_edges(self) -> None:
        source = _static("ordering", "Ordering", "src/Ordering.API")
        runtime = _k8s("shop", "orders").model_copy(
            update={
                "deployed_workloads": [
                    DeployedWorkload(
                        workload="deployment/orders",
                        images=["acr.io/orders:42"],
                        image_provenance={"org.opencontainers.image.source": "https://github.com/acme/Ordering"},
                    )
                ]
            }
        )
        graph = _reconcile([source, runtime])
        merged = {s.service_id: s for s in graph["services"]}[f"{URN}:shop:orders"]
        self.assertEqual(
            merged.metadata[IDENTITY_LINK_METADATA_KEY],
            "IMAGE_SOURCE_REPOSITORY: ordering = orders (via ordering)",
        )
        self.assertEqual(graph["edges"], [])
        title = _k8s("shop", "orders").model_copy(
            update={
                "deployed_workloads": [
                    DeployedWorkload(
                        workload="deployment/orders", images=["acr.io/x:1"],
                        image_provenance={"org.opencontainers.image.title": "ordering"},
                    )
                ]
            }
        )
        graph = _reconcile([_static("ordering", "Shop", "src/Ordering.API"), title, _k8s("shop", "basket")])
        merged = {s.service_id: s for s in graph["services"]}[f"{URN}:shop:orders"]
        self.assertEqual(
            merged.metadata[IDENTITY_LINK_METADATA_KEY],
            "RUNTIME_DECLARED_NAME: ordering = orders (via ordering)",
        )


class ScanRepositoryTests(unittest.TestCase):
    def test_compose_and_pipeline_declarations_reach_their_services(self) -> None:
        files = {
            "services/orders/Dockerfile": "FROM golang\n",
            "services/orders/go.mod": "module orders\n",
            "services/web/Dockerfile": "FROM node\n",
            "services/web/package.json": '{"name": "web"}\n',
            "docker-compose.yml": (
                "services:\n"
                "  order-service:\n    build: ./services/orders\n    image: shop/orders\n"
                "  storefront:\n    build:\n      context: .\n      dockerfile: services/web/Dockerfile\n"
            ),
            "azure-pipelines.yml": (
                "steps:\n- task: Docker@2\n  inputs:\n    repository: shop-web\n"
                "    Dockerfile: services/web/Dockerfile\n"
            ),
        }
        with tempfile.TemporaryDirectory() as root:
            for path, text in files.items():
                target = Path(root, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            scan = scan_checkout("shop", Path(root), "a" * 40)
        declared = {
            s.service.root: [(d.kind.value, d.value) for d in s.identity_declarations] for s in scan.services
        }
        self.assertEqual(
            declared,
            {
                "services/orders": [("COMPOSE_IMAGE", "shop/orders"), ("COMPOSE_SERVICE", "order-service")],
                "services/web": [("COMPOSE_SERVICE", "storefront"), ("PIPELINE_DOCKER_TASK", "shop-web")],
            },
        )
        # Identity only: the Compose file still contributes no reference.
        self.assertTrue(all(not s.references for s in scan.services))


if __name__ == "__main__":
    unittest.main()
