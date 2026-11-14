# Showcase deployment

One container on Azure Container Apps serves the API and the built frontend. It sits behind Entra
sign-in, and only users assigned to the `rc-showcase-signin` enterprise app can open it. Data (baselines,
policies, review answers, finished assessments) lives on the Azure Files share `rcdata`, mounted at `/data`.

| Resource (group `rc-showcase`, eastus) | Purpose |
|---|---|
| Container App `rcshow-app` (min 0, max 1 replica) | The server. It scales to zero when idle, so the first request after a pause waits for a cold start. |
| Container Apps environment `rcshow-env` | Logs go to the existing `rc-lab-logs` workspace. |
| Managed identity `rcshow-id` | Pulls the image (AcrPull on `rclab48a7217e`) and reads Key Vault secrets. |
| Key Vault `rcshow-kv-*` | `anthropic-api-key`, `backend-client-secret` (service principal `rc-backend-local`, read-only), `signin-client-secret` (app registration `rc-showcase-signin`). |
| Storage account `rcshowst*`, share `rcdata` | `/data`: `baselines/`, `policies/`, `reviews/reviews.sqlite3`, `assessments/`. |
| Budget `rc-showcase-monthly` (subscription) | Alerts subscription Owners at 80% of $25 actual and 100% forecast. |

One replica at most: SQLite on a network share uses the rollback journal (WAL needs shared memory)
and must have a single writer. SMB refuses SQLite's byte-range locks, so the app sets
`RC_SQLITE_VFS=unix-dotfile`, which locks with a lock file. Mount options such as `nobrl` are not used:
revisions with them did not start.

## Deploy

```bash
# 0. Image (no local Docker): build from a clean copy of committed files only.
git archive HEAD src frontend deploy/showcase mock_telemetry.json .dockerignore | tar -x -C <empty dir>
az acr build -r rclab48a7217e -t release-coordinator:showcase-<sha> -f deploy/showcase/Dockerfile <empty dir>

# 1. Infrastructure (no app yet).
az group create -n rc-showcase -l eastus --tags project=release-coordinator purpose=showcase
az deployment group create -g rc-showcase -n infra --template-file deploy/showcase/main.bicep \
  -p acrName=rclab48a7217e acrResourceGroup=ReleaseCoordinatorLab \
     logWorkspaceName=rc-lab-logs logWorkspaceResourceGroup=ReleaseCoordinatorLab \
     backendTenantId=<tenant> backendClientId=<rc-backend-local appId> \
     adoOrgAllowlist=https://dev.azure.com/<org> deployApp=false

# 2. Secrets (the operator needs Key Vault Secrets Officer on the vault). Write each value to a
#    private temp file, set it, delete the file; never pass secrets on the command line.
az keyvault secret set --vault-name <vault> -n anthropic-api-key --file <file>
az keyvault secret set --vault-name <vault> -n backend-client-secret --file <file>
#    Sign-in: app registration (single tenant, ID tokens, "assignment required" on its
#    enterprise app), redirect URI https://<fqdn>/.auth/login/aad/callback, and its secret:
az ad app credential reset --id <signin appId> --query password -o tsv > <file>
az keyvault secret set --vault-name <vault> -n signin-client-secret --file <file>

# 3. Seed the share: policies (and baselines to show) under /data.
az storage file upload-batch --account-name <storage> -d rcdata/policies -s policies

# 4. The app, with sign-in (same command plus):
#      deployApp=true image=release-coordinator:showcase-<sha> signinClientId=<signin appId>

# 5. Budget alert (subscription Owners):
az deployment sub create -l eastus --template-file deploy/showcase/budget.bicep -p startDate=<yyyy-MM-01>
```

Keep sign-in in the template. Configuring it with `az containerapp auth` adds an app secret that the
next Bicep deployment removes, and revisions created without it never activate. If a revision is stuck
in `ActivationFailed` after the secrets are fixed, create a fresh one:
`az containerapp update -g rc-showcase -n rcshow-app --revision-suffix <new>`.

Give someone access: add them (a guest from another organization works) under *Enterprise applications →
rc-showcase-signin → Users and groups*.

## What the showcase does not do yet

- **Onboarding (discovery) from the cloud:** there's no Kubernetes access from the container. Baselines are
  produced locally and uploaded to the share.
- **Webhook `POST /api/v1/webhooks/pr-assessment`:** disabled (`RC_DISABLE_RAW_WEBHOOK`). Pull requests are
  read from Azure DevOps by PR intake.
- **Roles:** anyone assigned can answer the review. The coordinator app role is a later step.
- **Overlapping replicas:** during a revision switch an old and a new replica overlap briefly. Answers
  written in that window rely on the lock file only.
