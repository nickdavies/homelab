#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(dirname "$0")
cd $SCRIPT_DIR

NODE="$1"
CLUSTER_ENDPOINT="https://192.168.254.10:6443"
CLUSTER_NAME="homelab-cluster"

NODE_PATCH="./nodes/$NODE.yaml"
SECRETS_DIR="./secrets"

if [ ! -f "$NODE_PATCH" ]; then
    echo "Node patch file $NODE_PATCH doesn't exist"
    exit 1
fi

PATCHES=$(find ./patches | grep -e '.ya\?ml' | sed 's/^\.\///' | sort)

rm "./rendered/$NODE" -rf

patchesArray=()
for PATCH in $PATCHES; do
    patchesArray+=(--config-patch @"$PATCH")
done

talosctl gen config "$CLUSTER_NAME" "$CLUSTER_ENDPOINT" \
    --with-secrets "$SECRETS_DIR/talos.yaml" \
    --with-examples=false \
    --output "./rendered/$NODE" \
    "${patchesArray[@]}" \
    --config-patch "@$NODE_PATCH"
