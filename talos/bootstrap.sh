#!/bin/bash

set -euo pipefail

# Resolved before the cd below, so the re-exec further down can name this script.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"

set -a; source talosenv; set +a

# Turns a fresh cluster (Talos up, API reachable, nothing else) into one Flux
# manages: the bootstrap-only secrets, then CoreDNS, flux-operator, the
# FluxInstance and the root sync, each applied from this checkout's copy of
# what Flux manages from then on. Run it from a checkout of main. Safe to
# re-run.
#
# The secrets are needed exactly once per cluster rebuild, to give Flux and
# External Secrets enough to start reconciling; everything after that comes out
# of 1Password via the Connect operator.
#
# The kubeconfig here is the break-glass cluster-admin one rather than an OIDC
# login, because at this point in a rebuild there is no Authelia to log in to.
OP_KUBECONFIG_REF="${OP_KUBECONFIG_REF:-op://Homelab/talos/kubeconfig}"
OP_FLUX_KEY_REF="${OP_FLUX_KEY_REF:-op://Homelab/flux_deploy_key_v1/privatekey}"
OP_CONNECT_CREDS_REF="${OP_CONNECT_CREDS_REF:-op://Homelab/1password-connect/1password-credentials.json}"
OP_CONNECT_TOKEN_REF="${OP_CONNECT_TOKEN_REF:-op://Homelab/1password-connect/access_token}"
# CoreDNS's Corefile forwards internal.${SECRET_DOMAIN}; same ref as config-gen.sh.
CLUSTER_DOMAIN_REF="${CLUSTER_DOMAIN_REF:-op://homelab-k8s/cluster-config/DOMAIN}"

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
        --secret "BOOTSTRAP_CLUSTER_DOMAIN=$CLUSTER_DOMAIN_REF" \
        -- "$SELF" "$@"
fi

export KUBECONFIG="$BOOTSTRAP_KUBECONFIG"

# Server-side, and forcing: on a re-run Flux already owns these fields.
apply() {
    kubectl apply --server-side --force-conflicts --field-manager=bootstrap "$@"
}

echo "Waiting for kubeAPI to be up"
timeout 10m bash -c "until kubectl version >/dev/null 2>&1; do sleep 1; done"

# Nothing manages these two namespaces: Flux must never own (and so be able
# to prune) flux-system, and external-secrets holds the secrets below before
# Flux runs.
for ns in flux-system external-secrets; do
    kubectl create namespace "$ns" --dry-run=client -o yaml | apply -f -
done

flux create secret git homelab-auth \
    --export \
    --url "$FLUX_REPO_URL" \
    --private-key-file "$BOOTSTRAP_FLUX_KEY" \
    | apply -f -

kubectl create secret generic onepassword-connect-credentials \
    --from-file="1password-credentials.json=$BOOTSTRAP_OP_CREDS" \
    -n external-secrets \
    --dry-run=client \
    -o yaml | apply -f -

kubectl create secret generic onepassword-connect-token \
    --from-literal="token=$(cat "$BOOTSTRAP_OP_TOKEN")" \
    -n external-secrets \
    --dry-run=client \
    -o yaml \
    | apply -f -

# Talos doesn't deploy CoreDNS (cluster.coreDNS.disabled), and Flux needs DNS
# to fetch from git, so install the copy Flux manages before Flux.
echo "Installing CoreDNS"
kubectl kustomize "$REPO/kubernetes/apps/kube-system/coredns/app" \
    | SECRET_DOMAIN="$(cat "$BOOTSTRAP_CLUSTER_DOMAIN")" flux envsubst --strict \
    | apply -n kube-system -f -
kubectl -n kube-system rollout status deploy/coredns --timeout 5m

echo "Installing flux-operator"
apply -k "$REPO/kubernetes/apps/flux-system/flux-operator/app"
kubectl wait --for=condition=Established --timeout 5m crd/fluxinstances.fluxcd.controlplane.io
kubectl -n flux-system rollout status deploy/flux-operator --timeout 5m

echo "Installing Flux"
apply -n flux-system -k "$REPO/kubernetes/apps/flux-system/flux-instance/app"
kubectl -n flux-system wait fluxinstance/flux --for=condition=Ready --timeout 10m

echo "Syncing kubernetes/flux"
apply -f "$REPO/kubernetes/flux/sync.yaml"

echo "Flux is up. Watch it bring up the rest with: flux get kustomizations --watch"
