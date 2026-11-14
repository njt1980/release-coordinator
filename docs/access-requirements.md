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

- Onboarding lists the organization's projects and a project's repositories
  (`POST /api/discover/devops/projects`, `/repositories`) so they can be
  picked instead of typed; disabled and empty repositories are not offered.
  Same read identity and organization allowlist; no extra permission.

### Pull request polling and feedback (`ado_connection`, opt-in)

All off by default; each project turns them on in its policy, and they
need `source_control`:

```yaml
ado_connection:
  poll: true                 # read-only; needs nothing beyond Readers
  repositories: [shop-api]   # required when polling
  target_branches: [main]    # optional; empty = any
  interval_minutes: 10
  change_type: additive      # labels rc:breaking / rc:additive override it
  status: false              # advisory status, genre release-coordinator
  comment: false             # the briefing as one closed thread
  link_base_url: ""          # optional: the status links to the record view
```

- **Polling** lists the active pull requests of `repositories` and assesses
  each new iteration (new source commit or retarget) through PR intake; the
  last handled iteration per pull request is kept in
  `{RC_DATA_DIR}/pr-watch/{tenant}/{project}.json`. The server starts the
  poller only if some policy turns it on when it starts, so turning it on for
  the first project needs a restart.
- **Status and comment** are the only writes to Azure DevOps. They go to the
  pull request's iteration that was assessed, in the policy's
  `source_control` project only, and need **Contribute to pull requests**
  (Git repositories permission `PullRequestContribute`) for the identity on
  that project. The project's **Readers** group has it by default; check
  with **Project settings → Repositories → Security**. The status is
  advisory: it blocks nothing unless a branch policy requires it; don't add
  one. The comment thread is posted closed, so "comments must be resolved"
  policies aren't held up.
- Lab check (2026-11-14): `rc-backend-local` already has it on
  `Release_Coordinator`, inherited from `[Release_Coordinator]\Readers`
  (effective allow 81922, which includes 16384); no explicit grant made.

## Log Analytics (runtime telemetry)

- Role **Log Analytics Reader**, scoped to the workspace that holds
  `AppDependencies` (the Application Insights resource must be
  workspace-based). The workspace ID (`customerId`) is passed per
  onboarding request and PR webhook.
- When the estate's Application Insights components send to several
  workspaces, list the others in the policy's
  `telemetry_additional_workspaces` (up to 99 workspace IDs) and grant the
  same role on each. Every telemetry query reads all of them at once, so a
  call whose caller and callee report to different workspaces is still
  joined. A baseline that read other workspaces than the policy lists is
  `BASELINE_SCOPE_MISMATCH`. The onboarding telemetry access test checks
  every listed workspace too (for the tenant/project entered), so a missing
  role shows up there instead of later as a `DISCOVERY_GAP`.
- Sampling: a sampled row stands for `ItemCount` calls and is counted so.
  When a radius service is unseen and the telemetry is sampled, the
  explanation says so: a service called rarely may have no row in the
  window while working.
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
- Optional role filter (`telemetry_role_filter`, off by default), applied
  on top of the scope above to the discovery baseline's telemetry query:
  - `role_names_in_inventory: true` keeps rows whose role name
    (`service.name` or `AppRoleName`, without a `<namespace>/` prefix,
    case-insensitive) is a discovered service, or a workload or container
    name the Kubernetes scan read;
  - `role_instances_in_pods: true` keeps rows whose `AppRoleInstance` is the
    name of a pod the Kubernetes scan read. Pods replaced since the scan
    don't match, so use it with a short lookback.

  **This is a filter, not proof of the cluster.** Another cluster running
  the same workloads reports the same role names, and pod names are unique
  only within one cluster's namespace. It never satisfies the requirement
  for `telemetry_cluster` or `telemetry_components`, and it is recorded in
  the baseline (`scan.telemetry_role_filter`) only as what was applied. A
  part that cannot be applied (no usable inventory name, pods not read) is
  left out and the telemetry stage becomes PARTIAL with a note saying so.
  Live telemetry at assessment and the Telemetry gap report are not
  filtered: the gap report exists to show roles outside the inventory.
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
- Observability gap report: `GET /api/v1/projects/{tenant}/{project}/observability`
  probes the baseline's workspace(s) within the policy's scope and lists,
  per telemetry role, the instrumentation stack (from `SDKVersion`: classic
  SDK 2.x, Azure Monitor OpenTelemetry, OpenTelemetry Collector), rows vs
  calls represented (sampling), and whether rows carry a namespace; then the
  gaps: rows without a role name, roles matching no inventory service,
  services without telemetry, no namespace, no request telemetry (callees
  can't be confirmed by request join), sampling, and message sends whose
  consumer isn't traced. Assessments add the radius services' gaps to the
  briefing as context; they never change the verdict. Same read access, no
  extra permission.
- Which protocols the telemetry can see (LIVE_UNOBSERVABLE) is judged per
  service from the stacks its own rows show (`src/core/telemetry_capability.py`,
  `PROFILES`). An edge counts as observable when the profile of either end
  sees its protocol; a service with no telemetry of its own sees nothing.
  Only claims observed in the probe lab or stated in vendor documentation
  count; unverified claims are listed but never clear a service.

  | Profile | Observable (evidence) | Unverified |
  |---|---|---|
  | Beyla 3.x (`beyla-3`) | HTTP(S), gRPC, Redis, PostgreSQL, MySQL, SQL Server, MongoDB, Kafka, NATS, MQTT (docs) | — |
  | Classic SDK 2.x for .NET | HTTP, SQL Server, Service Bus (lab); HTTPS (docs) | gRPC |
  | Azure Monitor Distro for .NET | HTTP (lab); HTTPS, SQL Server (docs) | gRPC |
  | … with `AZURE_EXPERIMENTAL_ENABLE_ACTIVITY_SOURCE=true` | as above + Service Bus (lab) | gRPC |
  | Plain OpenTelemetry SDK | none | everything: depends on the instrumentation libraries |

  The Distro variant with the activity source is picked when a role's rows
  include `Microsoft.ServiceBus` dependencies or `ServiceBusProcessor.*`
  requests. Collector rows (`otelc-…`) don't say what made the spans, so
  the policy's `telemetry_collector_source` (`beyla` or `otel_sdk`, the
  default) picks the profile. Service Bus addresses (`sb://`, or `amqps://`
  to `*.servicebus.windows.net`) are the protocol `servicebus`, separate
  from other AMQP-over-TLS brokers (`amqps`). When the role profiles can't
  be read, the Beyla profile judges every edge, as before.
