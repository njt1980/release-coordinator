"""Find network client libraries a service declares in its dependency manifests.

Pure functions the repository scanner calls for services whose scan found
no outbound address at all. Such a service declaring an HTTP, gRPC,
messaging, database, service-registry or feature-flag client library calls
something the scanner can't see (a registry, a helper hiding the variable
name, an SDK reading the environment itself), so its coverage must not look
complete (``CLIENT_WITHOUT_ADDRESS``).

Manifests read: ``package.json`` (``dependencies`` only), ``go.mod``,
``requirements*.txt``, ``pyproject.toml``, ``*.csproj``, ``pom.xml`` and
``build.gradle(.kts)``. Only names and line numbers are recorded.
"""

import json
import os
import re
from enum import Enum
from typing import Final

from pydantic import BaseModel, Field


class Ecosystem(str, Enum):
    NPM = "npm"
    GO = "go"
    PYTHON = "python"
    NUGET = "nuget"
    MAVEN = "maven"


class ClientLibrary(BaseModel, frozen=True):
    """A client library one manifest declares, at the line declaring it."""

    name: str = Field(min_length=1)
    line: int = Field(ge=1)


# Names are matched case-insensitively; an entry ending in "/" or "." is a
# prefix (a package family), any other entry must match exactly. Libraries
# that serve as much as they call (gRPC core: grpc-go, grpc-java, grpcio,
# @grpc/grpc-js) are left out: a pure gRPC server declares them too. .NET's
# client-only Grpc.Net.Client(Factory) stays.
_CLIENTS: Final[dict[Ecosystem, tuple[str, ...]]] = {
    Ecosystem.NPM: (
        "axios", "node-fetch", "got", "superagent", "request", "request-promise", "undici",
        "amqplib", "amqp-connection-manager", "rhea",
        "rabbitmq-client", "kafkajs", "@confluentinc/kafka-javascript", "node-rdkafka",
        "mongodb", "mongoose", "pg", "mysql", "mysql2", "redis", "ioredis", "connect-redis",
        "@openfeature/flagd-provider", "@azure/service-bus", "@dapr/dapr", "http-proxy",
        "http-proxy-middleware", "express-http-proxy", "nats", "mqtt",
    ),
    Ecosystem.GO: (
        "github.com/rabbitmq/amqp091-go", "github.com/streadway/amqp",
        "github.com/azure/go-amqp", "github.com/segmentio/kafka-go", "github.com/ibm/sarama",
        "github.com/shopify/sarama", "github.com/confluentinc/confluent-kafka-go/",
        "github.com/redis/go-redis/", "github.com/go-redis/redis/", "go.mongodb.org/mongo-driver",
        "github.com/lib/pq", "github.com/jackc/pgx/", "github.com/go-sql-driver/mysql",
        "github.com/nats-io/nats.go", "github.com/open-feature/go-sdk-contrib/providers/flagd",
        "github.com/azure/azure-sdk-for-go/sdk/messaging/", "github.com/dapr/go-sdk",
        "github.com/go-resty/resty/",
    ),
    Ecosystem.PYTHON: (
        "requests", "httpx", "aiohttp", "pika", "aio-pika", "kombu", "celery",
        "kafka-python", "confluent-kafka", "aiokafka", "redis", "pymongo", "motor", "psycopg2",
        "psycopg2-binary", "psycopg", "asyncpg", "pymysql", "mysqlclient", "sqlalchemy",
        "openfeature-provider-flagd", "azure-servicebus", "dapr", "nats-py", "paho-mqtt", "locust",
    ),
    Ecosystem.NUGET: (
        "grpc.net.client", "grpc.net.clientfactory", "microsoft.extensions.http", "refit",
        "restsharp", "rabbitmq.client", "masstransit", "nservicebus", "confluent.kafka",
        "azure.messaging.servicebus", "azure.messaging.eventhubs", "npgsql", "microsoft.data.sqlclient",
        "system.data.sqlclient", "stackexchange.redis", "mongodb.driver", "aspire.", "dapr.client",
        "dapr.aspnetcore", "yarp.reverseproxy", "openfeature.contrib.providers.flagd",
        "microsoft.entityframeworkcore.sqlserver", "npgsql.entityframeworkcore.postgresql",
        "pomelo.entityframeworkcore.mysql",
    ),
    Ecosystem.MAVEN: (
        "spring-boot-starter-webflux", "spring-cloud-starter-openfeign",
        "spring-cloud-starter-netflix-eureka-client", "spring-cloud-starter-config",
        "spring-cloud-starter-gateway", "spring-cloud-starter-gateway-server-webflux",
        "spring-boot-starter-amqp", "spring-rabbit", "spring-kafka", "kafka-clients",
        "spring-boot-starter-data-mongodb", "spring-boot-starter-data-redis",
        "spring-boot-starter-data-jpa", "spring-boot-starter-jdbc", "mysql-connector-java",
        "mysql-connector-j", "mariadb-java-client", "postgresql", "mongodb-driver-sync",
        "mongodb-driver", "jedis", "lettuce-core", "okhttp", "httpclient", "httpclient5", "jersey-client", "jersey-grizzly-connector",
        "jersey-apache-connector", "resteasy-client", "ribbon-loadbalancer", "ribbon-httpclient",
        "eureka-client", "spring-cloud-starter-loadbalancer", "consul-api",
        "cxf-rt-rs-client", "feign-core", "amqp-client", "flagd", "azure-messaging-servicebus",
    ),
}

