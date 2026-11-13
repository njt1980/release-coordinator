#!/usr/bin/env bash
# Create the eShop lab's PostgreSQL password Secret (eshop/eshop-postgres)
# if it doesn't exist. The password is random, generated here and sent
# straight to the cluster: never printed, never written to disk or Git.
# Idempotent: an existing Secret is kept (PostgreSQL keeps the password it
# was initialized with).
set -euo pipefail

NAMESPACE="${ESHOP_NAMESPACE:-eshop}"

kubectl get namespace "$NAMESPACE" > /dev/null 2>&1 \
  || kubectl create namespace "$NAMESPACE" > /dev/null

if kubectl -n "$NAMESPACE" get secret eshop-postgres > /dev/null 2>&1; then
  echo "secret $NAMESPACE/eshop-postgres exists; kept"
  exit 0
fi

password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
kubectl -n "$NAMESPACE" create secret generic eshop-postgres \
  --from-literal=password="$password" > /dev/null
unset password
echo "secret $NAMESPACE/eshop-postgres created"
