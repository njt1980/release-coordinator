#!/usr/bin/env bash
# Deploy the OpenTelemetry Demo lab into namespace otel-demo of the current
# kubectl context: the generated demo manifests, then the collector with its
# Application Insights connection string (read from Azure, never printed).
set -euo pipefail
cd "$(dirname "$0")"

RG="${RC_AKS_RG:-ReleaseCoordinatorLab}"
APPINSIGHTS="${RC_OTEL_DEMO_APPINSIGHTS:-rc-lab-appinsights-b}"

kubectl apply -f demo.yaml >/dev/null
connection="$(az monitor app-insights component show -g "$RG" --app "$APPINSIGHTS" --query connectionString -o tsv)"
kubectl -n otel-demo create secret generic appinsights \
  --from-literal=connection-string="$connection" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
unset connection
kubectl apply -f collector.yaml >/dev/null
echo "Applied. Waiting for deployments in otel-demo..."
kubectl -n otel-demo wait --for=condition=Available deployment --all --timeout=600s
