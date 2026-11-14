#!/usr/bin/env bash
# Create the probe's Azure resources, if missing (idempotent, never deletes):
#   - Application Insights rc-lab-appinsights-b, workspace-based on the lab
#     workspace rc-lab-logs, daily cap 0.2 GB (resource A, rc-lab-appinsights,
#     already exists: see ../telemetry/README.md);
#   - Service Bus namespace (Basic) with queue "orders" and a queue-scoped
#     Send+Listen rule "probe".
# Neither costs anything while idle.
set -euo pipefail

RG="${PROBE_RG:-ReleaseCoordinatorLab}"
LOCATION="${PROBE_LOCATION:-eastus}"
WORKSPACE="${PROBE_WORKSPACE:-rc-lab-logs}"
COMPONENT_B="${PROBE_COMPONENT_B:-rc-lab-appinsights-b}"
SUBSCRIPTION="$(az account show --query id -o tsv)"
SB_NAMESPACE="${PROBE_SB_NAMESPACE:-rclabprobe${SUBSCRIPTION%%-*}}"

workspace_id="$(az monitor log-analytics workspace show -g "$RG" -n "$WORKSPACE" --query id -o tsv)"
if az monitor app-insights component show -g "$RG" --app "$COMPONENT_B" > /dev/null 2>&1; then
  echo "application insights $COMPONENT_B exists; kept"
else
  az monitor app-insights component create -g "$RG" --app "$COMPONENT_B" -l "$LOCATION" \
    --kind web --application-type web --workspace "$workspace_id" -o none
  echo "application insights $COMPONENT_B created"
fi
az monitor app-insights component billing update -g "$RG" --app "$COMPONENT_B" --cap 0.2 -o none

if az servicebus namespace show -g "$RG" -n "$SB_NAMESPACE" > /dev/null 2>&1; then
  echo "service bus $SB_NAMESPACE exists; kept"
else
  az servicebus namespace create -g "$RG" -n "$SB_NAMESPACE" -l "$LOCATION" --sku Basic -o none
  echo "service bus $SB_NAMESPACE created"
fi
az servicebus queue show -g "$RG" --namespace-name "$SB_NAMESPACE" -n orders > /dev/null 2>&1 \
  || az servicebus queue create -g "$RG" --namespace-name "$SB_NAMESPACE" -n orders -o none
az servicebus queue authorization-rule show -g "$RG" --namespace-name "$SB_NAMESPACE" \
    --queue-name orders -n probe > /dev/null 2>&1 \
  || az servicebus queue authorization-rule create -g "$RG" --namespace-name "$SB_NAMESPACE" \
    --queue-name orders -n probe --rights Send Listen -o none
echo "service bus queue $SB_NAMESPACE/orders ready"
