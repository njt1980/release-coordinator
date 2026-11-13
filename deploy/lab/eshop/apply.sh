#!/usr/bin/env bash
# Deploy the eShop lab: the password Secret, backing services, the .NET
# services (with image registry and tag filled in) and read-only traffic.
# Idempotent. Pass --dry-run for a server-side dry run (validates against the
# cluster API, changes nothing; the cluster must be running).
#
#   ESHOP_REGISTRY=<registry host> ESHOP_TAG=<tag> ./apply.sh [--dry-run]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
: "${ESHOP_REGISTRY:?set ESHOP_REGISTRY}"
: "${ESHOP_TAG:?set ESHOP_TAG}"
case "$ESHOP_REGISTRY$ESHOP_TAG" in
  *'|'* | *'&'* | *'\'*) echo "ESHOP_REGISTRY/ESHOP_TAG contain an unsupported character" >&2; exit 1 ;;
esac

render_services() {
  sed -e "s|\${ESHOP_REGISTRY}|$ESHOP_REGISTRY|g" -e "s|\${ESHOP_TAG}|$ESHOP_TAG|g" \
    "$HERE/services.yaml"
}

if [[ "${1:-}" == "--dry-run" ]]; then
  for file in infrastructure.yaml traffic.yaml; do
    kubectl apply --dry-run=server -f "$HERE/$file"
  done
  render_services | kubectl apply --dry-run=server -f -
  exit 0
fi

"$HERE/create-secrets.sh"
kubectl apply -f "$HERE/infrastructure.yaml"
render_services | kubectl apply -f -
kubectl apply -f "$HERE/traffic.yaml"
kubectl -n eshop get pods
