"""One-hop following of file-local helpers through syntax trees (4d)."""

import unittest
from unittest.mock import patch

from src.core.models import OpaqueReferenceKind
from src.tools.devops_extractor import read_source_file
from src.tools.reference_sites import opaque_reference_sites
from src.tools.syntax_wrappers import (
    FollowedRead,
    SyntaxLanguage,
    WrapperSinkKind,
    follow_wrappers,
    literal,
    parse,
)

CSHARP = """
namespace Shop;
class Clients
{
    string Url(string key, string fallback) => _configuration[key] ?? fallback;

    string Db(string name) => _configuration.GetConnectionString(name);

    HttpClient Create(string address)
    {
        return new HttpClient { BaseAddress = new Uri(address) };
    }

    void Register()
    {
        var basket = Url(
            "Services:BasketUrl",
            "http://basket-api");
        var orders = Db("OrdersDb");
        var http = Create(_configuration["Services:CatalogUrl"]);
    }
}
"""


def _pairs(reads: list[FollowedRead]) -> set[tuple[WrapperSinkKind, str, str | None]]:
    return {(read.kind, read.name, read.default) for read in reads}


class SyntaxWrapperTests(unittest.TestCase):
    def test_csharp_config_helpers_multi_line_calls_and_address_helpers(self) -> None:
        analysis = follow_wrappers(CSHARP, "Clients.cs")
        self.assertTrue(analysis.parsed)
        self.assertEqual(
            _pairs(analysis.reads),
            {
                (WrapperSinkKind.CONFIG_KEY, "Services:BasketUrl", "http://basket-api"),
                (WrapperSinkKind.CONFIG_KEY, "ConnectionStrings:OrdersDb", None),
            },
        )
        self.assertEqual(
            {(sink.kind, sink.line) for sink in analysis.followed_sinks},
            {
                (WrapperSinkKind.CONFIG_KEY, 5),
                (WrapperSinkKind.CONFIG_KEY, 7),
                (WrapperSinkKind.CLIENT_ADDRESS, 11),
            },
        )

    def test_followed_helpers_add_bindings_and_clear_their_opaque_sites(self) -> None:
        self.assertTrue(opaque_reference_sites(CSHARP, "Clients.cs"))
        read = read_source_file(CSHARP, "web", "/src/Web/Clients.cs")
        self.assertEqual(read.sites, [])
        bindings = {binding.key: [d.template for d in binding.defaults] for binding in read.bindings}
        self.assertEqual(bindings["Services:BasketUrl"], ["http://basket-api"])
        self.assertIn("ConnectionStrings:OrdersDb", bindings)

    def test_public_or_delegated_or_variable_helpers_are_not_followed(self) -> None:
        public = "class A { public string Env(string n) => Environment.GetEnvironmentVariable(n); void M() { Env(\"A_URL\"); } }"
        delegated = (
            "class A { string Env(string n) => Environment.GetEnvironmentVariable(n);"
            " void M() { Env(\"A_URL\"); names.Select(Env); } }"
        )
        variable = "class A { string Env(string n) => Environment.GetEnvironmentVariable(n); void M() { Env(name); } }"
        overloaded = (
            "class A { string Env(string n) => Environment.GetEnvironmentVariable(n);"
            " string Env(int i) => i.ToString(); void M() { Env(\"A_URL\"); } }"
        )
        for source in (public, delegated, variable, overloaded):
            with self.subTest(source=source):
                analysis = follow_wrappers(source, "A.cs")
                self.assertTrue(analysis.parsed)
                self.assertEqual(analysis.reads, [])
                self.assertEqual(analysis.followed_sinks, [])
                read = read_source_file(source, "a", "/a/A.cs")
                self.assertIn(OpaqueReferenceKind.DYNAMIC_ENV_NAME, [site.kind for site in read.sites])

    def test_java_ternary_default_and_client_helper(self) -> None:
        java = """
            class Shipping {
                private String getenv(String key, String def) {
                    String val = System.getenv(key);
                    return val == null ? def : val;
                }
                private WebClient client(String url) { return WebClient.create(url); }
                void build() {
                    getenv("CART_ENDPOINT", "cart");
                    client(System.getenv("RATINGS_URL"));
                    client(props.getProperty("catalog.url"));
                }
            }
        """
        analysis = follow_wrappers(java, "Shipping.java")
        self.assertEqual(_pairs(analysis.reads), {(WrapperSinkKind.ENV_NAME, "CART_ENDPOINT", "cart")})
        self.assertIn(WrapperSinkKind.CLIENT_ADDRESS, [sink.kind for sink in analysis.followed_sinks])
        self.assertEqual(read_source_file(java, "shipping", "/shipping/Shipping.java").sites, [])

    def test_java_client_helper_called_with_an_unknown_address_keeps_its_site(self) -> None:
        java = """
            class A {
                private WebClient client(String url) { return WebClient.create(url); }
                void build() { client(computeUrl()); }
            }
        """
        self.assertEqual(follow_wrappers(java, "A.java").followed_sinks, [])
        kinds = [site.kind for site in read_source_file(java, "a", "/a/A.java").sites]
        self.assertIn(OpaqueReferenceKind.CLIENT_ADDRESS_EXPRESSION, kinds)

    def test_python_methods_keywords_nested_functions_and_fallbacks(self) -> None:
        python = (
            "import os\n"
            "class Settings:\n"
            "    def _env(self, name, default='http://localhost:8080'):\n"
            "        return os.environ.get(name, default)\n"
            "    def load(self):\n"
            "        self._env('CATALOG_URL')\n"
            "        self._env(name='CART_URL', default='http://cart:80')\n"
            "def main():\n"
            "    def host(n):\n"
            "        return os.getenv(n) or 'redis'\n"
            "    host('REDIS_HOST')\n"
        )
        self.assertEqual(
            _pairs(follow_wrappers(python, "settings.py").reads),
            {
                (WrapperSinkKind.ENV_NAME, "CATALOG_URL", "http://localhost:8080"),
                (WrapperSinkKind.ENV_NAME, "CART_URL", "http://cart:80"),
                (WrapperSinkKind.ENV_NAME, "REDIS_HOST", "redis"),
            },
        )

    def test_javascript_arrow_helpers_and_exports(self) -> None:
        javascript = (
            "const env = (n, d) => process.env[n] || d;\n"
            "export function exported(n) { return process.env[n]; }\n"
            "function viaModule(n) { return process.env[n]; }\n"
            "module.exports = { viaModule };\n"
            "env('CATALOGUE_URL', 'http://catalogue');\n"
            "exported('A_URL'); viaModule('B_URL');\n"
        )
        analysis = follow_wrappers(javascript, "server.js")
        self.assertEqual(_pairs(analysis.reads), {(WrapperSinkKind.ENV_NAME, "CATALOGUE_URL", "http://catalogue")})
        self.assertEqual([sink.line for sink in analysis.followed_sinks], [1])

    def test_typescript_and_tsx_grammars(self) -> None:
        typescript = "function env(n: string): string { return process.env[n] as string; }\nenv('ORDERS_URL');\n"
        self.assertEqual(
            [read.language for read in follow_wrappers(typescript, "a.ts").reads], [SyntaxLanguage.TYPESCRIPT]
        )
        tsx = "const env = (n: string) => process.env[n];\nconst A = () => <div>{env('UI_URL')}</div>;\n"
        self.assertEqual([read.name for read in follow_wrappers(tsx, "a.tsx").reads], ["UI_URL"])

    def test_unparseable_or_unknown_files_fall_back_to_regex_findings(self) -> None:
        broken = "class A { string Env(string n) => Environment.GetEnvironmentVariable(n); void M( { Env(\"A\"); }"
        self.assertIsNone(parse(broken, "A.cs"))
        self.assertFalse(follow_wrappers(broken, "A.cs").parsed)
        self.assertFalse(follow_wrappers("func main() {}", "main.go").parsed)

    def test_without_tree_sitter_the_regex_scanner_stands_alone(self) -> None:
        java = "class A { private String env(String n) { return System.getenv(n); } void m() { env(name); } }"
        with patch("src.tools.syntax_wrappers._parser", return_value=None):
            self.assertFalse(follow_wrappers(CSHARP, "Clients.cs").parsed)
            read = read_source_file(java, "a", "/a/A.java")
        self.assertIn(OpaqueReferenceKind.DYNAMIC_ENV_NAME, [site.kind for site in read.sites])

    def test_literal_rejects_interpolation(self) -> None:
        cases = {
            ("a.py", "x = f'{a}'"): None,
            ("a.py", "x = 'plain'"): "plain",
            ("a.js", "x = `t${a}`"): None,
            ("a.js", "x = `plain`"): "plain",
            ("A.cs", 'class A { string x = $"{a}"; }'): None,
            ("A.cs", 'class A { string x = @"plain"; }'): "plain",
        }
        for (path, source), expected in cases.items():
            with self.subTest(source=source):
                root = parse(source, path)
                assert root is not None
                strings = [
                    node
                    for node in _descendants(root)
                    if node.type
                    in ("string", "template_string", "interpolated_string_expression", "verbatim_string_literal", "string_literal")
                ]
                self.assertTrue(strings)
                self.assertEqual(literal(strings[0]), expected)


def _descendants(node):  # type: ignore[no-untyped-def]
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


if __name__ == "__main__":
    unittest.main()
