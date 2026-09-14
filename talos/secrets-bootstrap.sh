#!/bin/bash

set -euo pipefail

# Resolved before the cd below, so the re-exec further down can name this script.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")"

set -a; source talosenv; set +a

# Bootstrap-only secrets. These are needed exactly once per cluster rebuild, to
# give Flux and External Secrets enough to start reconciling; everything after
# that comes out of 1Password via the Connect operator.
#
# The kubeconfig here is the break-glass cluster-admin one rather than an OIDC
# login, because at this point in a rebuild there is no Authelia to log in to.
OP_KUBECONFIG_REF="${OP_KUBECONFIG_REF:-op://Homelab/talos/kubeconfig}"
OP_FLUX_KEY_REF="${OP_FLUX_KEY_REF:-op://Homelab/flux_deploy_key_v1/privatekey}"
OP_CONNECT_CREDS_REF="${OP_CONNECT_CREDS_REF:-op://Homelab/1password-connect/1password-credentials.json}"
OP_CONNECT_TOKEN_REF="${OP_CONNECT_TOKEN_REF:-op://Homelab/1password-connect/access_token}"

if [ -z "${FLUX_REPO_URL:-}" ]; then
    echo "FLUX_REPO_URL unset, make sure this is configured in talosenv" >&2
    exit 1
fi

# kubectl --from-file, flux --private-key-file and KUBECONFIG all take paths, so
# each secret is handed over as /dev/fd/<n> by talos-session and never written
# down. Skipped when already running under a wrapper.
if [ -z "${BOOTSTRAP_KUBECONFIG:-}" ]; then
    exec talos-session exec \
        --secret "BOOTSTRAP_KUBECONFIG=$OP_KUBECONFIG_REF" \
        --secret "BOOTSTRAP_FLUX_KEY=$OP_FLUX_KEY_REF" \
        --secret "BOOTSTRAP_OP_CREDS=$OP_CONNECT_CREDS_REF" \
        --secret "BOOTSTRAP_OP_TOKEN=$OP_CONNECT_TOKEN_REF" \
        -- "$SELF" "$@"
fi

export KUBECONFIG="$BOOTSTRAP_KUBECONFIG"

echo "Waiting for kubeAPI to be up"
timeout 10m bash -c "until kubectl version >/dev/null 2>&1; do sleep 1; done"

echo "Waiting for external-secrets namespace to exist"
kubectl wait --for=create namespaces/external-secrets --timeout 10m

echo "Waiting for flux-system namespace to exist"
kubectl wait --for=create namespaces/flux-system --timeout 10m

flux create secret git homelab-auth \
    --export \
    --url "$FLUX_REPO_URL" \
    --private-key-file "$BOOTSTRAP_FLUX_KEY" \
    | kubectl apply -f -

kubectl create secret generic onepassword-connect-credentials \
    --from-file="1password-credentials.json=$BOOTSTRAP_OP_CREDS" \
    -n external-secrets \
    --dry-run=client \
    -o yaml | kubectl apply -f -

kubectl create secret generic onepassword-connect-token \
    --from-literal="token=$(cat "$BOOTSTRAP_OP_TOKEN")" \
    -n external-secrets \
    --dry-run=client \
    -o yaml \
    | kubectl apply -f -