_REQUIREMENTS_FILE: Final[re.Pattern[str]] = re.compile(r"^requirements[\w.-]*\.txt$", re.IGNORECASE)
_GO_REQUIRE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:require\s+)?(?P<name>[a-z0-9.\-]+\.[a-z]{2,}/\S+)\s+v\d")
_PYTHON_REQUIREMENT: Final[re.Pattern[str]] = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)")
_PYPROJECT_DEPENDENCY: Final[re.Pattern[str]] = re.compile(r"""["'](?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*[\[<>=!~;"']""")
_PACKAGE_REFERENCE: Final[re.Pattern[str]] = re.compile(r"""<PackageReference\s+Include\s*=\s*["'](?P<name>[^"']+)""", re.IGNORECASE)
_MAVEN_ARTIFACT: Final[re.Pattern[str]] = re.compile(r"<artifactId>\s*(?P<name>[^<\s]+)\s*</artifactId>")
_GRADLE_DEPENDENCY: Final[re.Pattern[str]] = re.compile(r"""["'][\w.\-]+:(?P<name>[\w.\-]+)(?::[^"']*)?["']""")


def manifest_ecosystem(path: str) -> Ecosystem | None:
    """The ecosystem whose dependency manifest ``path`` is, or None."""
    filename = os.path.basename(path)
    lowered = filename.lower()
    if lowered == "package.json":
        return Ecosystem.NPM
    if lowered == "go.mod":
        return Ecosystem.GO
    if _REQUIREMENTS_FILE.match(filename) or lowered == "pyproject.toml":
        return Ecosystem.PYTHON
    if lowered.endswith(".csproj"):
        return Ecosystem.NUGET
    if lowered in ("pom.xml", "build.gradle", "build.gradle.kts"):
        return Ecosystem.MAVEN
    return None


def is_client_library(ecosystem: Ecosystem, name: str) -> bool:
    lowered = name.lower()
    return any(
        lowered.startswith(entry) if entry.endswith(("/", ".")) else lowered == entry
        for entry in _CLIENTS[ecosystem]
    )


def _npm_names(content: str) -> set[str]:
    try:
        document = json.loads(content)
    except ValueError:
        return set()
    dependencies = document.get("dependencies") if isinstance(document, dict) else None
    return set(dependencies) if isinstance(dependencies, dict) else set()


def _line_patterns(path: str, ecosystem: Ecosystem) -> tuple[re.Pattern[str], ...]:
    if ecosystem is Ecosystem.GO:
        return (_GO_REQUIRE,)
    if ecosystem is Ecosystem.PYTHON:
        is_pyproject = os.path.basename(path).lower() == "pyproject.toml"
        return (_PYPROJECT_DEPENDENCY,) if is_pyproject else (_PYTHON_REQUIREMENT,)
    if ecosystem is Ecosystem.NUGET:
        return (_PACKAGE_REFERENCE,)
    if path.lower().endswith("pom.xml"):
        return (_MAVEN_ARTIFACT,)
    return (_GRADLE_DEPENDENCY,)


def client_libraries(content: str, path: str) -> list[ClientLibrary]:
    """Client libraries ``path`` declares, in line order, each once."""
    ecosystem = manifest_ecosystem(path)
    if ecosystem is None:
        return []
    npm_names = _npm_names(content) if ecosystem is Ecosystem.NPM else set()
    found: dict[str, ClientLibrary] = {}
    for number, line in enumerate(content.splitlines(), start=1):
        if ecosystem is Ecosystem.NPM:
            names = [n for n in re.findall(r'"([^"]+)"\s*:', line) if n in npm_names]
        else:
            if line.lstrip().startswith(("#", "//", "<!--")):
                continue
            names = [m.group("name") for p in _line_patterns(path, ecosystem) for m in p.finditer(line)]
        for name in names:
            if is_client_library(ecosystem, name) and name.lower() not in found:
                found[name.lower()] = ClientLibrary(name=name, line=number)
    return list(found.values())
