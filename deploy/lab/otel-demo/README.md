# OpenTelemetry Demo lab

The [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) 3.1.0, a larger polyglot
microservices app, run on RC-Cluster for end-to-end runs of the release coordinator. The scale: about
20 workloads in 10 languages (.NET, Go, Java, Kotlin, Python, Node.js, C++, Rust, Ruby, Elixir, PHP),
gRPC with a shared `.proto`, Kafka, PostgreSQL, Valkey and feature flags (flagd).

| File | Contents |
| --- | --- |
| `generate.py` | Turns the demo's `compose.yaml` + `compose.full.yaml` into plain Kubernetes manifests (namespace `otel-demo`). |
| `demo.yaml` | Its output for 3.1.0 (generated; do not edit). |
| `collector.yaml` | The collector that replaces the demo's own. It exports traces (no internal spans) to Application Insights `rc-lab-appinsights-b`. |
| `apply.sh` | Applies both and creates the Secret `otel-demo/appinsights` from Azure (never printed). |

The code is in the Azure DevOps repository `opentelemetry-demo` (project `Release_Coordinator`),
imported from GitHub. Its default branch is `release-3.1.0`, the commit the images were built from.
The policy is `policies/default/otel-demo.yaml`.

```bash
python deploy/lab/otel-demo/generate.py <checkout of tag 3.1.0> --version 3.1.0 > deploy/lab/otel-demo/demo.yaml
deploy/lab/otel-demo/apply.sh
```

Differences from Compose, all needed on Kubernetes:

- `enableServiceLinks: false`. Otherwise Kubernetes injects `FLAGD_PORT=tcp://...` and the like.
- Kafka listens on all interfaces. In Compose its own name resolves to itself; here it resolves to a
  Service IP.
- Memory requests are half of each limit, so the demo fits beside the other lab apps on 2 nodes.
- The load generator runs one user, to stay under the component's 0.2 GB daily cap.

Known telemetry quirk: through the collector's `azuremonitor` exporter, gRPC, Redis and DNS client
spans arrive with the span name as `Target` (for example `grpc.oteldemo.CartService/GetCart`, `HGET`)
instead of the server address. The release coordinator reports those as unresolved dependencies,
except where the request join names the callee.
