"""PR 2b-3b: build-project boundaries, shared libraries, references not
understood, and unscoped telemetry."""

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from azure.monitor.query import LogsQueryStatus

from src.coordinator import nodes as coordinator_nodes
from src.coordinator.coverage import static_coverage_issues
from src.coordinator.file_mapping import map_changed_files
from src.coordinator.project_policy import (
    live_coverage,
    live_runtime_state,
    with_unscoped_sources,
)
from src.core.models import (
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    ConfigFramework,
    DiscoveryResult,
    KnowledgeGraph,
    OpaqueReferenceKind,
    ReasonCode,
    RepositoryCoverage,
    RuntimeEvidenceState,
    ServiceCoverage,
    ServiceNode,
    SharedLibrary,
    TelemetryClusterFilter,
    TelemetryScope,
    TelemetryStatus,
    UnscannedReason,
)
from src.tools.build_projects import (
    ProjectKind,
    build_layout,
    parse_csproj,
    parse_gradle,
    parse_pom,
    read_projects,
)
from src.tools.devops_extractor import FileClass, ReferenceKind, classify_path
from src.tools.policy_store import Exemptions, FileExemption
from src.tools.reference_sites import (
    BoundSection,
    bound_sections,
    opaque_reference_sites,
    unbound_section_sites,
)
from src.tools.telemetry_extractor import (
    TelemetryExtractor,
    build_unscoped_query,
    parse_unscoped_response,
)
from tests import test_live_evidence as live
from tests.test_coverage_and_shape import HEAD_SHA, _extractor


def _csproj(sdk: str = "Microsoft.NET.Sdk", body: str = "") -> str:
    return f'<Project Sdk="{sdk}">{body}</Project>'


def _refs(*paths: str) -> str:
    return "<ItemGroup>" + "".join(f'<ProjectReference Include="{p}" />' for p in paths) + "</ItemGroup>"


# --- Build projects ---------------------------------------------------------


