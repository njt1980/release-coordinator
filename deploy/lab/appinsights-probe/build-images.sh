#!/usr/bin/env bash
# Build and push the probe images in Azure with ACR Tasks (no local .NET SDK
# or Docker needed). Images: <registry>.azurecr.io/appinsights-probe/<service>:<tag>.
#
#   PROBE_ACR=<registry name> PROBE_TAG=<tag> ./build-images.sh [service...]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
: "${PROBE_ACR:?set PROBE_ACR (registry name, without .azurecr.io)}"
: "${PROBE_TAG:?set PROBE_TAG}"
declare -A PROJECTS=([gateway]=Gateway [orders]=Orders [inventory]=Inventory [fulfillment]=Fulfillment [billing]=Billing)
services=("$@")
[[ ${#services[@]} -gt 0 ]] || services=(gateway orders inventory fulfillment billing)

for service in "${services[@]}"; do
  project="${PROJECTS[$service]:?unknown service $service}"
  echo "== $service ($project)"
  az acr build --registry "$PROBE_ACR" --image "appinsights-probe/$service:$PROBE_TAG" \
    --build-arg PROJECT="$project" --file "$HERE/Dockerfile" --no-logs "$HERE" \
    --query "{status:status, runId:runId}" -o tsv
done
