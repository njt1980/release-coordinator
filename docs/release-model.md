# Supported applications and release model

Status: draft for review (2026-11-13; G11 onwards and the acceptance gates
added the same day after the external review)

This document states what kind of application the release coordinator
supports, what it needs from a team that onboards one, how it decides
whether a release may go ahead, and what is still missing for that decision
to hold on applications we don't own. Every change should keep the
platform working for any application that fits the shape below, not only
for the lab.

## 1. Supported application shape

An application qualifies when all of these hold:

| Assumption | What the platform relies on |
| --- | --- |
| Microservices on Kubernetes | Each service is a workload (Deployment, StatefulSet, DaemonSet or CronJob) in a known namespace, usually behind a Kubernetes Service. A workload no Service selects (a queue worker, a CronJob) is registered under its own name so the calls it makes count. Other objects are reported as out of shape (see below). |
| Services call each other by name | In-cluster DNS names (`order-service`, `order-service:3000`, `order-service.ns.svc.cluster.local`) or environment variables / ConfigMaps that hold them, as URLs (http, gRPC, AMQP, JDBC, Redis, MongoDB...), connection strings (`Server=...;`, `Host=...;`) or bare host names. |
| Configuration the platform understands | Environment variables read in code (with literal defaults); .NET `appsettings*.json` keys and their `A__B` overrides, `IConfiguration` reads, `GetConnectionString` and .NET Aspire client integrations; Spring `application*.yml`/`.properties`, `${NAME:default}` placeholders, relaxed binding, `@Value`, `@FeignClient`; Dockerfile `ENV`. The cluster's value always wins; a default in source control is used only when nothing at runtime may set the variable. |
| Source in Git | One or more repositories. A service owns a directory: declared in a `release-coord.yaml` at the repository root, or inferred from a `Dockerfile`, `go.mod` or `package.json` (directly or under `apps/`, `cmd/`, `services/`, `src/`). |
| One release target per project | A project releases to exactly one environment and cluster (its policy). Multiple clusters mean multiple projects. |
| Changes arrive as pull requests | The PR names its changed files, its base and head commits, and whether the change is additive or breaking. |

The platform never needs to modify the application's code, images or
manifests.

### Service identity

Every service is identified by a URI:
`urn:release-coord:{tenant}:{environment}:{cluster}:{namespace}:{name}`.
Every source of evidence (code, manifests, telemetry) names services
differently; all of them are resolved to this URI. A reference that does not
match exactly one registered service stays an **unresolved entity**, with a
kind: IP address, private DNS, external DNS, unregistered service,
unresolved environment variable, unset environment variable, or ambiguous
service.

Identity is namespace-safe:

- A service declared in source code takes the namespace given in
  `release-coord.yaml` (`namespace:`). Without one, it merges with the only
  Kubernetes service of that name in the scanned namespaces.
- When that can't be decided, nothing is merged and the service is an
  **ambiguous service**: two repository directories claim the same service,
  or the name exists in several scanned namespaces. Its code dependencies
  stay attached to the ambiguity (a `DISCOVERY_GAP` in any radius it
  reaches) and its files map to no service (`UNMAPPED_FILES`) until the
  namespace is declared.
- A dependency's bare target name (`db`, `db:5432`) resolves in the
  caller's own namespace, as in-cluster DNS does.
- Telemetry names its source by the `service.name` and `k8s.namespace.name`
  resource attributes, so the same workload name in two namespaces stays
  two services.
- A source service whose name matches no running service can still be
  linked by what is declared about it, for identity only (never for an
  edge): docker-compose services and images, skaffold artifacts, `Docker@2`
  pipeline tasks, .NET Aspire AppHost resources and a Dockerfile's
  `org.opencontainers.image.title` label on the build side; workload
  labels (`app.kubernetes.io/name`, `app.kubernetes.io/component`, `app`),
  `OTEL_SERVICE_NAME` and OCI image annotations on the cluster side. A
  link is made only when these facts point at exactly one running service
  that no other source service also points at; otherwise they are offered
  as suggestions. The link records why it was made.

### Coverage and shape

