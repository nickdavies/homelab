#!/bin/bash

set -euo pipefail

OUTPUT_DIR="./output/"
SECRETS_DIR="./$OUTPUT_DIR/secrets/"
MARKER_FILE="./$OUTPUT_DIR/.marker"

set -a; source talosenv; set +a

OP_CREDENTIALS_FILE="$SECRETS_DIR/1password-credentials.json"
OP_TOKEN_FILE="$SECRETS_DIR/1password_access_token"
FLUX_KEY_FILE="$SECRETS_DIR/deploy_key.key"

if [ ! -f "$MARKER_FILE" ]; then
    echo "Secrets appear not to be mounted please run load-secrets.sh first"
    exit 1
fi

if [ ! -f "$OP_CREDENTIALS_FILE" ]; then
    echo "1password connect credentials expected at $OP_CREDENTIALS_FILE but was not found"
    exit 1
fi
if [ ! -f "$OP_TOKEN_FILE" ]; then
    echo "1password connect token expected at $OP_TOKEN_FILE but was not found"
    exit 1
fi

if [ ! -f "$FLUX_KEY_FILE" ]; then
    echo "Flux private key expected at $FLUX_KEY_FILE but was not found"
    exit 1
fi

if [ -z "$FLUX_REPO_URL" ]; then
    echo "FLUX_REPO_URL unset make sure this is configured in talosenv"
    exit 1
fi

OP_CREDENTIALS=$(cat $OP_CREDENTIALS_FILE | base64 | tr '/+' '_-' | tr -d '=' | tr -d '\n')
OP_TOKEN=$(cat $OP_TOKEN_FILE)

export KUBECONFIG="$SECRETS_DIR/kubeconfig"

echo "Waiting for kubeAPI to be up"
timeout 10m bash -c "until kubectl version >/dev/null 2>&1; do sleep 1; done"

echo "Waiting for external-secrets namespace to exist"
kubectl wait --for=create namespaces/external-secrets --timeout 10m

echo "Waiting for flux-system namespace to exist"
kubectl wait --for=create namespaces/flux-system --timeout 10m

flux create secret git homelab-auth \
    --export \
    --url "$FLUX_REPO_URL" \
    --private-key-file $FLUX_KEY_FILE \
    | kubectl apply -f -

kubectl create secret generic onepassword-connect-credentials \
    --from-literal="1password-credentials.json=$OP_CREDENTIALS" \
    -n external-secrets \
    --dry-run=client \
    -o yaml | kubectl apply -f -

kubectl create secret generic onepassword-connect-token \
    --from-literal="token=$OP_TOKEN" \
    -n external-secrets \
    --dry-run=client \
    -o yaml \
    | kubectl apply -f -
