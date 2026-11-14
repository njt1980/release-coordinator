# Access the release coordinator needs

The backend reads three sources. It only reads; it never writes to any of
them. Grant access to a **non-personal identity**:

| Where the backend runs | Identity to use |
| --- | --- |
| A developer machine (lab) | A service principal; its credentials go in `.env` (gitignored) as `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` |
| Azure | A managed identity (no secret to store or rotate) |

The backend authenticates with `DefaultAzureCredential`, which picks the
service principal from those variables, or the managed identity on Azure,
before falling back to the developer's `az login`.

## Azure DevOps (static code scan)

- Add the identity to the organization (**Organization settings → Users**,
  access level Basic) and to the project's **Readers** group. It reads
  repositories, files and build definitions.
- Throttled requests (HTTP 429) are retried, waiting as Azure DevOps'
  `Retry-After` header says. Azure DevOps rate-limits per identity, so a
  large estate's first scan can take a while; with `RC_DATA_DIR` set, later
  scans read only files that changed. `RC_ADO_SCAN_CONCURRENCY` (default 4)
  sets how many repositories are scanned at once.
- Build definitions are read for their variables (the pipeline YAML at the
  scanned commit, definition variables and variable groups by name). If
  they can't be read, discovery still succeeds, but no environment variable
  of that repository can be shown unset.
- The organization must be connected to the Microsoft Entra tenant the
  identity belongs to.
- Personal Microsoft accounts (e.g. a Gmail or Outlook login, even when
  invited into the tenant as a guest) don't work. The `azure-devops` Python
  SDK always sends `X-VSS-ForceMsaPassThrough: true`, and with that header
  Azure DevOps rejects the account's Entra token with TF400813. Service
  principals, managed identities and native tenant members are not
  affected.

## Log Analytics (runtime telemetry)

- Role **Log Analytics Reader**, scoped to the workspace that holds
  `AppDependencies` (the Application Insights resource must be
  workspace-based). The workspace ID (`customerId`) is passed per
  onboarding request and PR webhook.
- Telemetry is scoped to the policy's cluster in one of two ways (or both):
  - `telemetry_cluster`: rows must carry a cluster attribute (by default
    `k8s.cluster.name` in `Properties`; Beyla and the OpenTelemetry
    Kubernetes attributes processor set it) equal to the value;
  - `telemetry_components`: rows must be written by one of these
    Application Insights components (their Azure resource IDs, matched
    against `_ResourceId`). This needs nothing in the row, so it is the
    scope for the classic SDK and the Azure Monitor Distro. It proves the
    cluster only if those components receive telemetry from that cluster
    alone (one component per environment).

  Only matching rows are counted, so a workspace shared by several clusters
  or applications is safe.
- Resource attributes read from every dependency row:
  - with `telemetry_cluster`, the cluster attribute above
    (`k8s.cluster.name` by default): rows without it are never counted as
    this cluster's;
  - `k8s.namespace.name` (or the classic Kubernetes enricher's
    `Kubernetes.Pod.Namespace`): without it a source is matched by bare name,
    which is ambiguous when the name exists in several namespaces;
  - `service.name` (or the role name Application Insights derives from it):
    the workload's name as Kubernetes knows it.
- Whether rows carry these attributes depends on the telemetry stack. Most
  .NET estates that send to Application Insights do **not** carry them
  today, so every row is filtered out and busy services look silent:

  | Telemetry stack | `k8s.*` in the row's Properties? | What to do today |
  | --- | --- | --- |
  | Beyla (eBPF) → OpenTelemetry Collector → Azure Monitor | Yes (set by the Collector's Kubernetes attributes processor; seen in our lab) | Nothing |
  | OpenTelemetry SDK → Collector with the Kubernetes attributes processor → Azure Monitor | Yes, inferred from the same Collector path; not tested with an application SDK | Nothing |
  | Azure Monitor OpenTelemetry Distro, `Microsoft.OpenTelemetry`, Application Insights SDK 3.x (.NET) | **No.** The exporter keeps resource attributes (`OTEL_RESOURCE_ATTRIBUTES`, Kubernetes detectors) out of Properties; only span attributes get there | Add the cluster and namespace as *span* attributes (a span processor in the application), route through a Collector, or use Beyla |
  | Classic Application Insights SDK 2.x (with or without the Kubernetes enricher) | **No.** The enricher writes `Kubernetes.*` keys and has no cluster key | Add the properties with a telemetry initializer, or use Beyla |

  For those stacks, scope by `telemetry_components` instead of a cluster
  attribute: no application change is needed. The classic SDK with the
  Kubernetes enricher still gives each source its namespace; Distro rows
  carry none, so their sources are matched by bare name.
- Application Insights target formats (`host | cid-v1:<appId>`,
  `server | database`, `sb://namespace/ | queue`) are understood: the host is
  the dependency and the database, queue or callee component is kept with
  the edge.
- Callees are also identified from their own telemetry: a dependency row's
  `Id` is the `ParentId` of the request it caused (same `OperationId`), so
  a call through a gateway, to an IP or to another component's ID still
  names the service that answered, and a message send names the consumer
  that processed it. Each such edge records `resolution: request_join` and
  the address it was reached at. This needs the callee's `AppRequests` in
  the queried workspace and within scope. The join reads the last 2 days
  (ending 10 minutes ago, as request rows arrive later); older or unjoined
  calls are still found by their address. A messaging consumer is only
  visible when it traces message processing (Azure Monitor Distro:
  `AZURE_EXPERIMENTAL_ENABLE_ACTIVITY_SOURCE=true`).
- Call counts are `sum(ItemCount)`, so sampled rows count for the calls they
  stand for. Dependencies to the Kubernetes API (the classic SDK's
  Kubernetes enricher) are dropped as `kubernetes_api` noise.
- When a radius service is uncovered, assessments count the dropped rows by
  source name and say which uncovered service they look like
  (`LiveCoverage.unscoped_sources`); the rows still never count.
- Role assignments can take a few minutes to take effect.

## Kubernetes (manifest scan)

- Read-only (`get`, `list`) on `services`, `configmaps` and `pods` (core),
  `deployments`, `statefulsets`, `daemonsets` and `replicasets` (apps), and
  `jobs` and `cronjobs` (batch) in each target namespace. Secrets are never
  read. A kind that can't be listed makes the stage PARTIAL, so no baseline
  is written.
- From the same objects, discovery records each workload's container images
  and, when the Azure DevOps `KubernetesManifest` task deployed it, the
  pipeline run from its `azure-pipelines/*` annotations. No extra permission.
- Locally the backend uses the current kubeconfig context; on Azure it uses
  the in-cluster service account or the cluster's Entra integration.
- Whichever it uses, discovery records the API server it actually read. The
  project policy's `kubernetes_api_server` must be that server
  (`kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'`),
  or the baseline is rejected as `BASELINE_SCOPE_MISMATCH`.

## Claude (risk model and Ask assistant)

- `ANTHROPIC_API_KEY` in `.env` (gitignored). Without it the risk model
  can't be consulted and the Ask panel says it isn't configured.
- The assistant uses `claude-opus-5-5` unless `ASSISTANT_MODEL` is set.

## Lab setup (2026-11-13)

| Identity | Grants |
| --- | --- |
| Service principal `rc-backend-local` (tenant Default Directory) | Log Analytics Reader on workspace `rc-lab-logs`; Azure DevOps org user with project Readers |
| Operator's kubeconfig | AKS `RC-Cluster` admin credentials |

The client secret expires on 2027-02-11. Renew it with
`az ad app credential reset --id <appId> --append --end-date <date>` and
update `.env`.
