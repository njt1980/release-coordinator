#!/usr/bin/env bash
# Create (or refresh) Secret appinsights-probe/probe from Azure:
#   appinsights-a, appinsights-b  Application Insights connection strings
#   servicebus                    queue-scoped Send+Listen connection string
#   sa-password                   SQL Server password (random; kept if the
#                                 Secret exists, SQL Server keeps its first one)
# Values go straight to the cluster: never printed, never on disk, never in Git.
set -euo pipefail

NAMESPACE="${PROBE_NAMESPACE:-appinsights-probe}"
RG="${PROBE_RG:-ReleaseCoordinatorLab}"
SUBSCRIPTION="$(az account show --query id -o tsv)"
SB_NAMESPACE="${PROBE_SB_NAMESPACE:-rclabprobe${SUBSCRIPTION%%-*}}"

kubectl get namespace "$NAMESPACE" > /dev/null 2>&1 || kubectl create namespace "$NAMESPACE" > /dev/null
password="$(kubectl -n "$NAMESPACE" get secret probe -o jsonpath='{.data.sa-password}' 2> /dev/null | base64 -d || true)"
# SQL Server requires upper, lower, digit and symbol characters.
[[ -n "$password" ]] || password="Rc1!$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
kubectl -n "$NAMESPACE" create secret generic probe --dry-run=client -o yaml \
  --from-literal=appinsights-a="$(az monitor app-insights component show -g "$RG" --app rc-lab-appinsights --query connectionString -o tsv)" \
  --from-literal=appinsights-b="$(az monitor app-insights component show -g "$RG" --app rc-lab-appinsights-b --query connectionString -o tsv)" \
  --from-literal=servicebus="$(az servicebus queue authorization-rule keys list -g "$RG" --namespace-name "$SB_NAMESPACE" --queue-name orders -n probe --query primaryConnectionString -o tsv)" \
  --from-literal=sa-password="$password" | kubectl apply -f - > /dev/null
unset password
echo "secret $NAMESPACE/probe ready"
