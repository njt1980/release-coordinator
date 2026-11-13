# eShop lab (.NET, plain manifests)

The second lab application: [dotnet/eShop](https://github.com/dotnet/eShop)
(imported into the Azure DevOps project as `eShop`), deployed with
hand-written Kubernetes manifests: no Helm, no Kustomize, no Aspire publisher.
It exercises what the AKS Store lab can't: C# configuration (`appsettings`,
`ConnectionStrings`, `Identity__Url`), shared libraries, gRPC, RabbitMQ
messaging, and service names that differ from their directories.

The eShop repository itself is left as imported (upstream code we don't
own); these manifests live here, like the lab's telemetry and traffic.

## Files

| File | What it deploys |
|---|---|
| `infrastructure.yaml` | namespace `eshop`; `postgres` (pgvector, the AppHost's four databases), `eventbus` (RabbitMQ), `redis` |
| `services.yaml` | the nine .NET services, wired as `src/eShop.AppHost/AppHost.cs` wires them |
| `traffic.yaml` | read-only GET traffic in `lab-traffic` (web app pages, catalog API) |
| `apply.sh` | deploys all of the above in order, filling in the image registry and tag (`--dry-run`: server-side dry run) |
| `create-secrets.sh` | the random PostgreSQL password Secret `eshop/eshop-postgres` |
| `build-images.sh` | builds and pushes the service images with `dotnet publish -t:PublishContainer` |

## How the AppHost maps to the manifests

| AppHost | Manifests |
|---|---|
| `WithReference(postgres database)` | `ConnectionStrings__<db>=Host=postgres;...;Database=<db>` (no password) + `PGPASSWORD` from the Secret |
| `WithReference(rabbitMq)` | `ConnectionStrings__eventbus=amqp://eventbus:5672` via ConfigMap `eshop-eventbus` (`envFrom`) |
| `WithReference(redis)` | `ConnectionStrings__redis=redis:6379` |
| `WithReference(<project>)` | `services__<project>__http__0=http://<project>` and a Service on port 80 |
| `WithEnvironment("Identity__Url", ...)` and the callback URLs | the same variables, with in-cluster URLs |
| `AddForwardedHeaders()` | `ASPNETCORE_FORWARDEDHEADERS_ENABLED=true` (ConfigMap `eshop-common`) |

Deployment names are the AppHost's resource names (`order-processor`,
`payment-processor`, `webhooksclient`). The static scanner names services
after their directories (`orderprocessor`, `paymentprocessor`,
`webhookclient`), so reconciliation has to cope with the difference, or the
repository needs a `release-coord.yaml`. That is deliberate: it is how real
repositories look.

## Deploy

Prerequisites: the lab cluster running (`app-up`), and the images built and
pushed to a registry the cluster can pull from:

```bash
ESHOP_REGISTRY=<registry> ESHOP_TAG=<tag> ./build-images.sh <eshop checkout>
```

Then:

```bash
ESHOP_REGISTRY=<registry> ESHOP_TAG=<tag> deploy/lab/eshop/apply.sh --dry-run  # validates, changes nothing
ESHOP_REGISTRY=<registry> ESHOP_TAG=<tag> deploy/lab/eshop/apply.sh
```

Services restart until PostgreSQL and RabbitMQ are ready, and
`order-processor` until `ordering-api` has run the ordering migrations.
Beyla instruments namespace `eshop` (see `../telemetry/beyla.yaml`); its
ConfigMap is read at startup, so restart the Beyla DaemonSet after changing it.

Onboard with namespace `eshop` and repository `eShop` (branch `main`).

## Sizing

Requests total about 0.7 CPU and 2 GiB of memory (limits up to about
5.5 GiB), on top of the AKS Store lab. The single-node lab cluster may need
a larger node or a second one.

## Limitations

- Nothing signs in: Identity.API's redirects need a browser-reachable URL,
  so basket and ordering calls (which need a user) don't happen in the lab.
- `mobile-bff` (a YARP proxy for the MAUI app) isn't deployed; neither are
  the MAUI apps, which run on devices.
- AI features are off (the AppHost's `UseFoundry` default).
- Credentials are lab-grade: RabbitMQ uses its public `guest` default and
  Redis has no password; both are reachable only inside the cluster.
