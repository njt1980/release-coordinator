# Showcase deployment

One container on Azure Container Apps serves the API and the built frontend. It sits behind Entra
sign-in, and only users assigned to the `rc-showcase-signin` enterprise app can open it. Data (baselines,
policies, review answers, finished assessments) lives on the Azure Files share `rcdata`, mounted at `/data`.

| Resource (group `rc-showcase`, eastus) | Purpose |
|---|---|
| Container App `rcshow-app` (min 0, max 1 replica) | The server. It scales to zero when idle, so the first request after a pause waits for a cold start. |
| Container Apps environment `rcshow-env` | Logs go to the existing `rc-lab-logs` workspace. |
| Managed identity `rcshow-id` | Pulls the image (AcrPull on `rclab48a7217e`) and reads Key Vault secrets. |
| Key Vault `rcshow-kv-*` | `anthropic-api-key`, `backend-client-secret` (service principal `rc-backend-local`, read-only). |
| Storage account `rcshowst*`, share `rcdata` | `/data`: `baselines/`, `policies/`, `reviews/reviews.sqlite3`, `assessments/`. |
| Budget `rc-showcase-monthly` (subscription) | Alerts subscription Owners at 80% of $25 actual and 100% forecast. |

One replica at most: SQLite on a network share uses the rollback journal (WAL needs shared memory)
and must have a single writer.

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

# 2. Secrets (the operator needs Key Vault Secrets Officer on the vault).
az keyvault secret set --vault-name <vault> -n anthropic-api-key --file <file>
az keyvault secret set --vault-name <vault> -n backend-client-secret --file <file>

# 3. Seed the share: policies (and baselines to show) under /data.
az storage file upload-batch --account-name <storage> -d rcdata/policies -s policies

# 4. The app (same command with deployApp=true image=release-coordinator:showcase-<sha>).

# 5. Sign-in: redirect URI https://<fqdn>/.auth/login/aad/callback on the app registration, then
az containerapp auth microsoft update -g rc-showcase -n rcshow-app \
  --client-id <signin appId> --client-secret <secret> --tenant-id <tenant> --yes
az containerapp auth update -g rc-showcase -n rcshow-app \
  --unauthenticated-client-action RedirectToLoginPage --enabled true
```

Give someone access: add them (a guest from another organization works) under *Enterprise applications →
rc-showcase-signin → Users and groups*.

## What the showcase does not do yet

- **Onboarding (discovery) from the cloud:** there's no Kubernetes access from the container. Baselines are
  produced locally and uploaded to the share.
- **Webhook `POST /api/v1/webhooks/pr-assessment`:** disabled (`RC_DISABLE_RAW_WEBHOOK`). Pull requests are
  read from Azure DevOps by PR intake.
- **Roles:** anyone assigned can answer the review. The coordinator app role is a later step.
