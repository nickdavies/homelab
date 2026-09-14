#!/bin/bash

set -euo pipefail

# Resolved before the cd below, so the re-exec further down can name this script.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")"

# The Talos secrets bundle holds the cluster CA private keys. It is never staged
# on disk: talos-session exposes it on a file descriptor as $TALOS_SECRETS.
TALOS_SECRETS_REF="${TALOS_SECRETS_REF:-op://Homelab/talos/secrets.yaml}"

# The apiserver's OIDC issuer URL needs the internal domain, which is kept out
# of git like every other value in cluster-config. Fetched the same way and
# substituted into the patches as ${SECRET_DOMAIN}.
CLUSTER_DOMAIN_REF="${CLUSTER_DOMAIN_REF:-op://homelab-k8s/cluster-config/DOMAIN}"

usage() {
    echo "Usage: $(basename "$0") <node> [node...]" >&2
    echo >&2
    echo "Renders Talos machine config for each node into \$XDG_RUNTIME_DIR." >&2
    echo "Every node in one invocation shares a single 1Password unlock." >&2
    exit 1
}

[ $# -ge 1 ] || usage

set -a; source talosenv; set +a

# Validate every node before touching 1Password, so a typo costs no unlock.
for NODE in "$@"; do
    if [ ! -f "./nodes/$NODE.yaml" ]; then
        echo "Node patch file ./nodes/$NODE.yaml doesn't exist" >&2
        exit 1
    fi
    if [ ! -f "./nodes/$NODE.env" ]; then
        echo "Node vars file ./nodes/$NODE.env doesn't exist" >&2
        exit 1
    fi
done

# Fetch the bundle by re-running under talos-session, which holds it in memory
# and hands it over as $TALOS_SECRETS=/dev/fd/<n>. Node validation happens above
# so a typo costs no 1Password unlock. Skipped when already running under a
# wrapper, so a larger batch can cover this script with a single unlock:
#   talos-session exec --secret TALOS_SECRETS=<ref> -- ./maintenance.sh
if [ -z "${TALOS_SECRETS:-}" ]; then
    exec talos-session exec \
        --secret "TALOS_SECRETS=$TALOS_SECRETS_REF" \
        --secret "CLUSTER_DOMAIN_FILE=$CLUSTER_DOMAIN_REF" \
        -- "$SELF" "$@"
fi

# envsubst needs this as a variable, not a path.
export SECRET_DOMAIN
SECRET_DOMAIN="$(cat "$CLUSTER_DOMAIN_FILE")"

# Rendered control-plane configs embed the cluster CA private keys, so they are
# written to the per-user tmpfs — mode 0700, nosuid, nodev, cleared at logout —
# rather than into the checkout, where they would land on the SSD.
: "${XDG_RUNTIME_DIR:?must be set: rendered configs carry private keys and need a tmpfs}"
OUTPUT_DIR="$XDG_RUNTIME_DIR/talos-render"
mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

for NODE in "$@"; do
    # Subshell: each node sources its own env file, and those vars must not
    # leak into the next node's render.
    (
        set -a; source "./nodes/$NODE.env"; set +a

        if [ -z "${NODE_TYPE:-}" ]; then
            echo "Error: NODE_TYPE must be set in ./nodes/$NODE.env" >&2
            exit 1
        fi

        if [ "$NODE_TYPE" != "controlplane" ] && [ "$NODE_TYPE" != "worker" ]; then
            echo "Error: NODE_TYPE must be either 'controlplane' or 'worker', got: '$NODE_TYPE'" >&2
            exit 1
        fi

        NODE_DIR="$OUTPUT_DIR/nodes/$NODE"
        rm -rf "$NODE_DIR"
        mkdir -p "$NODE_DIR"

        VALID_VARS="$(cat talosenv "./nodes/$NODE.env" | grep -v "^\s*$" | sed 's/\([^=]*\)=.*/${\1}/') \${SECRET_DOMAIN}"

        # Patches interpolate per-node variables (PRIMARY_MAC and friends), so
        # they are rendered under the node's own directory rather than into a
        # shared one where the last node processed would win.
        patchArgs=()
        render_patches() {
            local src_dir="$1" flag="$2" patch rendered
            [ -d "$src_dir" ] || return 0
            while IFS= read -r patch; do
                rendered="$NODE_DIR/$patch"
                mkdir -p "$(dirname "$rendered")"
                envsubst "$VALID_VARS" < "$patch" > "$rendered"
                patchArgs+=("$flag" "@$rendered")
            done < <(find "$src_dir" \( -name '*.yaml' -o -name '*.yml' \) | sed 's|^\./||' | sort)
        }

        render_patches "./patches" --config-patch
        render_patches "./patches-control-plane" --config-patch-control-plane
        render_patches "./patches-workers" --config-patch-worker

        talosctl gen config "$CLUSTER_NAME" "$CLUSTER_ENDPOINT" \
            --with-secrets "$TALOS_SECRETS" \
            --with-examples=false \
            --with-docs=false \
            --install-disk "" \
            --talos-version="${TALOS_VERSION}" \
            --kubernetes-version="${KUBE_VERSION}" \
            --output-types="${NODE_TYPE}" \
            --output "$NODE_DIR/${NODE_TYPE}.yaml" \
            --config-patch "$(envsubst "$VALID_VARS" < "./nodes/$NODE.yaml")" \
            "${patchArgs[@]}"

        # Catch structural errors offline rather than at apply time against a
        # live node. Every node here is bare metal, the Pi included — its
        # differences are the sbc-raspberrypi overlay at image build time and
        # the disk selector in its node patch, neither of which changes the
        # validation mode.
        talosctl validate --config "$NODE_DIR/${NODE_TYPE}.yaml" --mode metal

        echo "  apply with: talos-session exec admin -- talosctl apply -f $NODE_DIR/${NODE_TYPE}.yaml -n $NODE"
    )
done
