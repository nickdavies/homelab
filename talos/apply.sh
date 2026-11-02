#!/bin/bash

# Apply a node's rendered config to that node. The file and the target are
# both derived from the one node name, so one node's config can't be sent to
# another: that cloned identities live and took down etcd, flannel and DNS
# on 2026-11-02. Dry-run unless --apply is given.

set -euo pipefail
cd "$(dirname "$0")"

usage() {
    echo "Usage: $(basename "$0") <node> [--apply]" >&2
    echo >&2
    echo "Dry-runs the config ./config-gen.sh rendered for <node> against <node>." >&2
    echo "With --apply, applies it." >&2
    exit 1
}

[ $# -ge 1 ] && [ $# -le 2 ] || usage
NODE="$1"
case "${2:-}" in
    "") DRY_RUN=(--dry-run) ;;
    --apply) DRY_RUN=() ;;
    *) usage ;;
esac

if [ ! -f "./nodes/$NODE.env" ]; then
    echo "Node vars file ./nodes/$NODE.env doesn't exist" >&2
    exit 1
fi
set -a; source "./nodes/$NODE.env"; set +a

# Same addressing as patches/interfaces.yaml. By IP, not name, so a broken
# cluster DNS can't send it elsewhere.
NODE_IP="192.168.254.${NODE_IP_SEGMENT}"
FILE="$XDG_RUNTIME_DIR/talos-render/nodes/$NODE/$NODE_TYPE.yaml"

if [ ! -f "$FILE" ]; then
    echo "No render at $FILE; run ./config-gen.sh $NODE first" >&2
    exit 1
fi
# The render must name this node (its HostnameConfig document), which also
# catches a stale or hand-copied file.
if ! grep -qx "hostname: $NODE" "$FILE"; then
    echo "$FILE doesn't set hostname $NODE; re-render it" >&2
    exit 1
fi

exec talos-session exec admin -- \
    talosctl apply -f "$FILE" -e "$NODE_IP" -n "$NODE_IP" "${DRY_RUN[@]}"
