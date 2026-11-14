#!/usr/bin/env bash
# Deploy the probe (namespace appinsights-probe). Idempotent; run azure.sh and
# create-secrets.sh first. Pass --dry-run for a server-side dry run.
#
#   PROBE_REGISTRY=<registry host> PROBE_TAG=<tag> ./apply.sh [--dry-run]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
: "${PROBE_REGISTRY:?set PROBE_REGISTRY}"
: "${PROBE_TAG:?set PROBE_TAG}"
case "$PROBE_REGISTRY$PROBE_TAG" in
  *'|'* | *'&'* | *'\'*) echo "PROBE_REGISTRY/PROBE_TAG contain an unsupported character" >&2; exit 1 ;;
esac
dry_run=()
[[ "${1:-}" == "--dry-run" ]] && dry_run=(--dry-run=server)
sed -e "s|\${PROBE_REGISTRY}|$PROBE_REGISTRY|g" -e "s|\${PROBE_TAG}|$PROBE_TAG|g" "$HERE/manifests.yaml" \
  | kubectl apply "${dry_run[@]}" -f -
