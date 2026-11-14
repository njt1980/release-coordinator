# Discovery-accuracy benchmark

This benchmark measures how well the static scanner finds services and dependencies in real public
microservices apps, compared with each app's documented dependency graph. Its job is to catch scanner
changes that make discovery worse, and above all **silent misses**.

A missed edge is a *silent miss* when the calling service reports complete coverage. For a missed edge to
be *flagged*, the caller must report something unscanned or not understood. Silent misses break the
fail-closed promise: the coordinator sees "complete", but a dependency is missing.

## Run it

```bash
python -m tests.benchmarks.discovery.run_bench               # all apps, compare with baseline.json
python -m tests.benchmarks.discovery.run_bench eShop         # one app
python -m tests.benchmarks.discovery.run_bench --offline     # use cached checkouts only
python -m tests.benchmarks.discovery.run_bench --out /tmp/bench   # also write per-app details
python -m tests.benchmarks.discovery.run_bench --update-baseline  # accept the current numbers
```

- **First run:** fetches each pinned commit (shallow) into `~/.cache/rc-bench` (`RC_BENCH_CACHE` overrides
  this), about 350 MB in total. Later runs are offline and take about a minute.
- **What it runs:** the real scanner, through a fake Azure DevOps git client that serves files from disk.
  Checked-out code is only read, never built or executed.
- **Exit codes:**
  - 0: OK.
  - 1: ratchet regression (see `ratchet.py`): silent misses rose, or a precision or recall dropped by more
    than 0.02.
  - 2: checkout failure.
- **Accepting improvements:** re-run with `--update-baseline` and commit `baseline.json` in the same PR.

The default test run (`python -m unittest discover -s tests`) only runs `test_benchmark.py`. That file
checks the scoring, the ratchet, the env emulation and the integrity of the ground truth, offline. The
benchmark itself runs in CI through `.github/workflows/discovery-benchmark.yml`.

## Tuned vs held-out apps, and acceptance

Each app file has a `split`:

- **`tuned`** (the original 7): the scanner was changed while looking at these apps' misses, so their numbers flatter it.
- **`held_out`**: never used to tune (bank-of-anthos, piggymetrics, robot-shop and eShopOnContainers were, until step 1h). Their ground truth was
  built by a separate agent that never saw the scanner or its output.

Acceptance is judged on held-out apps only, per app (`acceptance.py`, agreed 2026-11-14):

1. no silent miss;
2. at least 50% of flagged causes point at a real missed dependency.

```bash
python -m tests.benchmarks.discovery.run_bench --offline --acceptance
```

**Rule:** once a scanner change is made by looking at a held-out app's misses, that app becomes `tuned`. An honest
re-measurement then needs new held-out apps. CI runs the ratchet, not acceptance.

**Result on 2026-11-14 (FAILED):** across the held-out apps, manifest-mode recall is 0.40 and precision 0.61, with 30
silent misses. 38% of flagged causes point at a real miss. On the tuned apps the same scanner shows recall 0.73,
precision 0.98 and 7 silent misses.

**Step 1h (2026-11-14):** the scanner was then fixed by studying those four apps' misses, so they are now `tuned`
(their numbers below flatter the scanner), and there is no held-out app until new ones are added. Fixed, in general
form: bare-host defaults for host-named variables (`CART_HOST=cart`, `DB_HOST=mysql` in a Dockerfile, `|| 'redis'`,
Go `LookupEnv` fallbacks, PHP `?:`, PDO DSNs); nginx and Envoy proxy configurations, including envsubst templates;
Dockerfiles nested under `src/<group>/<service>`; Envoy-only gateways; environment reads through a literal list or
a file-local wrapper. On the four apps: recall 0.66, precision 0.71, 16 silent misses (piggymetrics' Spring Cloud
Config and Eureka wiring: 12; eShopOnContainers' external-hostname and browser-side calls: 3; a public SaaS URL: 1).

**Step 1h-b:** an in-repository Spring Cloud config server's served files (`native` `classpath:` search locations) are
read as its clients' configuration; a YAML-nested `spring.cloud.config.uri` now flags the client when its server is
not in the repository; Zuul `serviceId` routes are registry calls; a variable only imported from a ConfigMap several
services share (`envFrom`) is no Kubernetes-environment edge on its own. Pooled: recall 0.78, precision 0.87,
10 silent misses (piggymetrics 1). Trade-off: eShop's lab shares its event-bus ConfigMap, so one true edge
(webapp to eventbus) is now a flagged miss instead of a K8S_ENV edge.

## What is measured

| Metric | Meaning |
|---|---|
| inventory P/R | Scanner service boundaries vs the app's real services (bogus boundaries such as test or tooling directories count against precision). |
| static P/R | Edges from literal URLs in code only. |
| manifest P/R | Static edges plus env-variable references resolved against env read from the app's own deployment files. This emulates the Kubernetes source. |
| missed flagged / silent / no-boundary | How each missed edge (manifest mode) shows up to a coordinator. |
| flags useful (services, causes) | Of the services flagged incomplete (and of the causes, services sharing the same unread files), how many have a real missed edge; the rest are false alarms. |

Edges are split into `svc` (service to service) and `infra` (service to database, cache or broker).
Unresolved targets count as false positives in `all`.

## Ground truth (`apps/<app>.json`)

Each file has:
- the app's repositories pinned to exact commits;
- the env sources;
- the nodes and edges of the documented dependency graph;
- a hand-made `mapping` from scanner names to ground-truth names.

Rules:
- **Every edge cites its primary source** (file:line or documentation URL) in `source`. A change to the
  ground truth must cite one too.
- Edges marked `ambiguous` or `optional` are never scored.
- `mapping.boundaries` maps a scanner service (`name` or `repo:name`) to a ground-truth name. `null` marks a
  bogus boundary, and `__optional__` means not scored.
- `mapping.hosts` maps reference hosts. `__noise__` means ignore the host, e.g. a documentation URL.
- Env source paths start with a pinned repository name, or with `@project/` for files in this repository.

Known caveats:
- One person built the ground truth from primary sources.
- eShop's env emulation uses this repository's own lab manifests (`deploy/lab/eshop`), which is circular.
- For Online Boutique and the OpenTelemetry demo, part of the ground truth comes from the same env wiring
  the manifest mode reads.
- Keep at least two apps whose ground truth does not come from env wiring (aks-store-demo, TeaStore, eShop's
  AppHost), so the manifest numbers stay honest.

| App | Upstream | Stack |
|---|---|---|
| aks-store-demo | Azure-Samples/aks-store-demo | Node, Go, Rust, Python, Vue; RabbitMQ |
| eShop | dotnet/eShop | .NET (Aspire), gRPC, RabbitMQ, Postgres, Redis |
| online-boutique | GoogleCloudPlatform/microservices-demo | Go, C#, Node, Python, Java; gRPC |
| otel-demo | open-telemetry/opentelemetry-demo | 10+ languages; gRPC, HTTP, Kafka |
| sock-shop | microservices-demo/* (archived; 9 repos) | Java, Go, Node; RabbitMQ |
| spring-petclinic-microservices | spring-petclinic/spring-petclinic-microservices | Spring Cloud (Eureka, Gateway, Config) |
| teastore | DescartesResearch/TeaStore | Java; registry-based discovery |
