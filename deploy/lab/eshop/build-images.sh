#!/usr/bin/env bash
# Build and push the eShop service images with the .NET SDK's container
# support (eShop has no Dockerfiles). Needs the .NET SDK eShop's global.json
# pins, a checkout of the eShop repository, and push access to the registry
# (for Azure Container Registry: az acr login -n <name>). No Docker daemon is
# needed to push to a remote registry.
#
#   ESHOP_REGISTRY=<registry host> ESHOP_TAG=<tag> ./build-images.sh <eshop checkout>
#
# Image names match services.yaml: $ESHOP_REGISTRY/eshop/<service>:$ESHOP_TAG.
set -euo pipefail

SRC="${1:?usage: build-images.sh <eshop checkout>}"
: "${ESHOP_REGISTRY:?set ESHOP_REGISTRY}"
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

for service in "${!PROJECTS[@]}"; do
  project="${PROJECTS[$service]}"
  echo "== $service ($project)"
  dotnet publish "$SRC/src/$project/$project.csproj" -c Release \
    -t:PublishContainer \
    -p:ContainerRegistry="$ESHOP_REGISTRY" \
    -p:ContainerRepository="eshop/$service" \
    -p:ContainerImageTag="$ESHOP_TAG"
done
