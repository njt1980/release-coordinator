#!/usr/bin/env bash
# Create the eShop lab's Secrets in namespace eshop, if they don't exist:
#
#   eshop-postgres  the PostgreSQL password (random).
#   eshop-tls       a lab CA (ca.crt) and a certificate signed by it
#                   (tls.crt, tls.key) for the services the web app calls
#                   over HTTPS (catalog-api, ordering-api; see services.yaml).
#
# Everything is generated here and sent straight to the cluster: never
# printed, never kept on disk (a private temporary directory is removed on
# exit), never in Git. Idempotent: existing Secrets are kept (PostgreSQL
# keeps the password it was initialized with).
set -euo pipefail

NAMESPACE="${ESHOP_NAMESPACE:-eshop}"
TLS_SERVICES=(catalog-api ordering-api)

kubectl get namespace "$NAMESPACE" > /dev/null 2>&1 \
  || kubectl create namespace "$NAMESPACE" > /dev/null

if kubectl -n "$NAMESPACE" get secret eshop-postgres > /dev/null 2>&1; then
  echo "secret $NAMESPACE/eshop-postgres exists; kept"
else
  password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  kubectl -n "$NAMESPACE" create secret generic eshop-postgres \
    --from-literal=password="$password" > /dev/null
  unset password
  echo "secret $NAMESPACE/eshop-postgres created"
fi

if kubectl -n "$NAMESPACE" get secret eshop-tls > /dev/null 2>&1; then
  echo "secret $NAMESPACE/eshop-tls exists; kept"
  exit 0
fi
work="$(mktemp -d)"
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT
sans=""
for service in "${TLS_SERVICES[@]}"; do
  sans+="DNS:$service,DNS:$service.$NAMESPACE,DNS:$service.$NAMESPACE.svc,DNS:$service.$NAMESPACE.svc.cluster.local,"
done
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=eshop-lab-ca" \
  -keyout "$work/ca.key" -out "$work/ca.crt" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" 2> /dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=eshop-lab" \
  -keyout "$work/tls.key" -out "$work/tls.csr" 2> /dev/null
printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\n' "${sans%,}" > "$work/ext.cnf"
openssl x509 -req -in "$work/tls.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" \
  -CAcreateserial -days 365 -extfile "$work/ext.cnf" -out "$work/tls.crt" 2> /dev/null
kubectl -n "$NAMESPACE" create secret generic eshop-tls \
  --from-file=ca.crt="$work/ca.crt" --from-file=tls.crt="$work/tls.crt" \
  --from-file=tls.key="$work/tls.key" > /dev/null
echo "secret $NAMESPACE/eshop-tls created"
