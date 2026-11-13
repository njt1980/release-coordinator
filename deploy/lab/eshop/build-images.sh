#!/usr/bin/env bash
# Build and push the eShop service images in Azure with ACR Tasks, from a
# checkout of the eShop repository, using deploy/lab/eshop/Dockerfile. No
# local .NET SDK or Docker daemon is needed; az must be logged in with push
# rights to the registry.
#
#   ESHOP_ACR=<registry name> ESHOP_TAG=<tag> ./build-images.sh <eshop checkout> [service...]
#
# Image names match services.yaml: <registry>.azurecr.io/eshop/<service>:<tag>.
# The Dockerfile (and a .dockerignore, when the checkout has none) is copied
# into the checkout for the upload and removed afterwards.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "${1:?usage: build-images.sh <eshop checkout> [service...]}" && pwd)"
shift
: "${ESHOP_ACR:?set ESHOP_ACR (registry name, without .azurecr.io)}"
: "${ESHOP_TAG:?set ESHOP_TAG}"

# service name -> project directory under src/
declare -A PROJECTS=(
  [identity-api]=Identity.API
  [basket-api]=Basket.API
  [catalog-api]=Catalog.API
  [ordering-api]=Ordering.API
  [order-processor]=OrderProcessor
  [payment-processor]=PaymentProcessor
  [webhooks-api]=Webhooks.API
  [webhooksclient]=WebhookClient
  [webapp]=WebApp
)
services=("$@")
[[ ${#services[@]} -gt 0 ]] || services=("${!PROJECTS[@]}")

cleanup=("$SRC/.rc-lab.Dockerfile")
cp "$HERE/Dockerfile" "$SRC/.rc-lab.Dockerfile"
if [[ ! -e "$SRC/.dockerignore" ]]; then
  printf '%s\n' .git e2e img tests '**/bin' '**/obj' '**/node_modules' > "$SRC/.dockerignore"
  cleanup+=("$SRC/.dockerignore")
fi
trap 'rm -f "${cleanup[@]}"' EXIT

for service in "${services[@]}"; do
  project="${PROJECTS[$service]:?unknown service $service}"
  echo "== $service ($project)"
  az acr build --registry "$ESHOP_ACR" --image "eshop/$service:$ESHOP_TAG" \
    --build-arg PROJECT="$project" --file "$SRC/.rc-lab.Dockerfile" --no-logs "$SRC" \
    --query "{status:status, runId:runId}" -o tsv
done
