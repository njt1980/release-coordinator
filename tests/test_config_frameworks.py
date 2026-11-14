"""PR 2b-3a: configuration frameworks, code defaults, image ENV, non-HTTP schemes."""

import unittest
from typing import Any

from src.coordinator.revision import head_topology
from src.core.config_resolution import (
    ResolutionOutcome,
    resolve_variable,
    spring_candidates,
    spring_variable,
)
from src.core.edge_resolution import resolve_endpoint, static_config_index
from src.core.env_names import endpoint_in_value, endpoint_of_value
from src.core.models import (
    ConfigBinding,
    ConfigFramework,
    DependencyEdge,
    DiscoveryMethod,
    KnowledgeGraph,
    PipelineVariables,
    ServiceEnvironment,
    ServiceNode,
    ServiceStaticConfig,
    StaticDefault,
    StaticDefaultOrigin,
    StaticDefaultSource,
    StaticDefaultUse,
    UnresolvedEntity,
    UnresolvedEntityKind,
    UnscannedReason,
)
from src.core.service_ids import IdentityContext, IdentityIndex, env_var_reference
from src.tools import devops_extractor
from src.tools.config_readers import (
    ConfigParseError,
    dockerfile_environment,
    dotnet_code_bindings,
    dotnet_settings_bindings,
    env_default_bindings,
    feign_client_services,
    spring_code_bindings,
    spring_config_bindings,
)
from src.tools.devops_extractor import (
    BoundarySource,
    FileClass,
    classify_path,
    ReferenceKind,
    ServiceBoundary,
    ServiceScan,
    dependency_bindings,
    read_source_file,
)
from tests.test_coverage_and_shape import CONTEXT, HEAD_SHA, _extractor, _id

# --- Values: schemes and connection strings ---------------------------------


