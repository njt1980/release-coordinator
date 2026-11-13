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
| Microservices on Kubernetes | Each service is a workload (Deployment or StatefulSet) with a Kubernetes Service in a known namespace. |
| Services call each other by name | In-cluster DNS names (`order-service`, `order-service:3000`, `order-service.ns.svc.cluster.local`) or environment variables / ConfigMaps that hold them. |
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

## 2. What a team provides

| Input | Purpose | Access (see [access-requirements.md](access-requirements.md)) |
| --- | --- | --- |
| Repositories and project | Static code scan: service boundaries and the addresses each service uses | Read on the repositories |
| Cluster and namespaces | Manifest scan: Services, workloads, ConfigMaps, environment variables | `get`/`list` on those kinds; never Secrets |
| Telemetry workspace | Observed calls between services | Read on the workspace |
| Project policy | What evidence is required, what is exempt, and what proves the cluster | Reviewed file in the repo (`policies/{tenant}/{project}.yaml`) |

The policy's `cluster` is a label. Two fields prove it:

- `kubernetes_api_server`: the API server the manifest scan must have read
  (required when `K8S_MANIFEST` is a required source).
- `telemetry_cluster`: an attribute and value (by default
  `k8s.cluster.name`) that every telemetry query filters on, at onboarding
  and at assessment (required when `RUNTIME_TELEMETRY` is required or
  runtime evidence must be live).

A baseline that read another API server, or telemetry with another filter
or none, is `BASELINE_SCOPE_MISMATCH`.

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
workspace.

### A pull request is assessed against the baseline

1. **Policy**: load the project policy. A project without a valid policy is
   never approved.
2. **Mapping**: map changed files to services by their directories.
3. **Radius**: walk the baseline from the changed services to every
   upstream consumer and downstream dependency.
4. **Live check**: query telemetry for the same workspace, cluster filter
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
   live evidence blocks with the reason. An observed edge in the radius
   that the baseline lacks means the baseline no longer describes the
   system (drift).
5. **Sequence**: order the deployment deterministically. For an additive
   change, providers deploy before consumers. For a breaking change,
   consumers deploy first, and consumers the PR doesn't change are reported
   as stale.
6. **Risk**: a model writes an advisory assessment. It can reject a release
   (`MODEL_REJECTED`) but can never approve one the deterministic checks
   block.
7. **Verdict**: `approved` only when no blocking reason applies.

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
| `DEPENDENCY_CYCLE` | No safe deployment order exists |
| `COMPATIBILITY_UNVERIFIED` | No evidence that the contract change is compatible |
| `DEMO_EVIDENCE`, `MODEL_REJECTED` | Mock data, or the risk model rejected the release |

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
| G6 | A service's evidence isn't reconciled across sources (an environment variable the code scan can't resolve vs the endpoint telemetry observes; differing service names) | Gaps that other evidence already answers still block | Record each edge's sources, and resolve environment-variable references with observed endpoints. **Partly done (PR 2a-2, revised in 2a-3):** a variable that Kubernetes provably doesn't set is classified `ENVIRONMENT_VARIABLE_UNSET` but stays a gap: live traffic doesn't prove the code path reading it ran, and defaults set in the code or the container image aren't read. Only a reviewed policy exemption accepts it; capturing code defaults and matching them with observed edges is planned for 2b. Secret-backed or imported variables are never treated as unset, and a name any container sets opaquely, or that a later `envFrom` Secret may override, is never treated as resolved. Each resolved value records the workload and container it came from. |
| G7 | Messaging is modeled as service → broker | The release risk is producer → queue/topic → consumer | Queues and topics as dependency nodes |
| G8 | No contract analysis | Nothing can be approved (`COMPATIBILITY_UNVERIFIED`) | Compare API and message schemas (OpenAPI, protobuf, JSON Schema) between base and head |
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
| G15 | The PR's base and head commits aren't verified, and the head isn't scanned for new dependencies; no evidence of what is deployed | The assessment may describe code other than what will ship | Verify base ancestry and scan the head; record deployed versions | PR 2b |
| G16 | The static scanner is heuristic and `SUCCESS` doesn't mean full coverage; pipeline variables are discarded | A missed reference looks like a missing dependency | Coverage accounting per service directory; read pipeline variables | PR 2b |
| G17 | Jobs are in memory, not safe across replicas, and have no deadlines | Restarts lose assessments; runs can hang | Durable job store with deadlines | Planned |
| G18 | Nothing enforces the verdict; an assessment never expires or yields to a newer head | A stale approval could be acted on | Required PR/CD check, superseded on a new head, expiry | Planned |
| G19 | No policy on what is sent to the risk model and the Ask assistant | Customer code and topology leave the boundary | Model data-egress policy: what is sent, redaction, opt-out | Planned |
| G20 | Packaging and operations; dependencies unpinned | Reproducible, supportable installs | Pinned dependencies, container image, runbooks | Planned |
| G21 | Kubernetes coverage is Services, Deployments, StatefulSets and bare Pods | DaemonSets, Jobs/CronJobs, Ingress and ExternalName services carry dependencies too | Read the remaining workload and routing kinds | Planned |

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
   scanned, and gaps are reported rather than hidden in `SUCCESS`.
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