Discovery records what it could not see instead of hiding it in `SUCCESS`:

- **Static coverage.** Every file in a service directory is accounted for:
  scanned, excluded by design (vendored dependencies, build output,
  documentation, lock files, test code), not source (images, Markdown), or
  **unscanned**: source in a language the scanner can't read (Rust, Kotlin,
  C++...), a file that couldn't be read or parsed, or a file whose
  references were read but **not understood** (a dynamic configuration key
  or environment name, an HTTP/gRPC client address that isn't a literal or
  a keyed configuration read, a configuration section bound to an options
  class with no keys in the configuration files, an unresolved project
  reference, configuration imported from a config server, or a network
  client library (HTTP, messaging, database, registry, feature-flag SDK)
  declared by a service whose scan found no address at all), each listed
  by line. Any unscanned file, in any
  service at the baseline or in a changed service at the pull request's
  head, makes the assessment `STATIC_COVERAGE_INCOMPLETE`: an unread call
  into a target would make its caller upstream, so every service counts,
  and live traffic doesn't substitute. A project accepts reviewed files
  with `exemptions.unscanned_files` (path globs, each with a reason).
  Source outside every service directory is listed, not read.
- **Pipeline variables.** Each repository's build pipelines are read at the
  scanned commit: YAML `variables` at every level, definition variables and
  variable groups. Secret, queue-time, macro and script-set variables,
  variable groups and templates are opaque. Pipelines never resolve a
  dependency (the live cluster's environment already reflects deploy-time
  substitution), but a variable a pipeline defines, or may define, could be
  baked into the image, so it is never treated as provably unset.
- **Environment-declared dependencies.** An endpoint-like variable
  (`*_URL`, `*_HOST`, `*_ADDR`, `ConnectionStrings__*`...) that a workload's
  Kubernetes environment sets to a registered service is an edge (provenance
  `kubernetes_env`), even when no code read of it was found: Go helpers
  hiding the name, destructured `process.env`, SDKs that read their own
  variables (`FLAGD_HOST`). It is weaker evidence than a read in code (the
  variable may be unused) and corroborates a code edge to the same service.
  Values naming nothing registered add nothing.
- **Kubernetes shape.** Objects outside the shape are recorded and listed
  in the report when they concern a radius service: an unexposed workload
  (registered), a transient Job or Pod (not registered), pods from a
  controller kind discovery doesn't read such as Argo Rollouts (read from
  the running pods), a Service with no selector or selecting no workload
  (environment unknown), an ExternalName Service (an edge to its host), and
  a workload whose name is taken by a Service that doesn't select it.
  Findings are advisory; their effects (unknown environments, unresolved
  hosts) already block through the gaps they cause.

## 2. What a team provides

| Input | Purpose | Access (see [access-requirements.md](access-requirements.md)) |
| --- | --- | --- |
| Repositories and project | Static code scan: service boundaries and the addresses each service uses | Read on the repositories |
| Cluster and namespaces | Manifest scan: Services, workloads, ConfigMaps, environment variables | `get`/`list` on those kinds; never Secrets |
| Telemetry workspace | Observed calls between services | Read on the workspace |
| Project policy | What evidence is required, what is exempt, what proves the cluster, and where the code lives (`source_control`) | Reviewed file in the repo (`policies/{tenant}/{project}.yaml`) |

The policy's `cluster` is a label. Two fields prove it:

- `kubernetes_api_server`: the API server the manifest scan must have read
  (required when `K8S_MANIFEST` is a required source).
- `telemetry_cluster` and/or `telemetry_components`: an attribute and value
  (by default `k8s.cluster.name`), or the Application Insights components
  (resource IDs), that every telemetry query filters on, at onboarding and
  at assessment. One of them is required when `RUNTIME_TELEMETRY` is
  required or runtime evidence must be live; classic Application Insights
  and Azure Monitor Distro rows carry no cluster attribute, so they are
  scoped by component.

A baseline that read another API server, or telemetry with another scope
or none, is `BASELINE_SCOPE_MISMATCH`.

