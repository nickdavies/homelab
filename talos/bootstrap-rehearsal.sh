#!/bin/bash

set -euo pipefail

# Rehearses bootstrap.sh on a throwaway kind cluster: does Flux alone, with no
# Talos bootstrap manifests, get from an empty cluster to applying cluster-apps?
#
# It runs the real bootstrap.sh with the real Flux deploy key (read-only) and
# dummy 1Password Connect credentials. The dummies are the safety: without them
# resolving, cluster-config-secret never becomes Ready, so cluster-apps (every
# app, including the ones that write to backups, DNS and the Cloudflare tunnel)
# never applies. Never give it the real Connect credentials.
#
# Checks:
#   1. flux-operator, the FluxInstance and the root sync come up;
#   2. prometheus-operator-crds is Ready, then external-secrets-operator, with
#      its ServiceMonitors created;
#   3. what cluster-apps itself applies passes a server-side dry-run, i.e. no
#      object in it lacks its CRD (the deadlock the first bootstrap hit).
#
# It syncs main, so run it after the change it tests is merged, before
# anything that depends on it (e.g. removing the Talos copies).
#
# Requires: kind, a container runtime, kubectl, flux, talos-session.
# Usage: ./bootstrap-rehearsal.sh            # create, check, keep the cluster
#        kind delete cluster --name homelab-rehearsal

cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
CLUSTER="${CLUSTER:-homelab-rehearsal}"
# From kind v0.33.0's release notes; its oldest image is 1.34.
NODE_IMAGE="${NODE_IMAGE:-kindest/node:v1.34.11@sha256:44e222ee2132dab25ff87301682f89eb82c7880ea3a1bf543bfe9708fd08d67d}"
OP_FLUX_KEY_REF="${OP_FLUX_KEY_REF:-op://Homelab/flux_deploy_key_v1/privatekey}"

if [ -z "${BOOTSTRAP_FLUX_KEY:-}" ]; then
    exec talos-session exec --secret "BOOTSTRAP_FLUX_KEY=$OP_FLUX_KEY_REF" -- "$0" "$@"
fi

work="$(mktemp -d /tmp/homelab-rehearsal.XXXXXX)"
trap 'rm -rf -- "/tmp/${work#/tmp/}"' EXIT
export KUBECONFIG="$work/kubeconfig"

if ! kind get clusters | grep -qx "$CLUSTER"; then
    kind create cluster --name "$CLUSTER" --image "$NODE_IMAGE" --kubeconfig "$KUBECONFIG"
else
    kind get kubeconfig --name "$CLUSTER" > "$KUBECONFIG"
fi
if [ "$(kubectl config current-context)" != "kind-$CLUSTER" ]; then
    echo "Refusing to run: kubeconfig context is not kind-$CLUSTER" >&2
    exit 1
fi

echo '{"dummy": "rehearsal"}' > "$work/op-creds.json"
echo "dummy-rehearsal-token" > "$work/op-token"

BOOTSTRAP_KUBECONFIG="$KUBECONFIG" \
BOOTSTRAP_OP_CREDS="$work/op-creds.json" \
BOOTSTRAP_OP_TOKEN="$work/op-token" \
    ./bootstrap.sh

ready() {
    kubectl -n flux-system wait "kustomization/$1" --for=condition=Ready --timeout "${2:-10m}"
}

echo "==> prometheus-operator-crds"
timeout 5m bash -c 'until kubectl -n flux-system get kustomization prometheus-operator-crds >/dev/null 2>&1; do sleep 5; done'
ready prometheus-operator-crds

echo "==> external-secrets-operator"
ready external-secrets-operator 15m
kubectl -n external-secrets wait helmrelease/external-secrets --for=condition=Ready --timeout 10m
monitors=$(kubectl -n external-secrets get servicemonitors --no-headers 2>/dev/null | wc -l)
echo "    ServiceMonitors in external-secrets: $monitors"
[ "$monitors" -gt 0 ] || { echo "FAIL: ESO installed without its ServiceMonitors" >&2; exit 1; }

echo "==> cluster-apps is blocked, as it should be with dummy secrets"
kubectl -n flux-system get kustomization cluster-apps -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}{"\n"}'

echo "==> server-side dry-run of what cluster-apps applies"
# --dry-run builds locally, leaving substitution variables as they are. Like
# kustomize-controller, apply the batch's own CRDs (MetalLB's) for real first,
# then dry-run the rest; only a missing CRD fails the rehearsal.
flux build kustomization cluster-apps --path "$REPO/kubernetes/apps" --dry-run \
    --kustomization-file "$REPO/kubernetes/flux/apps.yaml" > "$work/cluster-apps.yaml"
python3 -I - "$work" <<'EOF'
import sys, yaml
work = sys.argv[1]
docs = [d for d in yaml.safe_load_all(open(f"{work}/cluster-apps.yaml")) if d]
for name, keep in (("crds", True), ("rest", False)):
    picked = [d for d in docs if (d.get("kind") == "CustomResourceDefinition") == keep]
    yaml.safe_dump_all(picked, open(f"{work}/{name}.yaml", "w"))
EOF
apply_crds=$(kubectl apply --server-side --field-manager=rehearsal -f "$work/crds.yaml" | wc -l)
kubectl wait --for=condition=Established --timeout 2m -f "$work/crds.yaml" >/dev/null
echo "    applied $apply_crds CRDs from cluster-apps"
kubectl apply --server-side --dry-run=server --field-manager=rehearsal -f "$work/rest.yaml" \
    > "$work/dry-run.txt" 2>&1 || true
if grep -iE 'no matches for kind|ensure CRDs' "$work/dry-run.txt"; then
    echo "FAIL: cluster-apps applies objects whose CRDs don't exist yet" >&2
    exit 1
fi
echo "    $(grep -c 'serverside-applied' "$work/dry-run.txt") objects accepted; other dry-run errors (not CRD-related):"
grep -v 'serverside-applied' "$work/dry-run.txt" | sed 's/^/      /' || echo "      none"

echo
echo "PASS: Flux bootstraps from empty up to cluster-apps with no Talos manifests."
echo "Delete the cluster with: kind delete cluster --name $CLUSTER"
