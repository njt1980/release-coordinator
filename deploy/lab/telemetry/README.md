# Lab telemetry (no code changes)

Live dependency telemetry for the AKS Store lab, collected with eBPF so the
workloads' code and images stay untouched. This is the same path a team
without existing instrumentation would use.

```
pods in namespace default --eBPF--> Beyla (DaemonSet)
  --OTLP--> otel-collector (namespace observability)
  --azuremonitor exporter--> Application Insights rc-lab-appinsights
  --> Log Analytics workspace, table AppDependencies
  --> release coordinator (TELEMETRY_SOURCE=azure)
```

| File | Contents |
| --- | --- |
| `beyla.yaml` | Beyla 3.40.0 DaemonSet, RBAC and config (instruments namespace `default` only) |
| `collector.yaml` | Collector-contrib 0.164.0: OTLP in, CLIENT/PRODUCER spans only, resource attributes kept (the backend reads `service.name`, `k8s.namespace.name` and `k8s.cluster.name` from `Properties`), Azure Monitor out |

`.claude/skills/app-up/lab.sh up` applies both files and creates the Secret
`observability/appinsights` (key `connection-string`) from the Application
Insights resource. The connection string is never committed.

## Azure resources

- Log Analytics workspace `rc-lab-logs` (workspace ID
  `b430b9a6-1491-4b12-a744-49a5b9176580`), resource group
  `ReleaseCoordinatorLab`, 30-day retention, daily cap 0.2 GB. Dedicated to
  the lab so lab data stays out of other workspaces.
- Application Insights `rc-lab-appinsights` in the same resource group,
  workspace-based on `rc-lab-logs`, daily cap 0.2 GB.

Neither costs anything while no data arrives, so `app-down` leaves them.

## Point the release coordinator at it

Set `TELEMETRY_SOURCE=azure` in `.env`, then pass the workspace ID
(`customerId` of the workspace above, from
`az monitor log-analytics workspace show ... --query customerId`) as the
telemetry workspace when onboarding a project and in PR webhook payloads.
The backend takes it per request, not from the environment.

Check what arrives:

```kusto
AppDependencies
| where TimeGenerated > ago(1h)
| summarize calls=count(), last=max(TimeGenerated)
    by Cluster=tostring(Properties["k8s.cluster.name"]),
       Namespace=tostring(Properties["k8s.namespace.name"]),
       Source=tostring(Properties["service.name"]), Target, DependencyType
| order by Source asc
```

Beyla detects the cluster name itself; on AKS `k8s.cluster.name` is the
node resource group (`MC_ReleaseCoordinatorLab_RC-Cluster_eastus`). The
project policy's `telemetry_cluster` must name that value.

## Known limits

- Beyla runs privileged with `hostPID`; review on the security track before
  any shared deployment.
- The collector drops loopback calls (`127.0.0.1`, `::1`, `localhost`):
  they stay inside one pod.
- `azuremonitorexporter` v0.164.0 builds `Target` for database and messaging
  calls from `client.address`, falling back to the peer ClusterIP. The
  collector copies `server.address`/`server.port` (which Beyla resolves to
  the Service name) into `client.*`, so `Target` is e.g. `documentdb:10260`.
- Beyla reports a broker delivering to a consumer as a publish from the
  broker to the consumer's ephemeral port; the collector drops messaging
  spans whose peer port is 32768 or higher. Failed TCP connects (span name
  `CONNECT`, no protocol) are dropped too: the exporter would use the span
  name as `Target`.
- Message consumption (CONSUMER spans, e.g. makeline-service reading from
  rabbitmq) is dropped with the other non-dependency spans, so that edge is
  not observed; the publishing side (`order-service -> rabbitmq:5672`) is.
- Workloads without a Kubernetes Service (`virtual-customer`,
  `virtual-worker`) resolve only when the static-code scan registers them.

Observed on the lab (2026-11-13), with no browser traffic on the store
front ends: `virtual-customer -> order-service`, `virtual-worker ->
makeline-service`, `makeline-service -> documentdb:10260`,
`order-service -> rabbitmq:5672`.
- Beyla skips processes that already use the OpenTelemetry SDK
  (`exclude_otel_instrumented_services`), so SDK and eBPF don't report the
  same call twice.