`regulated_services` lists services (`<namespace>/<name>`, with an optional
classification such as `IEC 62304 class B`) whose changes may need
regulatory review. When a pull request changes one, or adds or removes a
dependency to or from one, the briefing's first decision is "possible
significant change: decide whether regulatory review is needed". The
verdict is unchanged; whether the change is significant is the
manufacturer's decision, recorded with the release decision.

`service_owners` maps services (`<namespace>/<name>`) to who answers for
them; the dependency review's "Next to review" queue names them (a
dependency belongs to its caller). The queue lists every unanswered item,
weakest evidence first: answers needing re-review, open questions, weak
evidence, a single source, then corroborated dependencies; ties go to the
more critical service.

`source_control` (organization URL and project) is where pull request
revisions are verified. It comes only from the policy, never from the
caller; without it every assessment is `REVISION_UNVERIFIED`.

Telemetry comes from either:

- **instrumentation the team already has** (OpenTelemetry SDK or Application
  Insights), read as is; or
- **our telemetry agent bundle**: an eBPF agent (Beyla) plus an
  OpenTelemetry Collector, installed in the cluster with no code changes.
  The lab instance is [deploy/lab/telemetry](../deploy/lab/telemetry/README.md).

## 3. How a release is decided

### Onboarding builds a baseline

Discovery runs the three scans and merges them into a **baseline
snapshot**: the services, the dependency edges (each labeled with the
methods that found it) and the unresolved entities. The snapshot records
what was scanned: commits, namespaces, environment, cluster and telemetry
workspace. It also records each service's container environment so a pull
request's head can be resolved the same way, storing only what
resolution needs: values of endpoint-like variables (`*_URL`, `*_HOST`...)
with credentials, query strings and secret-bearing connection strings
removed. Every other variable is recorded by name only.

### A pull request is assessed against the baseline

1. **Policy**: load the project policy. A project without a valid policy is
   never approved.
2. **Mapping**: map changed files to services by their directories.
3. **Revisions**: verify the pull request with source control. The base
   and head (full commit IDs) must exist, the base must be an ancestor of
   the head, and the changed files must be exactly what the commits change
   (`REVISION_UNVERIFIED` otherwise). The baseline's scanned commit must
   relate to the base: if the base is ahead, commits in between must not
   change any service in the radius; if the baseline is ahead and the head
   doesn't contain it, they must not change the services being changed
   (`BASELINE_REVISION_MISMATCH` otherwise). The changed services are then
   scanned **at the head**, and their code dependencies replace the
   baseline's, so a dependency the pull request adds is in the radius and
   one it removes is not. The report lists both.
4. **Radius**: walk that graph from the changed services to every
   upstream consumer and downstream dependency.
5. **Live check**: query telemetry for the same workspace, cluster filter
   and lookback (`runtime_lookback_days`). Noise is dropped in the backend
   and counted: loopback calls, targets with no host (an HTTP method or a
   span name), and messaging deliveries reported backwards (a messaging
   dependency type with a target port of 32768 and up). A port alone never
   proves direction, since servers listen high too (gRPC on 50051): any
   other edge to such a port is kept and marked *direction uncertain*. An
   uncertain edge whose reverse the baseline already has is a reply and
   not drift; any other is reported as drift with that qualification.
   Discovery treats them the same way. The result is **live** evidence
   only when the query returned rows and **each** radius service was seen
   within `max_telemetry_age_minutes`, judged per service so one busy
   service can't make a quiet one look fresh. A service that is quiet by
   nature can be listed in `exemptions.silent_services` with a reason, an
   `expires_on` date (expired entries are ignored and reported) and
   optionally `max_quiet_days` (it must still be seen within that many
   days). Otherwise it is
   `live_empty`, `live_stale` or `live_uncovered`, and a policy requiring
   live evidence blocks with the reason. A service that is unseen and whose
   every dependency uses a protocol the telemetry can't parse is
   **unobservable**, not quiet (`live_unobservable`, `RUNTIME_UNOBSERVABLE`).
   The lab's Beyla sees HTTP/1.1, HTTP/2, gRPC, Redis, PostgreSQL, MySQL,
   SQL Server, MongoDB, Kafka, NATS and MQTT. It does not see AMQP 0-9-1
   (RabbitMQ.Client, Spring AMQP, MassTransit) or AMQP over TLS (Azure
   Service Bus), and `amqp://` doesn't say which AMQP version a client
   speaks. A silent-service allowance never applies to an unobservable
   service; `exemptions.unobservable` accepts one protocol at a time, with a
   reason and `expires_on`. An observed edge in the radius
   that the baseline lacks means the baseline no longer describes the
   system (drift).
