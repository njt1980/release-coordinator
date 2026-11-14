"""Spring Cloud Config served files, config clients and Zuul routes (step 1h-b)."""

import unittest

from src.core.models import OpaqueReferenceKind
from src.tools.devops_extractor import ReferenceKind, spring_config_read
from src.tools.spring_cloud import (
    ServedFile,
    SpringSettings,
    config_servers,
    served_directory,
    served_file,
    spring_settings,
    zuul_route_services,
)
from tests.test_coverage_and_shape import _extractor


class SettingsTests(unittest.TestCase):
    def test_server_client_and_application_name(self) -> None:
        server = spring_settings(
            {
                "config/src/main/resources/application.yml": {
                    "spring.cloud.config.server.native.search-locations": "classpath:/shared",
                }
            }
        )
        self.assertEqual(server.served_directories, ["config/src/main/resources/shared"])
        client = spring_settings(
            {
                "account/src/main/resources/bootstrap.yml": {
                    "spring.application.name": "account-service",
                    "spring.cloud.config.uri": "http://config:8888",
                }
            }
        )
        self.assertEqual((client.application_name, client.config_server), ("account-service", "config"))
        imported = spring_settings({"a.yml": {"spring.config.import": "optional:configserver:http://cfg:8888"}})
        self.assertEqual(imported.config_server, "cfg")

    def test_only_classpath_locations_are_in_the_repository(self) -> None:
        self.assertIsNone(served_directory("file:/etc/config", "c/src/main/resources/application.yml"))
        self.assertEqual(served_directory("classpath:/", "c/src/main/resources/application.yml"), "c/src/main/resources")


class ServedFileTests(unittest.TestCase):
    SERVED = {"config/src/main/resources/shared": ["account", "gateway"]}
    NAMES: dict[str, str | None] = {"account": "account-service", "gateway": "gateway"}

    def test_application_file_goes_to_every_client(self) -> None:
        self.assertEqual(
            served_file("/config/src/main/resources/shared/application.yml", self.SERVED, self.NAMES),
            ServedFile(clients=["account", "gateway"]),
        )

    def test_named_file_goes_to_its_application_with_its_profile(self) -> None:
        self.assertEqual(
            served_file("/config/src/main/resources/shared/account-service-docker.yml", self.SERVED, self.NAMES),
            ServedFile(application="account-service", profile="docker", clients=["account"]),
        )

    def test_unknown_application_and_other_directories(self) -> None:
        unknown = served_file("/config/src/main/resources/shared/monitoring.yml", self.SERVED, self.NAMES)
        self.assertEqual(unknown, ServedFile(application="monitoring"))
        self.assertIsNone(served_file("/config/src/main/resources/application.yml", self.SERVED, self.NAMES))

    def test_clients_of_an_in_repository_server_only(self) -> None:
        settings = {
            "config": SpringSettings(served_directories=["config/src/main/resources/shared"]),
            "account": SpringSettings(application_name="account-service", config_server="config"),
            "billing": SpringSettings(application_name="billing", config_server="central-config.corp"),
            "plain": SpringSettings(application_name="plain"),
        }
        served = config_servers(settings, {"config": "config", "account": "account", "billing": "billing", "plain": "plain"})
        self.assertEqual(served, {"config/src/main/resources/shared": ["account"]})


class SpringReadTests(unittest.TestCase):
    def test_zuul_service_id_routes_are_registry_calls(self) -> None:
        properties = {
            "zuul.routes.accounts.serviceId": "account-service",
            "zuul.routes.stats.service-id": "statistics-service",
            "zuul.routes.auth.url": "http://auth-service:5000",
        }
        self.assertEqual(zuul_route_services(properties), ["account-service", "statistics-service"])
        read = spring_config_read(
            "zuul:\n  routes:\n    accounts:\n      serviceId: account-service\n", "gateway", "/gw/application.yml", None
        )
        self.assertEqual([(r.kind, r.endpoint) for r in read.references], [(ReferenceKind.URL, "lb://account-service")])

    def test_a_nested_yaml_config_uri_is_an_external_config_source(self) -> None:
        content = "spring:\n  cloud:\n    config:\n      uri: http://config:8888\n"
        read = spring_config_read(content, "account", "/account/src/main/resources/bootstrap.yml", None)
        self.assertEqual(
            [(s.line, s.kind, s.detail) for s in read.sites],
            [(4, OpaqueReferenceKind.EXTERNAL_CONFIG_SOURCE, "spring.cloud.config.uri")],
        )


class ScanTests(unittest.TestCase):
    FILES: dict[str, str | None] = {
        "/config/Dockerfile": "FROM java\n",
        "/config/src/main/resources/application.yml": (
            "spring:\n  cloud:\n    config:\n      server:\n        native:\n          search-locations: classpath:/shared\n"
        ),
        "/config/src/main/resources/shared/application.yml": (
            "eureka:\n  client:\n    serviceUrl:\n      defaultZone: http://registry:8761/eureka/\n"
        ),
        "/config/src/main/resources/shared/account-service.yml": "spring:\n  data:\n    mongodb:\n      host: account-mongodb\n",
        "/account/Dockerfile": "FROM java\n",
        "/account/src/main/resources/bootstrap.yml": (
            "spring:\n  application:\n    name: account-service\n  cloud:\n    config:\n      uri: http://config:8888\n"
        ),
        "/billing/Dockerfile": "FROM java\n",
        "/billing/src/main/resources/bootstrap.yml": (
            "spring:\n  application:\n    name: billing\n  cloud:\n    config:\n      uri: http://central-config.corp:8888\n"
        ),
    }

    def test_served_files_configure_their_clients_not_the_server(self) -> None:
        result = _extractor(self.FILES).scan_repository("repo-a")
        scans = {s.service.name: s for s in result.items[0].services}
        account = scans["account"]
        defaults = {b.variable: [d.template for d in b.defaults] for b in account.config.bindings}  # type: ignore[union-attr]
        self.assertEqual(defaults["SPRING_DATA_MONGODB_HOST"], ["account-mongodb"])
        self.assertEqual(defaults["EUREKA_CLIENT_SERVICEURL_DEFAULTZONE"], ["http://registry:8761"])
        # Its config server is read here, so importing from it hides nothing.
        self.assertEqual(account.coverage.unscanned, [])  # type: ignore[union-attr]
        config = scans["config"]
        self.assertEqual(config.config.bindings, [])  # type: ignore[union-attr]
        self.assertEqual(config.coverage.files_scanned, 4)  # type: ignore[union-attr]  # Dockerfile, application.yml, 2 served
        # A client of a config server outside the repository stays incomplete.
        billing = scans["billing"].coverage.unscanned  # type: ignore[union-attr]
        self.assertEqual([s.kind for u in billing for s in u.sites], [OpaqueReferenceKind.EXTERNAL_CONFIG_SOURCE])


if __name__ == "__main__":
    unittest.main()