class CsprojTests(unittest.TestCase):
    FILES = {"src/EventBus/EventBus.csproj", "src/Api/Api.csproj"}

    def kind(self, content: str) -> ProjectKind:
        return parse_csproj(content, "/src/Api/Api.csproj", self.FILES).kind

    def test_kinds(self) -> None:
        cases = {
            _csproj("Microsoft.NET.Sdk.Web"): ProjectKind.SERVICE,
            _csproj("Microsoft.NET.Sdk.Worker"): ProjectKind.SERVICE,
            _csproj(body="<PropertyGroup><OutputType>Exe</OutputType></PropertyGroup>"): ProjectKind.SERVICE,
            _csproj(): ProjectKind.LIBRARY,
            _csproj(
                body='<PropertyGroup><OutputType>Exe</OutputType></PropertyGroup>'
                '<ItemGroup><PackageReference Include="Microsoft.NET.Test.Sdk" /></ItemGroup>'
            ): ProjectKind.TEST,
            _csproj(body="<PropertyGroup><IsTestProject>true</IsTestProject></PropertyGroup>"): ProjectKind.TEST,
            '<Project Sdk="Microsoft.NET.Sdk"><Sdk Name="Aspire.AppHost.Sdk" Version="9.0.0" />'
            "<PropertyGroup><OutputType>Exe</OutputType></PropertyGroup></Project>": ProjectKind.APPHOST,
            _csproj(body="<PropertyGroup><IsAspireHost>true</IsAspireHost></PropertyGroup>"): ProjectKind.APPHOST,
            # eShop's tests: MSTest.Sdk with OutputType Exe.
            _csproj("MSTest.Sdk", "<PropertyGroup><OutputType>Exe</OutputType></PropertyGroup>"): ProjectKind.TEST,
            _csproj(
                body='<PropertyGroup><OutputType>Exe</OutputType></PropertyGroup>'
                '<ItemGroup><PackageReference Include="xunit.v3" /></ItemGroup>'
            ): ProjectKind.TEST,
            # A MAUI app runs on devices, not in the cluster.
            _csproj(
                body="<PropertyGroup><OutputType Condition=\"'$(TargetFramework)' != 'net10.0'\">Exe</OutputType>"
                "<UseMaui>true</UseMaui></PropertyGroup>"
            ): ProjectKind.CLIENT,
            _csproj("Microsoft.NET.Sdk", "<PropertyGroup><OutputType>WinExe</OutputType><UseWPF>true</UseWPF></PropertyGroup>"): ProjectKind.CLIENT,
            # Old-style project with an MSBuild namespace.
            '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
            "<PropertyGroup><OutputType>WinExe</OutputType></PropertyGroup></Project>": ProjectKind.SERVICE,
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                self.assertIs(self.kind(content), expected)

    def test_project_references_resolve_relative_to_the_file(self) -> None:
        content = _csproj(body=_refs(r"..\EventBus\EventBus.csproj", r"..\Missing\Missing.csproj", "$(Root)/X.csproj"))
        project = parse_csproj(content, "/src/Api/Api.csproj", self.FILES)

        self.assertEqual(project.root, "src/Api")
        self.assertEqual(project.references, ["src/EventBus"])
        self.assertEqual([site.detail for site in project.unresolved], [r"..\Missing\Missing.csproj", "$(Root)/X.csproj"])
        self.assertTrue(all(s.kind is OpaqueReferenceKind.PROJECT_REFERENCE_UNRESOLVED for s in project.unresolved))


class MavenGradleTests(unittest.TestCase):
    def test_pom_kinds_and_sibling_dependencies(self) -> None:
        parent = "<project><artifactId>parent</artifactId><packaging>pom</packaging></project>"
        service = (
            "<project><artifactId>customers</artifactId>"
            "<dependencies><dependency><artifactId>common</artifactId></dependency>"
            "<dependency><artifactId>spring-web</artifactId></dependency></dependencies>"
            "<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>"
            "</project>"
        )
        managed_only = (
            "<project><artifactId>common</artifactId><build><pluginManagement><plugins><plugin>"
            "<artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></pluginManagement></build></project>"
        )
        projects, failures = read_projects(
            {"/pom.xml": parent, "/customers/pom.xml": service, "/common/pom.xml": managed_only},
            ["/pom.xml", "/customers/pom.xml", "/common/pom.xml"],
        )
        by_root = {p.root: p for p in projects}

        self.assertEqual(failures, {})
        self.assertIs(by_root[""].kind, ProjectKind.AGGREGATOR)
        self.assertIs(by_root["customers"].kind, ProjectKind.SERVICE)
        self.assertIs(by_root["common"].kind, ProjectKind.LIBRARY)
        self.assertEqual(by_root["customers"].references, ["common"])
        self.assertEqual(parse_pom(parent, "/pom.xml").dependencies, [])

    def test_boot_plugin_is_inherited_from_a_parent_profile(self) -> None:
        # spring-petclinic-microservices: the parent applies the plugin in a
        # profile activated by a file each service module has.
        parent = (
            "<project><artifactId>parent</artifactId><packaging>pom</packaging><profiles>"
            "<profile><activation><file><exists>${basedir}/src/main/resources/application.yml</exists></file></activation>"
            "<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></profile>"
            "</profiles></project>"
        )
        child = "<project><artifactId>{0}</artifactId><parent><artifactId>parent</artifactId></parent></project>"
        files = {
            "/pom.xml": parent,
            "/customers/pom.xml": child.format("customers"),
            "/common/pom.xml": child.format("common"),
        }
        paths = [*files, "/customers/src/main/resources/application.yml"]
        projects, _ = read_projects(files, paths)
        kinds = {p.root: p.kind for p in projects}
        self.assertIs(kinds["customers"], ProjectKind.SERVICE)
        self.assertIs(kinds["common"], ProjectKind.LIBRARY)

        default_on = parent.replace(
            "<file><exists>${basedir}/src/main/resources/application.yml</exists></file>",
            "<activeByDefault>true</activeByDefault>",
        )
        projects, _ = read_projects({**files, "/pom.xml": default_on}, paths)
        self.assertIs({p.root: p.kind for p in projects}["common"], ProjectKind.SERVICE)

    def test_gradle_kinds_and_project_references(self) -> None:
        settings = {"": "rootProject.name = 'shop'\ninclude 'api', 'libs:core'\n"}
        directories = {"", "api", "libs", "libs/core"}
        api = parse_gradle(
            "plugins {\n  id 'org.springframework.boot' version '3.3.0'\n}\n"
            "dependencies {\n  implementation project(':libs:core')\n  implementation project(':gone')\n}\n",
            "/api/build.gradle",
            settings,
            directories,
        )
        cli = parse_gradle('plugins {\n    application\n}\n', "/cli/build.gradle.kts", settings, directories)
        core = parse_gradle("plugins { id 'java-library' }\n", "/libs/core/build.gradle", settings, directories)
        root = parse_gradle("subprojects { apply plugin: 'org.springframework.boot' }", "/build.gradle", settings, directories)

        self.assertIs(api.kind, ProjectKind.SERVICE)
        self.assertEqual(api.references, ["libs/core"])
        self.assertEqual([(s.line, s.detail) for s in api.unresolved], [(6, ":gone")])
        self.assertIs(cli.kind, ProjectKind.SERVICE)
        self.assertIs(core.kind, ProjectKind.LIBRARY)
        self.assertIs(root.kind, ProjectKind.AGGREGATOR)

    def test_unparseable_build_file_is_reported(self) -> None:
        _, failures = read_projects({"/a/A.csproj": "<Project"}, ["/a/A.csproj"])
        self.assertIn("a/A.csproj", failures)


class LayoutTests(unittest.TestCase):
    def test_libraries_are_consumed_transitively_and_tests_are_carved_out(self) -> None:
        files = {
            "src/Api/Api.csproj", "src/Worker/Worker.csproj", "src/Core/Core.csproj",
            "src/Infra/Infra.csproj", "src/Unused/Unused.csproj", "src/Api/Tests/Tests.csproj",
        }
        projects = [
            parse_csproj(_csproj("Microsoft.NET.Sdk.Web", _refs(r"..\Infra\Infra.csproj")), "src/Api/Api.csproj", files),
            parse_csproj(_csproj("Microsoft.NET.Sdk.Worker", _refs(r"..\Core\Core.csproj")), "src/Worker/Worker.csproj", files),
            parse_csproj(_csproj(body=_refs(r"..\Core\Core.csproj")), "src/Infra/Infra.csproj", files),
            parse_csproj(_csproj(), "src/Core/Core.csproj", files),
            parse_csproj(_csproj(), "src/Unused/Unused.csproj", files),
            parse_csproj(_csproj(body="<PropertyGroup><IsTestProject>true</IsTestProject></PropertyGroup>"), "src/Api/Tests/Tests.csproj", files),
        ]
        layout = build_layout(projects, ["src/Api", "src/Worker"])

        self.assertEqual(layout.owner("src/Api/Program.cs").services, ["src/Api"])
        self.assertEqual(layout.owner("src/Core/Bus.cs").services, ["src/Api", "src/Worker"])
        self.assertEqual(layout.owner("src/Core/Bus.cs").library, "src/Core")
        self.assertEqual(layout.owner("src/Infra/Db.cs").services, ["src/Api"])
        self.assertEqual(layout.owner("src/Unused/X.cs").services, [])
        self.assertEqual(layout.owner("src/Api/Tests/ApiTests.cs").services, [])
        self.assertEqual(layout.libraries_of("src/Api"), ["src/Core", "src/Infra"])
        self.assertEqual(
            [(lib.root, lib.consumers) for lib in layout.shared_libraries()],
            [("src/Core", ["src/Api", "src/Worker"]), ("src/Infra", ["src/Api"])],
        )


class ClassifyBuildPathTests(unittest.TestCase):
    def test_build_output_and_definitions(self) -> None:
        dotnet = frozenset({"src/Api"})
        cases = {
            "/src/Api/obj/project.assets.json": FileClass.EXCLUDED,
            "/src/Api/bin/Debug/appsettings.json": FileClass.EXCLUDED,
            "/web/bin/www": FileClass.NOT_SOURCE,
            "/web/bin/www.js": FileClass.SCANNED,
            "/api/.gradle/cache.properties": FileClass.EXCLUDED,
            "/api/build.gradle.kts": FileClass.NOT_SOURCE,
            "/settings.gradle.kts": FileClass.NOT_SOURCE,
            "/src/Api/Api.csproj": FileClass.NOT_SOURCE,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertIs(classify_path(path, dotnet), expected)


# --- Repository scans -------------------------------------------------------


ESHOP: dict[str, str | None] = {
    "/eShop.sln": "",
    "/src/Basket.API/Basket.API.csproj": _csproj("Microsoft.NET.Sdk.Web", _refs(r"..\EventBus\EventBus.csproj")),
    "/src/Basket.API/Program.cs": 'var url = builder.Configuration["Identity:Url"];',
    "/src/Basket.API/appsettings.json": '{"Identity": {"Url": "http://identity-api"}}',
    "/src/Basket.API/bin/Debug/net9.0/appsettings.json": "{not json",
    "/src/Basket.API/obj/project.assets.json": "{}",
    "/src/Ordering.API/Ordering.API.csproj": _csproj("Microsoft.NET.Sdk.Web", _refs(r"..\Ordering.Infrastructure\Ordering.Infrastructure.csproj")),
    "/src/Ordering.API/Program.cs": "app.Run();",
    "/src/Ordering.Infrastructure/Ordering.Infrastructure.csproj": _csproj(body=_refs(r"..\EventBus\EventBus.csproj")),
    "/src/Ordering.Infrastructure/Db.cs": 'var cs = configuration.GetConnectionString("orderingdb");',
    "/src/EventBus/EventBus.csproj": _csproj(),
    "/src/EventBus/Bus.cs": 'var host = configuration[key];\nvar amqp = "amqp://eventbus:5672";',
    "/src/eShop.AppHost/eShop.AppHost.csproj": _csproj(body="<PropertyGroup><IsAspireHost>true</IsAspireHost></PropertyGroup>"),
    "/src/eShop.AppHost/Program.cs": 'builder.AddProject("basket-api");',
    "/tests/Basket.UnitTests/Basket.UnitTests.csproj": _csproj(body='<ItemGroup><PackageReference Include="Microsoft.NET.Test.Sdk" /></ItemGroup>'),
    "/tests/Basket.UnitTests/BasketTests.cs": 'new Uri("http://basket-api");',
}


class EshopScanTests(unittest.TestCase):
    def setUp(self) -> None:
        result = _extractor(dict(ESHOP)).scan_repository("repo-a")
        self.assertEqual(result.errors, [])
        (self.scan,) = result.items
        self.services = {s.service.name: s for s in self.scan.services}

    def test_deep_runnable_projects_are_services(self) -> None:
        self.assertEqual(sorted(self.services), ["basket-api", "ordering-api"])
        self.assertEqual(self.services["basket-api"].service.root, "src/Basket.API")

    def test_build_output_is_excluded(self) -> None:
        coverage = self.services["basket-api"].coverage
        assert coverage is not None
        self.assertEqual(coverage.files_excluded, 2)
        self.assertNotIn(UnscannedReason.PARSE_FAILED, {u.reason for u in coverage.unscanned})

    def test_library_code_counts_for_each_consumer_transitively(self) -> None:
        ordering = self.services["ordering-api"]
        basket = self.services["basket-api"]
        assert ordering.coverage is not None and basket.coverage is not None
        self.assertEqual(ordering.coverage.libraries, ["src/EventBus", "src/Ordering.Infrastructure"])
        self.assertEqual(basket.coverage.libraries, ["src/EventBus"])
        for scan in (ordering, basket):
            with self.subTest(service=scan.service.name):
                urls = [(r.endpoint, r.service_name) for r in scan.references if r.kind is ReferenceKind.URL]
                self.assertIn(("amqp://eventbus:5672", scan.service.name), urls)
        assert ordering.config is not None
        self.assertIsNotNone(ordering.config.binding("ConnectionStrings__orderingdb"))
        self.assertEqual(
            [(lib.root, lib.consumers) for lib in self.scan.libraries],
            [("src/EventBus", ["src/Basket.API", "src/Ordering.API"]),
             ("src/Ordering.Infrastructure", ["src/Ordering.API"])],
        )

    def test_library_sites_are_not_understood_in_every_consumer(self) -> None:
        for name in ("basket-api", "ordering-api"):
            coverage = self.services[name].coverage
            assert coverage is not None
            (opaque,) = [u for u in coverage.unscanned if u.reason is UnscannedReason.REFERENCES_NOT_UNDERSTOOD]
            self.assertEqual(opaque.path, "src/EventBus/Bus.cs")
            self.assertEqual([(s.line, s.kind) for s in opaque.sites], [(1, OpaqueReferenceKind.DYNAMIC_CONFIG_KEY)])

    def test_apphost_and_test_code_are_excluded(self) -> None:
        self.assertNotIn("src/eShop.AppHost/Program.cs", self.scan.unowned_source_files)
        # Test code never runs in production: excluded, not reported as unowned.
        self.assertNotIn("tests/Basket.UnitTests/BasketTests.cs", self.scan.unowned_source_files)

    def test_head_scan_of_one_service_reads_its_libraries_only(self) -> None:
        result = _extractor(dict(ESHOP)).scan_repository("repo-a", only_roots={"src/Basket.API"})
        (scan,) = result.items[0].services
        assert scan.coverage is not None
        self.assertEqual(scan.coverage.libraries, ["src/EventBus"])
        self.assertEqual(scan.coverage.files_scanned, 3)  # Program.cs, appsettings.json, Bus.cs

    def test_head_scan_doesnt_swallow_a_nested_service(self) -> None:
        files: dict[str, str | None] = {
            "/Dockerfile": "FROM x",
            "/app.py": "x",
            "/services/inner/Inner.csproj": _csproj("Microsoft.NET.Sdk.Web"),
            "/services/inner/Program.cs": 'new Uri("http://inner-dep");',
        }
        result = _extractor(files).scan_repository("repo-a", only_roots={""})
        (scan,) = result.items[0].services
        assert scan.coverage is not None
        self.assertEqual(scan.coverage.files_scanned, 2)
        self.assertEqual([r.endpoint for r in scan.references], [])


class SpringScanTests(unittest.TestCase):
    FILES: dict[str, str | None] = {
        "/pom.xml": "<project><artifactId>petclinic</artifactId><packaging>pom</packaging></project>",
        "/customers-service/pom.xml": (
            "<project><artifactId>customers-service</artifactId>"
            "<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></project>"
        ),
        "/customers-service/src/main/java/Config.java": (
            '@ConfigurationProperties(prefix = "visits-client")\nclass VisitsProps {}\n'
            '@ConfigurationProperties("vets")\nclass VetsProps {}\n'
        ),
        "/customers-service/src/main/resources/application.yml": "visits_client:\n  base-url: http://visits-service\n",
    }

    def test_bound_section_without_keys_is_not_understood(self) -> None:
        result = _extractor(dict(self.FILES)).scan_repository("repo-a")
        (scan,) = result.items[0].services
        self.assertEqual(scan.service.name, "customers-service")
        assert scan.coverage is not None
        (opaque,) = scan.coverage.unscanned
        self.assertEqual(opaque.path, "customers-service/src/main/java/Config.java")
        self.assertEqual(
            [(s.line, s.kind, s.detail) for s in opaque.sites],
            [(3, OpaqueReferenceKind.UNBOUND_SECTION, "vets")],
        )


# --- References not understood ---------------------------------------------


class OpaqueSiteTests(unittest.TestCase):
    def kinds(self, content: str, path: str) -> list[tuple[int, OpaqueReferenceKind]]:
        return [(site.line, site.kind) for site in opaque_reference_sites(content, path)]

    def test_dynamic_keys_and_names(self) -> None:
        dynamic = OpaqueReferenceKind.DYNAMIC_CONFIG_KEY
        env = OpaqueReferenceKind.DYNAMIC_ENV_NAME
        cases = [
            ('var a = configuration["A:B"];\nvar b = configuration[key];', "x.cs", [(2, dynamic)]),
            ('cfg.GetValue<string>($"Services:{name}:Url");', "x.cs", [(1, dynamic)]),
            # nameof(Options) is a literal section (bound_sections), a variable is not.
            ('config.GetSection(nameof(Options));\nconfig.GetSection("Ok");', "x.cs", []),
            ("x.BindConfiguration(sectionName);", "x.cs", [(1, dynamic)]),
            # Aspire clients: a non-literal connection name hides the connection;
            # EF Core's options lambda is not a name.
            ('b.AddRabbitMQClient(connectionName);\nb.AddRedisClient("redis");', "x.cs", [(1, dynamic)]),
            ("s.AddDbContext<C>(options => options.UseNpgsql(cs));", "x.cs", []),
            ("Environment.GetEnvironmentVariable(name);", "x.cs", [(1, env)]),
            ('env.getProperty(key);\nenv.getProperty("a.b");', "X.java", [(1, dynamic)]),
            ('System.getenv(name);\nSystem.getenv().get(name);\nSystem.getenv("A");', "X.java", [(1, env), (2, env)]),
            ('os.getenv(name)\nos.environ[key]\nos.getenv("A")\nos.environ.get(f"{p}_URL")', "a.py", [(1, env), (2, env), (4, env)]),
            ("process.env[name];\nprocess.env[`A`];\nprocess.env[`${p}_URL`];", "a.ts", [(1, env), (3, env)]),
            ('os.Getenv(key)\nos.Getenv("A")', "a.go", [(1, env)]),
            ('ENV[name]\nENV.fetch("A")', "a.rb", [(1, env)]),
            ("// configuration[key]\n# os.getenv(name)", "x.cs", []),
        ]
        for content, path, expected in cases:
            with self.subTest(content=content):
                self.assertEqual(self.kinds(content, path), expected)

    def test_client_addresses(self) -> None:
        address = OpaqueReferenceKind.CLIENT_ADDRESS_EXPRESSION
        cases = [
            ('client.BaseAddress = new Uri("http://catalog-api");', "x.cs", []),
            # C# 9 target-typed new, as eShop registers its clients.
            ('AddHttpClient<C>(o => o.BaseAddress = new("https+http://catalog-api"));', "x.cs", []),
            ('AddGrpcClient<B>(o => o.Address = new("http://basket-api"));', "x.cs", []),
            ("AddGrpcClient<B>(o => o.Address = new(settings.Url));", "x.cs", [(1, address)]),
            ('c.BaseAddress = new Uri(builder.Configuration["Catalog:Url"]!);', "x.cs", []),
            ('c.BaseAddress = new Uri($"https+http://catalog-api/");', "x.cs", []),
            ('var url = configuration.GetValue<string>("Catalog:Url");\nc.BaseAddress = new Uri(url);', "x.cs", []),
            ("c.BaseAddress = new Uri(options.Value.CatalogUrl);", "x.cs", [(1, address)]),
            ("var channel = GrpcChannel.ForAddress(address);", "x.cs", [(1, address)]),
            ('o.Address = new Uri(Environment.GetEnvironmentVariable("BASKET_URL"));', "x.cs", []),
            ('customer.Address = dto.Address;', "x.cs", []),
            ('WebClient.builder().baseUrl("http://vets-service").build();', "X.java", []),
            ('@Value("${vets.url}")\nprivate String vetsUrl;\nWebClient.create(vetsUrl);', "X.java", []),
            ("WebClient.builder().baseUrl(props.getUrl());", "X.java", [(1, address)]),
            ('ManagedChannelBuilder.forTarget(target).build();', "X.java", [(1, address)]),
        ]
        for content, path, expected in cases:
            with self.subTest(content=content):
                self.assertEqual(self.kinds(content, path), expected)

    def test_bound_sections(self) -> None:
        sections = bound_sections(
            'services.Configure<A>(config.GetSection("Catalog"));\n'
            'services.AddOptions<B>().BindConfiguration("Basket");\n'
            'config.GetSection("Kestrel");',
            "x.cs",
        )
        self.assertEqual([(s.section, s.line) for s in sections], [("Catalog", 1), ("Basket", 2)])
        keys = {ConfigFramework.DOTNET: {"catalog:Url"}}
        self.assertEqual([s.detail for s in unbound_section_sites(sections, keys)], ["Basket"])

    def test_nameof_binds_the_named_section(self) -> None:
        # eShop: AddOptions<PaymentOptions>().BindConfiguration(nameof(PaymentOptions)).
        sections = bound_sections(
            "b.AddOptions<P>().BindConfiguration(nameof(PaymentOptions));\n"
            "c.GetSection(nameof(Settings.Catalog));",
            "x.cs",
        )
        self.assertEqual([s.section for s in sections], ["PaymentOptions", "Catalog"])
        self.assertEqual(
            [s.detail for s in unbound_section_sites(sections, {ConfigFramework.DOTNET: set()})],
            ["PaymentOptions", "Catalog"],
        )

    def test_a_section_read_from_a_section_is_relative(self) -> None:
        (scopes,) = bound_sections('identitySection.GetRequiredSection("Scopes").GetChildren();', "x.cs")
        self.assertEqual(unbound_section_sites([scopes], {ConfigFramework.DOTNET: {"Identity:Scopes:orders"}}), [])
        self.assertEqual(len(unbound_section_sites([scopes], {ConfigFramework.DOTNET: {"Identity:Url"}})), 1)

    def test_spring_sections_bind_relaxed(self) -> None:
        section = BoundSection(section="visits-client", framework=ConfigFramework.SPRING, line=1)
        self.assertEqual(unbound_section_sites([section], {ConfigFramework.SPRING: {"visitsClient.baseUrl"}}), [])
        self.assertEqual(len(unbound_section_sites([section], {ConfigFramework.SPRING: {"visits.url"}})), 1)


class OpaqueCoverageGateTests(unittest.TestCase):
    def _graph(self) -> KnowledgeGraph:
        result = _extractor(dict(ESHOP)).scan_repository("repo-a")
        services = [
            ServiceNode(
                service_id=f"urn:release-coord:acme:prod:aks-1:shop:{scan.service.name}",
                name=scan.service.name,
                metadata={REPOSITORY_METADATA_KEY: "repo-a", SERVICE_ROOT_METADATA_KEY: scan.service.root},
            )
            for scan in result.items[0].services
        ]
        return KnowledgeGraph(services=services, edges=[], static_coverage=[result.items[0].coverage()])

    def test_not_understood_blocks_and_is_exemptible(self) -> None:
        graph = self._graph()
        issues, exempted = static_coverage_issues(None, graph, "snap")
        self.assertEqual({i.code for i in issues}, {ReasonCode.STATIC_COVERAGE_INCOMPLETE})
        self.assertEqual(len(issues), 2)
        self.assertIn("src/EventBus/Bus.cs:1 dynamic config key", issues[0].detail)
        self.assertEqual(exempted, [])

        policy = live.POLICY.model_copy(
            update={
                "exemptions": Exemptions(
                    unscanned_files=[FileExemption(pattern="src/EventBus/**", reason="reviewed: key is the bus host")]
                )
            }
        )
        issues, exempted = static_coverage_issues(policy, graph, "snap")
        self.assertEqual(issues, [])
        self.assertEqual(len(exempted), 2)


# --- Changed files of a shared library -------------------------------------


def _service(name: str, root: str) -> ServiceNode:
    return ServiceNode(
        service_id=f"urn:release-coord:acme:prod:aks-1:shop:{name}",
        name=name,
        metadata={REPOSITORY_METADATA_KEY: "repo-a", SERVICE_ROOT_METADATA_KEY: root},
    )


class LibraryFileMappingTests(unittest.TestCase):
    def _graph(self, consumers: list[str]) -> KnowledgeGraph:
        return KnowledgeGraph(
            services=[_service("basket-api", "src/Basket.API"), _service("ordering-api", "src/Ordering.API")],
            edges=[],
            static_coverage=[
                RepositoryCoverage(
                    repository="repo-a",
                    commit_sha=HEAD_SHA,
                    services=[ServiceCoverage(service_name="basket-api", root="src/Basket.API")],
                    libraries=[SharedLibrary(root="src/EventBus", project_file="src/EventBus/EventBus.csproj", consumers=consumers)],
                )
            ],
        )

    def test_library_change_targets_every_consumer(self) -> None:
        mapping = map_changed_files(
            self._graph(["src/Basket.API", "src/Ordering.API"]), "REPO-A", ["src/EventBus/Bus.cs", "README.md"]
        )
        self.assertEqual([t.rsplit(":", 1)[1] for t in mapping.target_services], ["basket-api", "ordering-api"])
        self.assertEqual(mapping.unmapped_files, ["README.md"])

    def test_unregistered_consumer_keeps_the_file_unmapped(self) -> None:
        mapping = map_changed_files(
            self._graph(["src/Basket.API", "src/Gone.API"]), "repo-a", ["src/EventBus/Bus.cs"]
        )
        self.assertEqual([t.rsplit(":", 1)[1] for t in mapping.target_services], ["basket-api"])
        self.assertEqual(mapping.unmapped_files, ["src/EventBus/Bus.cs"])


# --- Unscoped telemetry -----------------------------------------------------


CLUSTER = TelemetryScope(cluster=TelemetryClusterFilter(value="aks-1"))


class UnscopedTelemetryTests(unittest.TestCase):
    def test_query_counts_rows_without_the_cluster_attribute(self) -> None:
        query = build_unscoped_query(3, CLUSTER)
        self.assertIn('isempty(tostring(Properties["k8s.cluster.name"]))', query)
        self.assertIn("summarize Rows=count() by Source=SourceName", query)
        with self.assertRaises(ValueError):
            build_unscoped_query(0, CLUSTER)

    def test_parse_response(self) -> None:
        table = SimpleNamespace(name="PrimaryResult", columns=["Source", "Rows"], rows=[["orders", 12], ["", 3]])
        response = SimpleNamespace(status=LogsQueryStatus.SUCCESS, tables=[table])
        self.assertEqual(parse_unscoped_response(response), {"orders": 12})  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            parse_unscoped_response(SimpleNamespace(status=LogsQueryStatus.PARTIAL))  # type: ignore[arg-type]

    def test_extractor_never_raises_and_skips_without_filter(self) -> None:
        extractor = TelemetryExtractor.__new__(TelemetryExtractor)
        extractor._additional_workspaces = []
        extractor._source = "azure"
        extractor._workspace_id = "ws"
        extractor._client = MagicMock()
        extractor._client.query_workspace.side_effect = RuntimeError("boom")
        self.assertIsNone(extractor.get_unscoped_sources(3, CLUSTER))
        self.assertIsNone(extractor.get_unscoped_sources(3, None))
        extractor._source = "mock"
        self.assertIsNone(extractor.get_unscoped_sources(3, CLUSTER))

    def test_uncovered_service_seen_only_unscoped_is_explained(self) -> None:
        radius = {live.FRONT, live.ORDER}
        coverage = live_coverage(live.POLICY, [live._observed(live.FRONT, live.RABBIT)], radius, live.NOW)
        order_name = live.ORDER.rsplit(":", 1)[1]
        coverage = with_unscoped_sources(coverage, {f"shop.{order_name}": 40, "other": 2})

        self.assertEqual(coverage.uncovered_services, [live.ORDER])
        self.assertEqual(coverage.unscoped_services, {live.ORDER: [f"shop.{order_name}"]})
        state, why = live_runtime_state(live.POLICY, coverage, live.NOW)
        self.assertIs(state, RuntimeEvidenceState.LIVE_UNCOVERED)
        self.assertIn("without the cluster attribute", why)
        self.assertIn(f"shop.{order_name}: 40 rows", why)

    def test_node_runs_the_diagnostic_only_for_uncovered_radius(self) -> None:
        state: dict[str, object] = {
            "workspace_id": live.WORKSPACE,
            "tenant": "acme",
            "project_id": "shop",
            "project_policy": live.LOADED,
            "baseline_snapshot_id": "snap",
            "baseline_graph": live.GRAPH,
            "target_services": [live.ORDER],
            "upstream_services": [live.FRONT],
            "downstream_services": [],
        }
        with patch.object(coordinator_nodes, "TelemetryExtractor") as extractor_cls, patch.object(
            coordinator_nodes, "utc_now", return_value=live.NOW
        ):
            extractor = extractor_cls.return_value
            extractor.is_mock = False
            extractor.get_active_dependencies.return_value = DiscoveryResult.success(
                [live._edge("shop/front", "rabbitmq", live.NOW - timedelta(minutes=1))]
            )
            extractor.get_runtime_metrics.return_value = {}
            extractor.dropped_records = {}
            extractor.get_unscoped_sources.return_value = {live.ORDER.rsplit(":", 1)[1]: 7}
            update = coordinator_nodes.validate_runtime_telemetry_node(state)  # type: ignore[arg-type]

        status = update["telemetry_status"]
        assert isinstance(status, TelemetryStatus) and status.coverage is not None
        extractor.get_unscoped_sources.assert_called_once()
        self.assertEqual(list(status.coverage.unscoped_services), [live.ORDER])


if __name__ == "__main__":
    unittest.main()