6. **Sequence**: order the deployment deterministically. For an additive
   change, providers deploy before consumers. For a breaking change,
   consumers deploy first, and consumers the PR doesn't change are reported
   as stale.
6. **Risk**: a model writes an advisory assessment. It can reject a release
   (`MODEL_REJECTED`) but can never approve one the deterministic checks
   block.
7. **Verdict**: `approved` only when no blocking reason applies.

### Several pull requests as one release (release train)

Single-PR assessment is the default. When several pull requests (possibly
in several repositories of one project) ship together, their completed
assessments can be combined (`POST /api/v1/release-trains` with their job
IDs, or "Release train" on the Assessments page). Nothing is re-assessed:

- **Radius**: the union of the members' subgraphs; a service any member
  changes is a target.
- **Order**: one deployment order over that union from the same sort as a
  single PR, using the dependency edges each member's order honoured. An
  edge into a service a BREAKING member changes deploys its consumer first;
  every other edge deploys its provider first. Conflicts show as a cycle.
- **Verdict**: worst member wins. The train is blocked as well when its
  members were assessed against different baselines (`BASELINE_MISMATCH`),
  when a member predates kept ordering facts (`ORDER_FACTS_MISSING`), or
  when the combined order has a cycle (`TRAIN_CYCLE`).
- Breaking-change consumers that another member changes are listed
  separately from those no member changes.

Trains are kept in memory; recreate one from the same assessments after a
restart. A decision is still recorded per pull request.

### Where to look when a service fails (advisory)

Selecting a service on the Inventory page shows "If it fails: where to look
first" (`GET /api/v1/projects/{tenant}/{project}/rca?service=...`). It walks
what the service depends on (up to 4 hops) in the newest baseline with the
dependency review applied, and lists, for the service and each dependency,
the changes recorded across the last baselines (dependency added or
removed, image or repository commit changed, configuration keys by name,
contract files added or removed) and completed assessments that changed it.
Nearer first; within a hop, recently changed first, then stronger
evidence. It reads no logs, metrics or incidents and diagnoses nothing; it
is a starting list for a person.

### Assessing pull requests automatically (opt-in)

With `ado_connection.poll: true` in a project's policy, the server lists
the active pull requests of the named repositories every
`interval_minutes` and assesses each new iteration through the same PR
intake as "Assess PR #N". Two further switches, off by default, post the
result back: an advisory pull request status (genre `release-coordinator`)
and the briefing as a closed comment thread. The platform sets no branch
policy, so the status blocks nothing unless the team adds one. Access is
described in `docs/access-requirements.md`.

### Every run is logged

Each onboarding and assessment writes a live, plain-language log: one line
per step saying what was read or checked and what it found (passed,
warning, blocked or failed), with details such as IDs and errors. The app
shows it as the crawl runs and on the Runs page
(`GET /api/runs/{run_id}/events?after=N`). Logs live in memory; the log of
the onboarding that built a baseline is kept beside the snapshot
(`GET /api/baselines/{tenant}/{project}/{snapshot}/run-log`). Logs never
contain credentials.

### Asking the platform

An **Ask** panel on every page answers questions about the platform's own
evidence (baselines, services and their dependencies, policies, runs and
their logs, assessments and reason codes). Claude (`claude-opus-5-5`,
overridable with `ASSISTANT_MODEL`) answers by calling read-only tools; no
tool approves, exempts, edits a policy or starts a run. It can draft a
change, such as a policy entry, for review in a pull request. Questions
send platform metadata (names, paths, policy contents, run logs) to
Anthropic's API, never source code or credentials; a written data-egress
policy (G19) is required before anyone else's data goes through it.

