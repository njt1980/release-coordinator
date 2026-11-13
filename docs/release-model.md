# Supported applications and release model

Status: draft for review (2026-11-13)

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
kind: IP address, private DNS, external DNS, unregistered service, or
unresolved environment variable.

## 2. What a team provides

| Input | Purpose | Access (see [access-requirements.md](access-requirements.md)) |
| --- | --- | --- |
| Repositories and project | Static code scan: service boundaries and the addresses each service uses | Read on the repositories |
| Cluster and namespaces | Manifest scan: Services, workloads, ConfigMaps, environment variables | `get`/`list` on those kinds; never Secrets |
| Telemetry workspace | Observed calls between services | Read on the workspace |
| Project policy | What evidence is required and what is exempt | Reviewed file in the repo (`policies/{tenant}/{project}.yaml`) |

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
4. **Live check**: query telemetry for the same workspace. An observed edge
   in the radius that the baseline lacks means the baseline no longer
   describes the system (drift).
5. **Sequence**: order the deployment deterministically. For an additive
   change, providers deploy before consumers. For a breaking change,
   consumers deploy first, and consumers the PR doesn't change are reported
   as stale.
6. **Risk**: a model writes an advisory assessment. It can reject a release
   (`MODEL_REJECTED`) but can never approve one the deterministic checks
   block.
7. **Verdict**: `approved` only when no blocking reason applies.

### Blocking reasons

| Code | Meaning |
| --- | --- |
| `POLICY_MISSING`, `POLICY_INVALID` | No usable policy for the project |
| `BASELINE_UNAVAILABLE`, `BASELINE_STALE`, `BASELINE_SCOPE_MISMATCH` | No baseline, too old, or scanned for another environment/cluster |
| `REQUIRED_SOURCE_MISSING` | A discovery source the policy requires did not succeed |
| `UNMAPPED_FILES` | Changed files no service owns and the policy doesn't exempt |
| `DISCOVERY_GAP` | An unresolved entity in the release radius |
| `BASELINE_DRIFT` | Live telemetry shows an edge in the radius that the baseline lacks |
| `DEPENDENCY_CYCLE` | No safe deployment order exists |
| `COMPATIBILITY_UNVERIFIED` | No evidence that the contract change is compatible |
| `DEMO_EVIDENCE`, `MODEL_REJECTED` | Mock data, or the risk model rejected the release |

## 4. Gaps before this works on applications we don't own

Found while running the lab end to end (2026-11-13). Each gap is phrased as
a platform capability, not a lab fix.

| # | Gap | Why it matters | Proposed capability |
| --- | --- | --- | --- |
| G1 | Telemetry noise is only filtered in our collector (loopback calls, reversed broker deliveries, failed connects, path-only URLs, exporter quirks) | Teams that bring their own telemetry won't run our collector, so the noise reaches the backend and blocks releases | Normalize in the backend's telemetry adapter as well; the collector is an optimization |
| G2 | Every unresolved external call blocks (e.g. npm's update check at container start) | Real applications call package registries, cloud APIs and SaaS; exempting entity by entity is fragile (IPs change) | **Egress policy**: allow external destinations by host pattern, CIDR or category, each with a reason |
| G3 | Fixed telemetry lookback (30 days at onboarding, 7 at assessment) | Bad or removed edges linger for a month | `runtime_lookback_days` in the project policy |
| G4 | Telemetry only from Azure Monitor `AppDependencies` | Teams use Datadog, Grafana Tempo, Prometheus and others | Telemetry adapter interface; Azure Monitor first |
| G5 | Code only from Azure DevOps | Many teams use GitHub or GitLab | Code-host adapter interface |
| G6 | A service's evidence isn't reconciled across sources (an environment variable the code scan can't resolve vs the endpoint telemetry observes; differing service names) | Gaps that other evidence already answers still block | Record each edge's sources, and resolve environment-variable references with observed endpoints |
| G7 | Messaging is modeled as service → broker | The release risk is producer → queue/topic → consumer | Queues and topics as dependency nodes |
| G8 | No contract analysis | Nothing can be approved (`COMPATIBILITY_UNVERIFIED`) | Compare API and message schemas (OpenAPI, protobuf, JSON Schema) between base and head |
| G9 | The agent bundle is a lab directory with fixed names | Every cluster differs | A versioned, parameterized bundle (namespaces, connection string) with documented known issues |
| G10 | The backend runs as a single local process with no authentication | Shared and customer use | The security track: authentication, tenant isolation, webhook signatures, managed identity |

## 5. Proposed order

1. **Step 2 (next):** G1, G2, G3 and G6, plus one service instrumented with
   the OpenTelemetry SDK so the "team already has telemetry" path is tested
   next to eBPF.
2. **Security track (G10):** before any shared deployment.
3. **Contract analysis (G8):** the main remaining blocker to approving a
   real release.
4. **Adapters (G4, G5), messaging topology (G7), agent bundle (G9):** as
   onboarding needs them.

## 6. Out of scope for now

- Applications not on Kubernetes (VMs, serverless).
- Executing the deployment. The platform decides and orders; the team's CD
  system deploys.
- Database schema migrations as a dependency type.