class EndpointValueTests(unittest.TestCase):
    def test_schemes_and_connection_strings_name_their_server(self) -> None:
        cases = {
            "http://orders:8080/api": "http://orders:8080",
            "https+http://basket-api": "https+http://basket-api",
            "grpc://payments:50051": "grpc://payments:50051",
            "dns:///basket-api:50051": "basket-api:50051",
            "amqp://guest:guest@rabbitmq:5672/": "amqp://rabbitmq:5672",
            "jdbc:postgresql://u:p@postgres:5432/db": "postgresql://postgres:5432",
            "jdbc:sqlserver://sqldata:1433;databaseName=x": "sqlserver://sqldata:1433",
            "mongodb://a:1,b:2/db": "mongodb://a:1",
            "lb://vets-service": "lb://vets-service",
            "Server=tcp:sqldata,1433;User Id=sa;Password=x": "sqldata:1433",
            "Server=sql\\INSTANCE;Database=x": "sql",
            "Host=postgres;Port=5432;Username=u;Password=p": "postgres:5432",
            "HostName=rabbitmq": "rabbitmq",
            "Endpoint=sb://bus.servicebus.windows.net/;SharedAccessKey=k": "sb://bus.servicebus.windows.net",
            "s3://bucket/key": None,
            "ftp://files": None,
            "just text": None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(endpoint_of_value(value), expected)

    def test_connection_string_context_accepts_bare_hosts(self) -> None:
        self.assertIsNone(endpoint_of_value("redis:6379,abortConnect=false"))
        self.assertEqual(
            endpoint_of_value("redis:6379,abortConnect=false", connection_string=True), "redis:6379"
        )
        self.assertEqual(endpoint_of_value("basket-data", connection_string=True), "basket-data")

    def test_an_embedded_url_counts(self) -> None:
        self.assertEqual(
            endpoint_in_value("optional:configserver:http://config:8888/"), "http://config:8888"
        )


# --- Readers -----------------------------------------------------------------

APPSETTINGS = """{
  // .NET allows comments and trailing commas
  "ConnectionStrings": {
    "EventBus": "amqp://localhost",
    "OrderingDb": "Server=sqldata;Database=Ordering;User Id=sa;Password=Pass@word",
  },
  "Identity": { "Url": "http://identity-api", "Audience": "orders" },
  "Kestrel": { "Endpoints": { "Http": { "Url": "http://0.0.0.0:80" } } },
  "OpenAI": { "Endpoint": "https://api.openai.com" },
  "Logging": { "LogLevel": { "Default": "Information" } },
}"""


class DotnetReaderTests(unittest.TestCase):
    def test_settings_keys_become_overridable_bindings(self) -> None:
        bindings = {b.key: b for b in dotnet_settings_bindings(APPSETTINGS, "src/Ordering/appsettings.json", None)}

        self.assertEqual(
            sorted(bindings),
            ["ConnectionStrings:EventBus", "ConnectionStrings:OrderingDb", "Identity:Url", "OpenAI:Endpoint"],
        )
        ordering = bindings["ConnectionStrings:OrderingDb"]
        self.assertEqual(ordering.variable, "ConnectionStrings__OrderingDb")
        self.assertIs(ordering.framework, ConfigFramework.DOTNET)
        # Only the server is kept: the password never reaches a snapshot.
        self.assertEqual([d.template for d in ordering.defaults], ["sqldata"])
        self.assertNotIn("Pass@word", ServiceStaticConfig(
            repository="r", root="", service_name="s", bindings=list(bindings.values())
        ).model_dump_json())

    def test_public_only_keys_are_not_dependencies(self) -> None:
        kept = dependency_bindings(dotnet_settings_bindings(APPSETTINGS, "appsettings.json", None))
        self.assertNotIn("OpenAI:Endpoint", [b.key for b in kept])
        # A localhost default is kept: it is reported, not silently dropped.
        self.assertIn("ConnectionStrings:EventBus", [b.key for b in kept])

    def test_profile_files_carry_their_profile(self) -> None:
        bindings = dotnet_settings_bindings(
            '{"Identity": {"Url": "http://identity-staging"}}', "appsettings.Staging.json", "Staging"
        )
        self.assertEqual(bindings[0].defaults[0].profile, "Staging")

    def test_malformed_settings_raise(self) -> None:
        with self.assertRaises(ConfigParseError):
            dotnet_settings_bindings('{"a": ', "appsettings.json", None)

    def test_code_reads(self) -> None:
        code = """
            var db = builder.Configuration.GetConnectionString("catalogdb");
            builder.AddRabbitMQClient("eventbus");
            builder.AddNpgsqlDbContext<CatalogContext>("catalogdb");
            builder.Services.AddHttpClient("github");
            var orders = configuration["Services:OrdersUrl"] ?? "http://ordering-api";
            var basket = config.GetValue<string>("Basket:Endpoint", "http://basket-api");
            var level = configuration["Logging:Level"];
        """
        bindings = {b.variable: b for b in dotnet_code_bindings(code, "Program.cs")}

        self.assertEqual(
            sorted(bindings),
            ["Basket__Endpoint", "ConnectionStrings__catalogdb", "ConnectionStrings__eventbus",
             "Services__OrdersUrl"],
        )
        self.assertEqual(bindings["Services__OrdersUrl"].defaults[0].source, StaticDefaultSource.CODE)


SPRING_YAML = """
spring:
  application:
    name: customers-service
  config:
    import: optional:configserver:${CONFIG_SERVER_URL:http://localhost:8888/}
  datasource:
    url: jdbc:postgresql://${DB_USER:u}:${DB_PASS:p}@${DB_HOST:postgres}:5432/db
    password: ${DB_PASSWORD:secret}
server:
  port: 8081
vets:
  host: vets-service
  url: http://${vets.host}:8080
---
spring:
  config:
    activate:
      on-profile: docker
eureka:
  client:
    serviceUrl:
      defaultZone: http://discovery-server:8761/eureka/
"""


class SpringReaderTests(unittest.TestCase):
    def test_relaxed_binding_names(self) -> None:
        self.assertEqual(spring_variable("spring.datasource.url"), "SPRING_DATASOURCE_URL")
        self.assertEqual(spring_variable("my-app.orders-url"), "MYAPP_ORDERSURL")
        self.assertIn("DB_HOST", spring_candidates("DB_HOST"))
        self.assertIn("VETS_HOST", spring_candidates("vets.host"))

    def test_config_file_bindings(self) -> None:
        bindings = {
            b.key: b
            for b in spring_config_bindings(SPRING_YAML, "customers/src/main/resources/application.yml", None)
        }

        self.assertNotIn("server.port", bindings)
        self.assertNotIn("spring.datasource.password", bindings)
        url = bindings["spring.datasource.url"]
        self.assertEqual(url.variable, "SPRING_DATASOURCE_URL")
        # User info (and its placeholders) is removed from the stored template.
        self.assertEqual(url.defaults[0].template, "jdbc:postgresql://${DB_HOST:postgres}:5432/db")
        # A reference to another key gets that key's value as its default.
        self.assertEqual(bindings["vets.url"].defaults[0].template, "http://${vets.host:vets-service}:8080")
        self.assertEqual(bindings["eureka.client.serviceUrl.defaultZone"].defaults[0].profile, "docker")
        self.assertIn("spring.config.import", bindings)

    def test_properties_files(self) -> None:
        bindings = spring_config_bindings(
            "# comment\norders.url=http://orders:8080\nserver.port=80\n",
            "application-prod.properties",
            "prod",
        )
        self.assertEqual([(b.variable, b.defaults[0].profile) for b in bindings], [("ORDERS_URL", "prod")])

    def test_code_reads_and_feign_clients(self) -> None:
        code = """
            @Value("${visits.url:http://visits-service}") String visits;
            String host = env.getProperty("billing.host", "billing");
            @FeignClient(name = "customers-service") interface Customers {}
        """
        bindings = {b.variable: b for b in spring_code_bindings(code, "A.java")}
        self.assertEqual(sorted(bindings), ["BILLING_HOST", "VISITS_URL"])
        self.assertEqual(bindings["VISITS_URL"].defaults[0].template, "http://visits-service")
        self.assertEqual(feign_client_services(code), ["customers-service"])

    def test_discovery_client_lookups_name_registry_services(self) -> None:
        code = 'List<ServiceInstance> i = discoveryClient.getInstances("vets-service");'
        self.assertEqual(feign_client_services(code), ["vets-service"])


class CodeDefaultTests(unittest.TestCase):
    def test_env_reads_with_literal_defaults(self) -> None:
        code = """
            a = os.getenv("ORDERS", "http://orders:3000")
            b = os.environ.get("AI_URL", 'http://ai-service:5001')
            c = os.getenv("PORT", "3000")
            const d = process.env.PRODUCTS_URL || "http://product-service:3002";
            String e = System.getenv().getOrDefault("PAYMENTS", "http://payments");
            var f = Environment.GetEnvironmentVariable("SHIPPING_URL") ?? "http://shipping";
        """
        bindings = {b.variable: b.defaults[0].template for b in env_default_bindings(code, "x")}
        self.assertEqual(
            bindings,
            {
                "ORDERS": "http://orders:3000",
                "AI_URL": "http://ai-service:5001",
                "PRODUCTS_URL": "http://product-service:3002",
                "PAYMENTS": "http://payments",
                "SHIPPING_URL": "http://shipping",
            },
        )

    def test_a_url_that_is_only_a_default_is_not_a_fixed_edge(self) -> None:
        read = read_source_file('requests.get(os.getenv("ORDERS", "http://orders:3000"))', "api", "/api/a.py")
        self.assertEqual(
            [(r.kind, r.endpoint) for r in read.references], []
        )
        self.assertEqual([b.variable for b in read.bindings], ["ORDERS"])

    def test_non_http_urls_in_code_are_references(self) -> None:
        read = read_source_file(
            'amqp.connect("amqp://user:pw@rabbitmq:5672"); db("jdbc:postgresql://pg:5432/x")',
            "api",
            "/api/a.js",
        )
        self.assertEqual(
            sorted(r.dependency_target() or "" for r in read.references), ["pg", "rabbitmq"]
        )


class ComposeFileTests(unittest.TestCase):
    def test_compose_files_are_excluded(self) -> None:
        for path in (
            "src/store-admin/docker-compose.yml",
            "src/api/compose.yaml",
            "src/api/docker-compose.override.yml",
            "src/api/compose-dev.yml",
        ):
            with self.subTest(path=path):
                self.assertIs(classify_path(path), FileClass.EXCLUDED)
        self.assertIs(classify_path("src/api/composer.json"), FileClass.SCANNED)
        self.assertIs(classify_path("src/api/k8s/deployment.yml"), FileClass.SCANNED)


class DockerfileTests(unittest.TestCase):
    def test_final_stage_env_and_build_args(self) -> None:
        image = dockerfile_environment(
            """
            FROM node:20 AS build
            ENV BUILD_ONLY_URL=http://builder
            FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS base
            ENV ASPNETCORE_ENVIRONMENT=Staging
            FROM base AS final
            ARG VERSION
            ENV ORDERS_URL=http://orders:8080 \\
                BUILD_TAG=$VERSION
            ENV LEGACY_HOST legacy-host
            """
        )
        self.assertEqual(
            image.variables,
            {"ASPNETCORE_ENVIRONMENT": "Staging", "ORDERS_URL": "http://orders:8080", "LEGACY_HOST": "legacy-host"},
        )
        self.assertEqual(image.opaque, ["BUILD_TAG"])


# --- Resolution precedence -------------------------------------------------

API = _id("api")


def _binding(variable: str, *defaults: StaticDefault, framework: ConfigFramework = ConfigFramework.DOTNET, key: str = "") -> ConfigBinding:
    return ConfigBinding(variable=variable, key=key or variable.replace("__", ":"), framework=framework, defaults=list(defaults))


def _default(template: str, profile: str | None = None, source: StaticDefaultSource = StaticDefaultSource.CONFIG_FILE) -> StaticDefault:
    return StaticDefault(template=template, source=source, file_path=f"appsettings{'.' + profile if profile else ''}.json", profile=profile)


def _config(*bindings: ConfigBinding, **image: str) -> ServiceStaticConfig:
    return ServiceStaticConfig(
        repository="repo-a", root="src/api", service_name="api", bindings=list(bindings), image_variables=image,
        image_file="src/api/Dockerfile",
    )


def _env(**variables: str) -> ServiceEnvironment:
    return ServiceEnvironment(service_id=API, variables=variables)


EVENT_BUS = _binding(
    "ConnectionStrings__EventBus",
    _default("amqp://rabbitmq"),
    _default("amqp://rabbitmq-staging", profile="Staging"),
    _default("amqp://from-code", source=StaticDefaultSource.CODE),
)


class ResolutionPrecedenceTests(unittest.TestCase):
    def test_the_cluster_wins_and_matches_dotnet_names_case_insensitively(self) -> None:
        result = resolve_variable(
            "ConnectionStrings__EventBus",
            _config(EVENT_BUS),
            _env(CONNECTIONSTRINGS__EVENTBUS="Host=bus;Password=x"),
            None,
        )
        self.assertEqual((result.outcome, result.endpoint), (ResolutionOutcome.RUNTIME, "bus"))

    def test_base_file_default_under_the_default_profile(self) -> None:
        result = resolve_variable("ConnectionStrings__EventBus", _config(EVENT_BUS), _env(), None)
        self.assertEqual(
            (result.outcome, result.endpoint, result.origin, result.profile),
            (ResolutionOutcome.STATIC_DEFAULT, "amqp://rabbitmq", StaticDefaultOrigin.CONFIG_FILE, None),
        )

    def test_the_active_profile_file_wins(self) -> None:
        result = resolve_variable(
            "ConnectionStrings__EventBus", _config(EVENT_BUS), _env(ASPNETCORE_ENVIRONMENT="Staging"), None
        )
        self.assertEqual((result.endpoint, result.profile), ("amqp://rabbitmq-staging", "Staging"))

    def test_an_unknown_profile_with_differing_defaults_is_opaque(self) -> None:
        environment = ServiceEnvironment(service_id=API, opaque_variables=["ASPNETCORE_ENVIRONMENT"])
        result = resolve_variable("ConnectionStrings__EventBus", _config(EVENT_BUS), environment, None)
        self.assertIs(result.outcome, ResolutionOutcome.OPAQUE)

    def test_config_file_beats_code_default(self) -> None:
        code_only = _binding("Orders__Url", _default("http://from-code", source=StaticDefaultSource.CODE))
        result = resolve_variable("Orders__Url", _config(code_only), _env(), None)
        self.assertEqual((result.endpoint, result.origin), ("http://from-code", StaticDefaultOrigin.CODE))

    def test_image_env_beats_config_files(self) -> None:
        result = resolve_variable(
            "ConnectionStrings__EventBus",
            _config(EVENT_BUS, ConnectionStrings__EventBus="amqp://image-bus"),
            _env(),
            None,
        )
        self.assertEqual(
            (result.outcome, result.endpoint, result.origin, result.file_path),
            (ResolutionOutcome.STATIC_DEFAULT, "amqp://image-bus", StaticDefaultOrigin.IMAGE_ENV, "src/api/Dockerfile"),
        )

    def test_anything_that_may_set_it_blocks_defaults(self) -> None:
        cases: dict[str, tuple[ServiceEnvironment | None, PipelineVariables | None]] = {
            "environment not read": (None, None),
            "opaque in Kubernetes": (ServiceEnvironment(service_id=API, opaque_variables=["ConnectionStrings__EventBus"]), None),
            "imported Secret": (ServiceEnvironment(service_id=API, has_opaque_sources=True), None),
            "pipeline": (_env(), PipelineVariables(repository="repo-a", withheld_variables=["connectionstrings__eventbus"])),
        }
        for name, (environment, pipeline) in cases.items():
            with self.subTest(name):
                result = resolve_variable("ConnectionStrings__EventBus", _config(EVENT_BUS), environment, pipeline)
                self.assertIs(result.outcome, ResolutionOutcome.OPAQUE)

    def test_a_localhost_default_stays_unset(self) -> None:
        local = _binding("ConnectionStrings__Redis", _default("localhost"))
        result = resolve_variable("ConnectionStrings__Redis", _config(local), _env(), None)
        self.assertIs(result.outcome, ResolutionOutcome.UNSET)
        self.assertIn("localhost", result.detail)

    def test_spring_placeholders(self) -> None:
        url = _binding(
            "SPRING_DATASOURCE_URL",
            _default("jdbc:postgresql://${DB_HOST:postgres}:5432/db"),
            framework=ConfigFramework.SPRING,
            key="spring.datasource.url",
        )
        default = resolve_variable("SPRING_DATASOURCE_URL", _config(url), _env(), None)
        self.assertEqual((default.outcome, default.endpoint), (ResolutionOutcome.STATIC_DEFAULT, "postgresql://postgres:5432"))
        runtime = resolve_variable("SPRING_DATASOURCE_URL", _config(url), _env(DB_HOST="pg-prod"), None)
        self.assertEqual((runtime.outcome, runtime.endpoint), (ResolutionOutcome.RUNTIME, "postgresql://pg-prod:5432"))
        whole = resolve_variable(
            "SPRING_DATASOURCE_URL", _config(url), _env(SPRING_DATASOURCE_URL="jdbc:postgresql://other:5432/x"), None
        )
        self.assertEqual(whole.endpoint, "postgresql://other:5432")

    def test_spring_profiles(self) -> None:
        zone = _binding(
            "EUREKA_CLIENT_SERVICEURL_DEFAULTZONE",
            _default("http://discovery-server:8761/eureka/", profile="docker"),
            framework=ConfigFramework.SPRING,
            key="eureka.client.serviceUrl.defaultZone",
        )
        inactive = resolve_variable("EUREKA_CLIENT_SERVICEURL_DEFAULTZONE", _config(zone), _env(), None)
        self.assertIs(inactive.outcome, ResolutionOutcome.UNSET)
        active = resolve_variable(
            "EUREKA_CLIENT_SERVICEURL_DEFAULTZONE", _config(zone), _env(SPRING_PROFILES_ACTIVE="k8s,docker"), None
        )
        self.assertEqual(active.endpoint, "http://discovery-server:8761")


# --- Reconciliation ------------------------------------------------------------

SERVICES = [
    ServiceNode(service_id=API, name="api", metadata={"repository": "repo-a", "service_root": "src/api"}),
    ServiceNode(service_id=_id("rabbitmq"), name="rabbitmq"),
]


class ResolveEndpointTests(unittest.TestCase):
    def _resolve(self, config: ServiceStaticConfig | None, configs_known: bool = True) -> tuple[str, dict[str, UnresolvedEntity], dict[tuple[str, str], StaticDefaultUse]]:
        index = IdentityIndex(s.identity for s in SERVICES)
        unresolved: dict[str, UnresolvedEntity] = {}
        used: dict[tuple[str, str], StaticDefaultUse] = {}
        configs = static_config_index(SERVICES, [config] if config else []) if configs_known else None
        target = resolve_endpoint(
            env_var_reference("api", "ConnectionStrings__EventBus"),
            index,
            CONTEXT.in_namespace("shop"),
            {API: _env()},
            unresolved,
            {},
            configs,
            used,
        )
        return target, unresolved, used

    def test_a_default_resolves_to_a_registered_service_and_is_recorded(self) -> None:
        target, _, used = self._resolve(_config(_binding("ConnectionStrings__EventBus", _default("amqp://rabbitmq:5672"))))
        self.assertEqual(target, _id("rabbitmq"))
        use = used[(API, "ConnectionStrings__EventBus")]
        self.assertEqual((use.origin, use.file_path, use.target), (StaticDefaultOrigin.CONFIG_FILE, "appsettings.json", _id("rabbitmq")))

    def test_a_default_naming_an_unknown_service_is_that_gap(self) -> None:
        target, unresolved, _ = self._resolve(_config(_binding("ConnectionStrings__EventBus", _default("amqp://kafka"))))
        self.assertIs(unresolved[target].kind, UnresolvedEntityKind.UNREGISTERED_SERVICE)

    def test_no_default_is_unset(self) -> None:
        target, unresolved, used = self._resolve(_config())
        self.assertIs(unresolved[target].kind, UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET)
        self.assertEqual(used, {})

    def test_a_snapshot_without_config_records_resolves_as_before(self) -> None:
        target, unresolved, _ = self._resolve(None, configs_known=False)
        self.assertIs(unresolved[target].kind, UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET)


# --- Repository scan -----------------------------------------------------------


class ScanTests(unittest.TestCase):
    FILES: dict[str, str | None] = {
        "/src/Ordering.API/Dockerfile": "FROM mcr.microsoft.com/dotnet/aspnet:8.0\nENV ASPNETCORE_ENVIRONMENT=Staging\n",
        "/src/Ordering.API/appsettings.json": APPSETTINGS,
        "/src/Ordering.API/appsettings.Development.json": '{"Identity": {"Url": "http://localhost:5105"}}',
        "/src/Ordering.API/Program.cs": 'builder.AddRabbitMQClient("eventbus");\nvar c = new GrpcChannel("grpc://basket-api:81");',
        "/src/customers/Dockerfile": "FROM eclipse-temurin:21",
        "/src/customers/src/main/resources/application.yml": SPRING_YAML,
        "/src/customers/src/main/resources/application-broken.yml": "a: [",
        "/src/customers/src/main/java/App.java": '@FeignClient(name = "vets-service") interface Vets {}',
    }

    def test_framework_files_become_bindings_not_fixed_edges(self) -> None:
        result = _extractor(self.FILES).scan_repository("repo-a")
        scans = {scan.service.root: scan for scan in result.items[0].services}

        ordering = scans["src/Ordering.API"]
        env_refs = {r.endpoint for r in ordering.references if r.kind is ReferenceKind.ENV_VAR}
        url_targets = {r.dependency_target() for r in ordering.references if r.kind is ReferenceKind.URL}
        self.assertEqual(
            env_refs,
            {"ConnectionStrings__EventBus", "ConnectionStrings__OrderingDb", "Identity__Url"},
        )  # eventbus from code merged case-insensitively; OpenAI (public) dropped
        self.assertEqual(url_targets, {"basket-api"})  # identity-api is a default, not a fixed edge
        assert ordering.config is not None
        self.assertEqual(ordering.config.image_variables, {"ASPNETCORE_ENVIRONMENT": "Staging"})
        self.assertEqual(ordering.config.commit_sha, HEAD_SHA)
        identity = ordering.config.binding("identity__url")
        assert identity is not None
        self.assertEqual(
            sorted((d.template, d.profile or "") for d in identity.defaults),
            [("http://identity-api", ""), ("http://localhost:5105", "Development")],
        )

        customers = scans["src/customers"]
        self.assertIn("vets-service", {r.dependency_target() for r in customers.references})
        self.assertIn("SPRING_DATASOURCE_URL", {r.endpoint for r in customers.references})
        assert customers.coverage is not None
        self.assertEqual(
            [(f.path, f.reason) for f in customers.coverage.unscanned],
            [("src/customers/src/main/resources/application-broken.yml", UnscannedReason.PARSE_FAILED)],
        )
        self.assertNotIn("Pass@word", result.items[0].model_dump_json())


# --- Pull request head ---------------------------------------------------------


class HeadTopologyTests(unittest.TestCase):
    def test_a_changed_default_at_head_changes_the_edge(self) -> None:
        services = [*SERVICES, ServiceNode(service_id=_id("kafka"), name="kafka")]
        graph = KnowledgeGraph(
            services=services,
            edges=[
                DependencyEdge(
                    source_service_id=API,
                    target_service_id=_id("rabbitmq"),
                    discovery_methods=[DiscoveryMethod.STATIC_CODE],
                    confidence_score=0.7,
                    last_observed_at="2026-11-13T00:00:00Z",
                )
            ],
            service_environments=[_env()],
            static_config=[_config(_binding("ConnectionStrings__EventBus", _default("amqp://rabbitmq")))],
            static_defaults_used=[],
        )
        head = ServiceScan(
            service=ServiceBoundary(name="api", root="src/api", source=BoundarySource.MARKER),
            references=[
                devops_extractor.EndpointReference(
                    service_name="api", endpoint="ConnectionStrings__EventBus", file_path="x", kind=ReferenceKind.ENV_VAR
                )
            ],
            config=_config(_binding("ConnectionStrings__EventBus", _default("kafka:9092"))),
        )

        topology = head_topology(graph, CONTEXT.in_namespace("shop"), {"src/api": API}, [head])

        self.assertEqual([(c.source_id, c.target_id) for c in topology.added], [(API, _id("kafka"))])
        self.assertEqual([(c.source_id, c.target_id) for c in topology.removed], [(API, _id("rabbitmq"))])
        uses: Any = topology.graph.static_defaults_used
        self.assertEqual([u.target for u in uses], [_id("kafka")])


if __name__ == "__main__":
    unittest.main()