### Blocking reasons

| Code | Meaning |
| --- | --- |
| `POLICY_MISSING`, `POLICY_INVALID` | No usable policy for the project |
| `BASELINE_UNAVAILABLE`, `BASELINE_STALE`, `BASELINE_SCOPE_MISMATCH` | No baseline, too old, or scanned for another environment/cluster (or not provably the policy's cluster) |
| `REQUIRED_SOURCE_MISSING` | A discovery source the policy requires did not succeed, or runtime evidence is weaker than required (e.g. live telemetry empty, stale or not covering the radius) |
| `UNMAPPED_FILES` | Changed files no service owns and the policy doesn't exempt |
| `DISCOVERY_GAP` | An unresolved entity in the release radius that the policy doesn't exempt or allow as egress (an unset environment variable included: only a reviewed `exemptions.dependencies` entry accepts it) |
| `BASELINE_DRIFT` | Live telemetry shows an edge in the radius that the baseline lacks |
| `REVISION_UNVERIFIED` | The PR's commits can't be verified: missing, base not an ancestor of head, changed files that don't match the commits, no `source_control` in the policy, or the head couldn't be scanned |
| `BASELINE_REVISION_MISMATCH` | Commits between the baseline's scanned revision and the PR's base change the release, the two have diverged, or the baseline predates recorded container environments |
| `RUNTIME_UNOBSERVABLE` | Live evidence is required, but a radius service talks only protocols the telemetry can't see, so not seeing it proves nothing; accepted per protocol only through `exemptions.unobservable` |
| `IDENTITY_UNLINKED` | A service in the radius exists in source code but matches no running service by name (or by name up to case and `-_.` separators, unique on both sides); near names are offered as a question, never applied |
| `DEPENDENCY_CYCLE` | No safe deployment order exists |
| `COMPATIBILITY_UNVERIFIED` | No contract evidence proves the change compatible. `.proto` files, OpenAPI documents and message payload types are read; under the default `contract_scope: strict` only a PR changing nothing but contract files (`.proto`, OpenAPI) can be verified, under `interfaces` code changes count when every caller reaches each changed service through a contract both builds declare (`<Protobuf>`, or an `<OpenApiReference>` client of the document the service serves) |
| `CONTRACT_BREAKING` | A changed contract breaks compatibility: a `.proto` (field removed, renamed or retyped, RPC removed or changed...), an OpenAPI document (operation removed or renamed, new required parameter or request field, request body made required, success response or response field removed, type changed, request enum narrowed) or a message channel's payload type (type or property removed, property retyped). Fully evidenced, so the assessment stays actionable: a No-go a person may overrule in the decision record |
| `DEMO_EVIDENCE`, `MODEL_REJECTED` | Mock data, or the risk model rejected the release |
| `DB_MIGRATION`, `CONFIG_KEYS_CHANGED`, `FEATURE_FLAGS_CHANGED` | Advisory change flags (actionable; no automatic approval, a person decides). The PR adds or changes a database migration (EF Core `Migrations/*.cs`, Flyway `db/migration/V*__*`, Liquibase changelogs), adds/removes/changes keys in `appsettings*.json`, `application*`/`bootstrap*` `.yml`/`.properties` or `.env*` files, or changes a feature-flag section (`FeatureManagement`; `features`/`togglz`/`ff4j`; `FEATURE_*`/`FF_*`). Config files are read at base and head through `source_control` and compared by key path; only key names are kept, never values. Without source control the keys come from the diff lines and the flag says so |
| `CHANNEL_CONSUMERS_UNKNOWN` | A changed service publishes to a message channel (queue, topic, exchange, or an integration event / message type) that no scanned service consumes, so its readers are missing from the radius. Blocks by default; `messaging.external_consumers` accepts a channel with a reason, and `messaging.unknown_consumers: advisory` turns it into `CHANNEL_CONSUMERS_UNCONFIRMED` (actionable; no automatic approval, a person decides). Consumers of a changed producer's channels are in the radius as its dependents; channel links never order deployments |

### A person decides, and the decision is recorded

The assessment advises; a person decides. On a completed assessment the
coordinator records **Approve**, **Decline** or **Further analysis**
(`POST /api/v1/webhooks/pr-assessment/{job_id}/decisions`). Declining,
asking for further analysis, and approving what the assessment did not
approve all need a reason.

Each record is shaped for 21 CFR Part 11 (shaped for, not compliant: only
the customer's validation for its intended use can establish that):

- **Signature manifestation (11.50):** the signer's printed name and ID,
  the UTC time and the meaning ("Approved for release", "Declined for
  release", "Further analysis requested before release") are part of the
  record and of every view of it.
- **Signer:** behind the platform's sign-in the signer is the signed-in
  Entra user (display name, user principal name, object ID), never a typed
  name; `RC_DECISION_ROLE` can require an app role. Locally the typed name
  is kept and labelled "not an electronic signature".
- **Signature/record link (11.70):** the record carries the SHA-256 of the
  exact assessment shown, the PR, head commit, baseline snapshot, policy
  version and policy hash, and the tool's own verdict at signing time.
  Listing a decision says whether the stored assessment is still the one
  signed.
- **Audit trail (11.10(e)):** records are only appended, into one SHA-256
  hash chain; the database refuses UPDATE and DELETE, and a correction is a
  new record naming the one it corrects. `GET /api/v1/decisions/verify`
  recomputes every hash and link, so a change made outside the application
  is detected.

The assessments page shows every decision with its full signature line and
asks the signer to confirm the meaning, name and commit before recording.
Dependency review answers get the same protection: append-only, one hash
chain, verified on every read (an older review database is migrated on
first use, its answers chained in the order they were given).

## 4. Gaps before this works on applications we don't own

Found while running the lab end to end (2026-11-13). Each gap is phrased as
a platform capability, not a lab fix.

| # | Gap | Why it matters | Proposed capability |
| --- | --- | --- | --- |
| G1 | Telemetry noise is only filtered in our collector (loopback calls, reversed broker deliveries, failed connects, path-only URLs, exporter quirks) | Teams that bring their own telemetry won't run our collector, so the noise reaches the backend and blocks releases | Normalize in the backend's telemetry adapter as well; the collector is an optimization. **Done (PR 2a-2).** |
| G2 | Every unresolved external call blocks (e.g. npm's update check at container start) | Real applications call package registries, cloud APIs and SaaS; exempting entity by entity is fragile (IPs change) | **Egress policy**: allow external destinations by host pattern, CIDR or category, each with a reason. **Done (PR 2a-2)** for `host`, `host_suffix` and `cidr`; categories later. |
| G3 | Fixed telemetry lookback (30 days at onboarding, 7 at assessment) | Bad or removed edges linger for a month | `runtime_lookback_days` in the project policy. **Done (PR 2a-2).** |
| G4 | Telemetry only from Azure Monitor `AppDependencies` | Teams use Datadog, Grafana Tempo, Prometheus and others | Telemetry adapter interface; Azure Monitor first |
| G5 | Code only from Azure DevOps | Many teams use GitHub or GitLab | Code-host adapter interface |
| G6 | A service's evidence isn't reconciled across sources (an environment variable the code scan can't resolve vs the endpoint telemetry observes; differing service names) | Gaps that other evidence already answers still block | Record each edge's sources, and resolve environment-variable references with observed endpoints. **Partly done (PR 2a-2, revised in 2a-3):** a variable that Kubernetes provably doesn't set is classified `ENVIRONMENT_VARIABLE_UNSET` but stays a gap: live traffic doesn't prove the code path reading it ran, and defaults set in the code or the container image aren't read. Only a reviewed policy exemption accepts it. **PR 2b-3a:** when Kubernetes, the pipelines and the image's Dockerfile `ENV` provably don't set a variable, its default in source control (the active profile's configuration file, then the base file, then the default in code) resolves it, and the snapshot lists each such use (`static_defaults_used`). Defaults that differ by profile while the profile variable is opaque, or that point at the container itself (`localhost`), don't resolve. Matching defaults with observed edges is still open. Secret-backed or imported variables are never treated as unset, and a name any container sets opaquely, or that a later `envFrom` Secret may override, is never treated as resolved. Each resolved value records the workload and container it came from. |
| G7 | Messaging channels (queues, topics, exchanges, message types) are typed nodes with producers and consumers, read from .NET (event bus, MassTransit, Azure Service Bus, RabbitMQ.Client) and Java (Spring Rabbit/Kafka, Spring Cloud Stream) code; Service Bus entities can be listed read-only from Azure | Go, Node, Python and Rust producers and consumers, Confluent/plain Kafka clients, Dapr and NServiceBus aren't read: their consumers are missing from the radius and nothing flags it | Readers for further languages and frameworks |
| G8 | Contract analysis covers `.proto`, OpenAPI (local `$ref` only; `oneOf`/`anyOf` and external refs opaque) and C#/Java payload types of type-routed channels (`coordinator/contracts.py`, `core/proto_contract.py`, `core/openapi_contract.py`, `core/message_contract.py`) | HTTP APIs without an OpenAPI document in the repository, queue/topic payloads, JSON Schema and AsyncAPI stay unverified; OpenAPI client links are recorded for .NET `<OpenApiReference>` only | Add JSON Schema / AsyncAPI differs; record non-.NET client links (Go, Java, Node generated stubs) |
| G9 | The agent bundle is a lab directory with fixed names | Every cluster differs | A versioned, parameterized bundle (namespaces, connection string) with documented known issues |
| G10 | The backend runs as a single local process with no authentication | Shared and customer use | The security track: authentication, tenant isolation, webhook signatures, managed identity, and an allowlist of code-host organizations a tenant may name |

Added after the external review of 2026-11-13, which found the platform
fit for an engineer-reviewed pilot, not for gating releases:

| # | Gap | Why it matters | Proposed capability | Status |
| --- | --- | --- | --- | --- |
| G11 | The cluster was a caller-supplied label; the scan used whatever kubeconfig was current, and telemetry wasn't scoped by cluster | A baseline could describe another cluster than the one it is labeled with | The policy names the API server and a telemetry cluster filter; the snapshot records both and they must match | Done (PR 2a-1) |
| G12 | Same-named services merged: code services had an unknown namespace, and the lab collector dropped the telemetry namespace | Two services could share one node, hiding a dependency | Declared namespaces, ambiguous services, targets resolved in the caller's namespace, namespaced telemetry sources | Done (PR 2a-1) |
| G13 | A live query with zero rows counted as live evidence; the reported runtime edges came from the baseline; real metrics were empty | "Live" didn't prove the radius was observed | Live evidence states (empty, stale, uncovered), live edges in the report, metrics from the query | Done (PR 2a-2) |
| G14 | One PR-wide additive/breaking flag | A PR changes several interfaces, each with its own compatibility | Per-interface change classification, from contract analysis (G8) | After G8 |
| G15 | The PR's base and head commits aren't verified, and the head isn't scanned for new dependencies; no evidence of what is deployed | The assessment may describe code other than what will ship | Verify base ancestry and scan the head; record deployed versions | **Partly done (PR 2b-1):** revisions verified against the policy's `source_control`, baseline-to-base lag checked, changed services scanned at the head. **T1c:** discovery records what the cluster runs per service (`ServiceNode.deployed_workloads`: images, and the pipeline run from the KubernetesManifest task's `azure-pipelines/*` annotations); comparing it with the assessed revision is still open. |
| G16 | The static scanner is heuristic and `SUCCESS` doesn't mean full coverage; pipeline variables are discarded | A missed reference looks like a missing dependency | Coverage accounting per service directory; read pipeline variables | **Done (PR 2b-2)** for coverage accounting (`STATIC_COVERAGE_INCOMPLETE`, `exemptions.unscanned_files`) and pipeline variables (read at the scanned commit; they stop false "unset" claims). The scanner itself is still heuristic. **PR 2b-3a** adds framework configuration (.NET, Spring), code defaults, Dockerfile `ENV`, connection strings and non-HTTP schemes; a configuration file that can't be parsed is unscanned (`PARSE_FAILED`). **PR 2b-3b:** `.csproj`/`pom.xml`/`build.gradle` projects at any depth are services when runnable (or containerized); test and Aspire AppHost projects are not, and `obj/`, `.gradle/` and a .NET project's `bin/` are build output. A shared library's files count for every service consuming it (`<ProjectReference>`, Maven sibling modules, Gradle `project(':x')`, transitively), and a pull request changing one targets those consumers. References read but not understood are `REFERENCES_NOT_UNDERSTOOD` (blocking, exemptible like other unscanned files). Still heuristic: constants, options classes and helper wrappers are reported, not followed; package.json workspaces and Go `replace` modules aren't libraries yet. |
| G17 | Jobs are in memory, not safe across replicas, and have no deadlines | Restarts lose assessments; runs can hang | Durable job store with deadlines | Planned |
| G18 | Nothing enforces the verdict; an assessment never expires or yields to a newer head | A stale approval could be acted on | Required PR/CD check, superseded on a new head, expiry | Planned |
| G19 | No policy on what is sent to the risk model and the Ask assistant | Customer code and topology leave the boundary | Model data-egress policy: what is sent, redaction, opt-out | Planned |
| G20 | Packaging and operations; dependencies unpinned | Reproducible, supportable installs | Pinned dependencies, container image, runbooks | Planned |
| G21 | Kubernetes coverage is Services, Deployments, StatefulSets and bare Pods | DaemonSets, Jobs/CronJobs, Ingress and ExternalName services carry dependencies too | Read the remaining workload and routing kinds | **Done (PR 2b-2)** except Ingress: DaemonSets, CronJobs, owner-less Jobs and ReplicaSets, pods of unread controllers, unexposed workloads, ExternalName edges, and out-of-shape findings. Ingress (an entry point, not a dependency) is still open. |

## 5. Acceptance gates

The coordinator's verdict may gate a real release only when every one of
these holds. Until then it is advisory and reviewed by an engineer.

1. **Scope is proven** (G11): the baseline read the policy's API server,
   and all telemetry is filtered to the policy's cluster.
2. **Identity is unambiguous** (G12): no ambiguous service and no unknown
   namespace in the release radius.
3. **Live evidence is real** (G13): the live query completed, is fresh,
   and observed every service in the radius, or each silent service is
   explicitly allowlisted with a reason.
4. **The assessed code is the code that ships** (G15, G18): base and head
   are verified, the head is scanned, and a newer head supersedes the
   assessment, which also expires.
5. **Static coverage is accounted for** (G16): every service directory was
   scanned, and gaps are reported rather than hidden in `SUCCESS`
   (`STATIC_COVERAGE_INCOMPLETE` unless a reviewed exemption covers them).
6. **Compatibility is evidenced** (G8, G14) for every changed interface.
7. **Access is controlled** (G10): authentication, tenant isolation, signed
   webhooks, and allowlisted organizations.
8. **Runs are durable and enforced** (G17, G18): jobs survive restarts and
   have deadlines, and the verdict is a required check.
9. **Data handling is defined** (G19, G20): model egress policy, pinned
   dependencies.
10. **Proven beyond the lab**: on a cluster we don't control, a
    deliberately unsafe change is blocked for the right reason and a safe
    one is not blocked by noise.

## 6. Proposed order

1. **Evidence you can trust:** PR 2a-1 (G11, G12), then PR 2a-2 (G13, G1,
   G2, G3, G6).
2. **PR 2b:** static coverage accounting and the PR head scan (G15, G16).
3. **PR 2c:** one service instrumented with the OpenTelemetry SDK, so the
   "team already has telemetry" path is tested next to eBPF.
4. **Security track (G10):** before any shared deployment.
5. **Contract analysis and messaging (G8, G14, G7):** the main remaining
   blocker to approving a real release.
6. **Durable jobs and gate integration (G17, G18), then G19 to G21.**
7. **Adapters (G4, G5) and the agent bundle (G9):** as onboarding needs them.

## 7. Out of scope for now

- Applications not on Kubernetes (VMs, serverless).
- Executing the deployment. The platform decides and orders; the team's CD
  system deploys.
- Database schema migrations as a dependency type.
