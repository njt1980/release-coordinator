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
- Telemetry rows must carry a cluster attribute (by default
  `k8s.cluster.name` in `Properties`; Beyla and the OpenTelemetry Kubernetes
  attributes processor set it). The project policy's `telemetry_cluster`
  names the attribute and value, and only matching rows are counted, so a
  workspace shared by several clusters is safe.
- Role assignments can take a few minutes to take effect.

## Kubernetes (manifest scan)

- Read-only (`get`, `list`) on `services`, `configmaps` and `pods` (core),
  `deployments`, `statefulsets`, `daemonsets` and `replicasets` (apps), and
  `jobs` and `cronjobs` (batch) in each target namespace. Secrets are never
  read. A kind that can't be listed makes the stage PARTIAL, so no baseline
  is written.
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
