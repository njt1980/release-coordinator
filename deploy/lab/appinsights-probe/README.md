# Application Insights probe (lab)

Real `AppDependencies` / `AppRequests` rows from the two telemetry stacks a
.NET estate on Kubernetes typically runs, so the Application Insights adapter
(wave T2: callee resolution, scoping, gap report) is built and tested against
real data rather than documentation. The workspace keeps the rows (30 days),
so the cluster only needs to run while data is generated.

```
traffic --> gateway --HTTP--> orders --HTTP--> inventory
            (Distro, A)       (classic, B) --SQL--> mssql
                                           --Service Bus queue "orders"--> fulfillment --HTTP--> inventory
                                                                           (Distro, A)          (classic, A)
```

| Service | Telemetry stack | Application Insights resource | What it exercises |
| --- | --- | --- | --- |
| gateway | Azure Monitor OpenTelemetry Distro | A `rc-lab-appinsights` | Distro HTTP target `host[:port]`; a call into another resource |
| orders | Classic SDK 2.x + Kubernetes enricher | B `rc-lab-appinsights-b` | `inventory \| cid-v1:<appId>`, `tcp:mssql,1433 \| orders`, `sb://<ns>/ \| orders` targets; role name from the container |
| inventory | Classic SDK 2.x + Kubernetes enricher | A | Callee of two stacks and two resources |
| fulfillment | Distro | A | Service Bus consumer: producer → consumer correlation |

Both resources send to the lab workspace `rc-lab-logs`, so one workspace holds
two components, and two stacks, in one cluster. Sampling is left at each
stack's default. Traffic is one order every 2 seconds.

## Run

From this directory, with `az` logged in and kubectl on the lab cluster:

```bash
./azure.sh                                   # App Insights B + Service Bus (idempotent)
PROBE_ACR=rclab48a7217e PROBE_TAG=v1 ./build-images.sh
./create-secrets.sh                          # connection strings + SQL password -> Secret, never printed
PROBE_REGISTRY=rclab48a7217e.azurecr.io PROBE_TAG=v1 ./apply.sh
```

While the probe runs, the Beyla collector (`observability/otel-collector`)
can be scaled to 0 so the AKS Store and eShop traffic doesn't use up resource
A's 0.2 GB daily cap; `app-up` restores it.

Stop: `kubectl delete namespace appinsights-probe`, then `app-down`. The
Application Insights resource and the Service Bus namespace (Basic) cost
nothing while idle and are kept for reruns.

## Not covered

Application Insights SDK 3.x (same exporter as the Distro), Java/Node
autoinstrumentation, Event Hubs, Redis, gRPC, and sampling under load.
