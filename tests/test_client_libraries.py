"""Client libraries declared without any address (the silent-miss guard)."""

import unittest

from src.core.models import OpaqueReferenceKind, UnscannedReason
from src.tools.client_libraries import Ecosystem, client_libraries, manifest_ecosystem
from src.tools.reference_sites import external_config_sites
from tests.test_coverage_and_shape import _extractor


def _names(content: str, path: str) -> list[tuple[str, int]]:
    return [(library.name, library.line) for library in client_libraries(content, path)]


class ManifestTests(unittest.TestCase):
    def test_ecosystems(self) -> None:
        cases = {
            "/web/package.json": Ecosystem.NPM,
            "/api/go.mod": Ecosystem.GO,
            "/ml/requirements.txt": Ecosystem.PYTHON,
            "/ml/requirements-prod.txt": Ecosystem.PYTHON,
            "/ml/pyproject.toml": Ecosystem.PYTHON,
            "/src/Ordering.API/Ordering.API.csproj": Ecosystem.NUGET,
            "/orders/pom.xml": Ecosystem.MAVEN,
            "/ad/build.gradle.kts": Ecosystem.MAVEN,
            "/web/package-lock.json": None,
            "/api/main.go": None,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertIs(manifest_ecosystem(path), expected)

    def test_npm_reads_runtime_dependencies_only(self) -> None:
        content = (
            "{\n"
            '  "name": "front-end",\n'
            '  "dependencies": {\n'
            '    "express": "^4",\n'
            '    "request": "^2",\n'
            '    "@openfeature/flagd-provider": "^0.13"\n'
            "  },\n"
            '  "devDependencies": { "axios": "^1" }\n'
            "}\n"
        )
        self.assertEqual(
            _names(content, "package.json"),
            [("request", 5), ("@openfeature/flagd-provider", 6)],
        )

    def test_go_python_nuget_maven_gradle(self) -> None:
        go = "module x\n\nrequire (\n\tgithub.com/rabbitmq/amqp091-go v1.9.0\n\tgithub.com/gorilla/mux v1.8.0\n)\n"
        self.assertEqual(_names(go, "go.mod"), [("github.com/rabbitmq/amqp091-go", 4)])
        requirements = "# HTTP\nrequests==2.31\nflask==3\npika>=1.3\n"
        self.assertEqual(_names(requirements, "requirements.txt"), [("requests", 2), ("pika", 4)])
        csproj = (
            '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
            '  <PackageReference Include="Grpc.AspNetCore" />\n'
            '  <PackageReference Include="Grpc.Net.ClientFactory" />\n'
            '  <PackageReference Include="Aspire.RabbitMQ.Client" />\n'
            "</Project>\n"
        )
        self.assertEqual(
            _names(csproj, "A.csproj"), [("Grpc.Net.ClientFactory", 3), ("Aspire.RabbitMQ.Client", 4)]
        )
        pom = "<artifactId>orders</artifactId>\n<artifactId>spring-boot-starter-amqp</artifactId>\n"
        self.assertEqual(_names(pom, "pom.xml"), [("spring-boot-starter-amqp", 2)])
        gradle = 'implementation("org.apache.kafka:kafka-clients:3.7.0")\nimplementation "io.grpc:grpc-netty:1.6"\n'
        self.assertEqual(_names(gradle, "build.gradle.kts"), [("kafka-clients", 1)])

    def test_grpc_core_serves_as_much_as_it_calls(self) -> None:
        # A pure gRPC server declares these too, so they prove no outbound call.
        self.assertEqual(_names("require google.golang.org/grpc v1.60.0\n", "go.mod"), [])
        self.assertEqual(_names("grpcio==1.60\n", "requirements.txt"), [])

    def test_unparseable_package_json_names_nothing(self) -> None:
        self.assertEqual(_names("{ not json", "package.json"), [])


class GuardScanTests(unittest.TestCase):
    def test_service_with_client_library_and_no_address_is_not_understood(self) -> None:
        files: dict[str, str | None] = {
            # front-end calls through a helper the scanner can't follow.
            "/front-end/package.json": '{"dependencies": {"request": "^2"}}',
            "/front-end/Dockerfile": "FROM node:20\n",
            "/front-end/api.js": "module.exports = (domain) => util.format('http://catalogue%s', domain);\n",
            # orders has a readable address: no guard.
            "/orders/package.json": '{"dependencies": {"axios": "^1"}}',
            "/orders/Dockerfile": "FROM node:20\n",
            "/orders/index.js": "axios.get('http://payment/pay')\n",
            # worker declares no client library: no guard.
            "/worker/package.json": '{"dependencies": {"express": "^4"}}',
            "/worker/Dockerfile": "FROM node:20\n",
        }
        scan = _extractor(files).scan_repository("repo-a").items[0]
        coverage = {s.service.name: s.coverage for s in scan.services}
        front = coverage["front-end"]
        assert front is not None
        (flagged,) = front.unscanned
        self.assertEqual((flagged.path, flagged.reason), ("front-end/package.json", UnscannedReason.REFERENCES_NOT_UNDERSTOOD))
        self.assertEqual(
            [(s.line, s.kind, s.detail) for s in flagged.sites],
            [(1, OpaqueReferenceKind.CLIENT_WITHOUT_ADDRESS, "request")],
        )
        for name in ("orders", "worker"):
            service = coverage[name]
            assert service is not None
            self.assertEqual(service.unscanned, [], name)

    def test_a_library_manifest_flags_its_consumers(self) -> None:
        # TeaStore: services reach each other through a shared registry client.
        files: dict[str, str | None] = {
            "/pom.xml": "<project><modules><module>webui</module><module>registryclient</module></modules></project>",
            "/webui/pom.xml": (
                "<project><artifactId>webui</artifactId><dependencies><dependency><groupId>x</groupId>"
                "<artifactId>registryclient</artifactId></dependency></dependencies>"
                "<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></project>"
            ),
            "/webui/Dockerfile": "FROM eclipse-temurin\n",
            "/registryclient/pom.xml": (
                "<project><artifactId>registryclient</artifactId>\n<dependencies>\n<dependency>\n"
                "<artifactId>ribbon-loadbalancer</artifactId>\n</dependency></dependencies></project>"
            ),
            "/registryclient/Client.java": "class Client {}\n",
        }
        scan = _extractor(files).scan_repository("repo-a").items[0]
        (webui,) = scan.services
        assert webui.coverage is not None
        self.assertEqual(
            [(u.path, [s.detail for s in u.sites]) for u in webui.coverage.unscanned],
            [("registryclient/pom.xml", ["ribbon-loadbalancer"])],
        )


class ExternalConfigTests(unittest.TestCase):
    def test_config_server_imports_are_not_understood(self) -> None:
        yaml_config = (
            "spring:\n  config:\n    import: optional:configserver:${CONFIG_SERVER_URL:http://localhost:8888/}\n"
            "# import: configserver:http://old\n"
            "---\nspring:\n  data:\n    mongodb:\n      uri: mongodb://db\n"
        )
        sites = external_config_sites(yaml_config)
        self.assertEqual(
            [(s.line, s.kind, s.detail) for s in sites],
            [(3, OpaqueReferenceKind.EXTERNAL_CONFIG_SOURCE, "spring.config.import")],
        )
        properties = "spring.cloud.config.uri=http://config:8888\nspring.data.mongodb.uri=mongodb://db\n"
        self.assertEqual([s.detail for s in external_config_sites(properties)], ["spring.cloud.config.uri"])


if __name__ == "__main__":
    unittest.main()
