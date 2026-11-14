"""Proxy configurations (nginx, Envoy) and environment reads through
names the same file fixes to literals."""

import unittest

from src.core.models import OpaqueReferenceKind
from src.tools.devops_extractor import FileClass, ReferenceKind, classify_path, read_source_file
from src.tools.proxy_configs import envoy_upstreams, nginx_upstreams, proxy_upstreams
from src.tools.reference_sites import FollowedEnvRead, followed_env_reads, opaque_reference_sites

NGINX_TEMPLATE = """
server {
    listen 8080;
    # proxy_pass http://127.0.0.1;
    location /api/catalogue/ {
        proxy_pass http://${CATALOGUE_HOST}:8080/;
    }
    location /api/ratings/ {
        proxy_pass http://${RATINGS_HOST}:80/;
    }
    location /legacy/ {
        proxy_pass http://legacy-api:9000;
    }
    location /pool/ {
        proxy_pass http://backend_pool;
    }
    location /dyn/ {
        proxy_pass http://$target;
    }
    location /grpc/ {
        grpc_pass grpc://stock:50051;
    }
}
upstream backend_pool {
    server orders-1:8080;
    server orders-2:8080 weight=2;
    server unix:/tmp/sock;
}
"""

ENVOY = """
static_resources:
  listeners:
  - address:
      socket_address: {address: 0.0.0.0, port_value: 80}
  clusters:
  - name: catalog
    load_assignment:
      endpoints:
      - lb_endpoints:
        - endpoint:
            address:
              socket_address: {address: catalog-api, port_value: 80}
  - name: agg
    hosts:
    - socket_address:
        address: webshoppingagg
        port_value: 80
"""


class NginxTests(unittest.TestCase):
    def test_proxy_pass_upstream_blocks_and_envsubst_hosts(self) -> None:
        read = nginx_upstreams(NGINX_TEMPLATE)
        self.assertEqual(
            read.urls,
            ["http://orders-1:8080", "http://orders-2:8080", "http://legacy-api:9000", "grpc://stock:50051"],
        )
        self.assertEqual(read.variables, ["CATALOGUE_HOST", "RATINGS_HOST"])
        # proxy_pass to an nginx variable can't be followed.
        self.assertEqual(
            [(site.line, site.kind) for site in read.sites],
            [(18, OpaqueReferenceKind.CLIENT_ADDRESS_EXPRESSION)],
        )

    def test_a_conf_file_without_proxy_directives_is_not_read_as_nginx(self) -> None:
        self.assertIsNone(proxy_upstreams("key = value\n", "/svc/app.conf"))

    def test_an_envsubst_template_is_scanned_and_its_hosts_become_env_reads(self) -> None:
        self.assertIs(classify_path("/web/default.conf.template"), FileClass.SCANNED)
        self.assertIs(classify_path("/web/notes.md.template"), FileClass.NOT_SOURCE)
        read = read_source_file(NGINX_TEMPLATE, "web", "/web/default.conf.template")
        env_reads = [r.endpoint for r in read.references if r.kind is ReferenceKind.ENV_VAR]
        self.assertEqual(env_reads, ["CATALOGUE_HOST", "RATINGS_HOST"])
        urls = sorted(r.dependency_target() or "" for r in read.references if r.kind is ReferenceKind.URL)
        self.assertEqual(urls, ["legacy-api", "orders-1", "orders-2", "stock"])

    def test_a_template_host_variable_not_named_as_a_host_is_not_understood(self) -> None:
        read = read_source_file("location / {\n  proxy_pass http://${BACKEND}/;\n}\n", "web", "/web/nginx.conf")
        self.assertEqual([r.endpoint for r in read.references], [])
        self.assertEqual([(s.line, s.kind) for s in read.sites], [(2, OpaqueReferenceKind.CLIENT_ADDRESS_EXPRESSION)])


class EnvoyTests(unittest.TestCase):
    def test_cluster_socket_addresses(self) -> None:
        self.assertEqual(
            envoy_upstreams(ENVOY).urls,
            ["http://0.0.0.0:80", "http://catalog-api:80", "http://webshoppingagg:80"],
        )

    def test_listener_addresses_are_not_dependencies(self) -> None:
        read = read_source_file(ENVOY, "gw", "/gw/envoy.yaml")
        self.assertEqual(sorted(r.dependency_target() or "" for r in read.references), ["catalog-api", "webshoppingagg"])

    def test_invalid_yaml_yields_nothing(self) -> None:
        self.assertEqual(envoy_upstreams("a: [").urls, [])


class FollowedEnvReadTests(unittest.TestCase):
    def test_a_loop_over_a_literal_list(self) -> None:
        java = """
            private static final String[] EXPECTED = {
                "VERSION", "BALANCES_API_ADDR"
            };
            for (String v : EXPECTED) { String value = System.getenv(v); }
        """
        self.assertEqual(
            followed_env_reads(java, "A.java"),
            [FollowedEnvRead(name="VERSION"), FollowedEnvRead(name="BALANCES_API_ADDR")],
        )
        self.assertEqual(opaque_reference_sites(java, "A.java"), [])
        python = 'NAMES = ["A_HOST", "B_URL"]\nfor n in NAMES:\n    os.environ[n]\n'
        self.assertEqual([r.name for r in followed_env_reads(python, "a.py")], ["A_HOST", "B_URL"])

    def test_a_loop_over_a_computed_list_is_not_followed(self) -> None:
        python = "NAMES = load()\nfor n in NAMES:\n    os.environ[n]\n"
        self.assertEqual(followed_env_reads(python, "a.py"), [])
        self.assertEqual(len(opaque_reference_sites(python, "a.py")), 1)

    def test_a_file_local_wrapper_called_with_literals(self) -> None:
        java = """
            private String getenv(String key, String def) {
                String val = System.getenv(key);
                return val == null ? def : val;
            }
            void build() { helper = new CartHelper(getenv("CART_ENDPOINT", "cart")); }
        """
        self.assertEqual(followed_env_reads(java, "A.java"), [FollowedEnvRead(name="CART_ENDPOINT", default="cart")])
        self.assertEqual(opaque_reference_sites(java, "A.java"), [])
        read = read_source_file(java, "shipping", "/shipping/A.java")
        self.assertIn(("CART_ENDPOINT", ["cart"]), [(b.variable, [d.template for d in b.defaults]) for b in read.bindings])

    def test_a_wrapper_other_files_may_call_is_not_followed(self) -> None:
        java = """
            public String env(String key) { return System.getenv(key); }
            void a() { env("CART_HOST"); }
        """
        self.assertEqual(followed_env_reads(java, "A.java"), [])
        self.assertEqual(len(opaque_reference_sites(java, "A.java")), 1)

    def test_a_wrapper_called_with_a_variable_is_not_followed(self) -> None:
        go = 'func env(key string) string {\n  return os.Getenv(key)\n}\nfunc main() { env(name) }\n'
        self.assertEqual(followed_env_reads(go, "m.go"), [])
        self.assertEqual(len(opaque_reference_sites(go, "m.go")), 1)


if __name__ == "__main__":
    unittest.main()