- When a radius service is uncovered, assessments count the dropped rows by
  source name and say which uncovered service they look like
  (`LiveCoverage.unscoped_sources`); the rows still never count.
- Role assignments can take a few minutes to take effect.
- Consumer attribution (advisory): when a pull request breaks an OpenAPI
  operation, the assessment asks the same workspaces which callers' requests
  the serving service handled, by request name (the route), joining each
  request to its caller's dependency span. Same read permission; nothing
  extra. It only says who was seen using which operation in the lookback
  window (sampled or rare calls can be missing) and never changes the
  contract check's result.

## Azure Service Bus (broker inventory, optional)

- Only when the project policy lists `messaging.service_bus_namespaces`
  (subscription ID, resource group, namespace). Discovery then lists each
  namespace's queues, topics and topic subscriptions through the Azure
  Resource Manager REST API (GET only; `azure-core` with
  `DefaultAzureCredential`, no extra package).
- Needs the built-in **Reader** role on each namespace (or its resource
  group). Never reads messages, keys or connection strings.
- Advisory: entities no scanned code declares are listed in the inventory
  (`broker_only`), and a channel with unknown consumers says whether the
  broker lists subscriptions on it. A failed read is recorded
  (`broker_errors`), never fails discovery, and nothing here adds a
  producer, a consumer or an edge.

## Kubernetes (manifest scan)

- Read-only (`get`, `list`) on `services`, `configmaps` and `pods` (core),
  `deployments`, `statefulsets`, `daemonsets` and `replicasets` (apps), and
  `jobs` and `cronjobs` (batch) in each target namespace. Secrets are never
  read. A kind that can't be listed makes the stage PARTIAL, so no baseline
  is written.
- From the same objects, discovery records each workload's container images
  and, when the Azure DevOps `KubernetesManifest` task deployed it, the
  pipeline run from its `azure-pipelines/*` annotations. No extra permission.
- OCI image provenance annotations on a workload or its pod template
  (`org.opencontainers.image.title`, `.source`, `.revision`) are recorded
  too and used for identity only: the title as a name the workload declares,
  the source repository's name to tie it to the source service scanned from
  that repository (only when exactly one unlinked source service points at
  it). A Dockerfile's literal `org.opencontainers.image.title` `LABEL` names
  the image built from its directory. Never an edge.
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
- **What leaves the platform:** the advisory risk review sends the pull
  request's title, description and diff with the assessment context (service
  names, dependencies, telemetry summary); the assistant sends the user's
  question and the platform records its tools read (no source code). Both go
  to Anthropic's Claude API, which processes them as a sub-processor; list it
  in the customer's data-processing records.
- **Redaction:** everything sent is redacted first (`src/core/redaction.py`):
  private keys, credentials in URLs, known token shapes (Anthropic, GitHub,
  AWS, Slack, JWT, bearer, Azure SAS signatures), connection-string secrets,
  values assigned to `*key*`/`*secret*`/`*token*`/`*password*` names, and
  e-mail addresses become `[redacted:<kind>]`. Counts per kind are recorded
  on the assessment (`advisory_redactions`). The patterns find well-known
  shapes, not every secret.
- **Per-project switch:** `external_ai: false` in the project policy keeps
  the project's data from the model: no advisory risk review (the
  assessment says `ADVISORY_DISABLED` and is never approved automatically; a
  person decides) and the assistant neither answers on that project nor
  returns its data from other pages. Default `true`. A project whose policy
  can't be read is treated as `false` by the assistant.

## Lab setup (2026-11-13)

| Identity | Grants |
| --- | --- |
| Service principal `rc-backend-local` (tenant Default Directory) | Log Analytics Reader on workspace `rc-lab-logs`; Azure DevOps org user with project Readers |
| Operator's kubeconfig | AKS `RC-Cluster` admin credentials |

The client secret expires on 2027-02-11. Renew it with
`az ad app credential reset --id <appId> --append --end-date <date>` and
update `.env`.
