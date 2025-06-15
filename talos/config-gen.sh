#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(dirname "$0")
cd $SCRIPT_DIR

set -a; source talosenv; set +a

NODE="$1"

NODE_PATCH="./nodes/$NODE.yaml"
SECRETS_DIR="./secrets"

if [ ! -f "$NODE_PATCH" ]; then
    echo "Node patch file $NODE_PATCH doesn't exist"
    exit 1
fi

PATCHES=$(find ./patches | grep -e '.ya\?ml' | sed 's/^\.\///' | sort)
OUTPUT_PATCHES_DIR="./rendered/patches/"

rm "./rendered/nodes/$NODE" -rf
mkdir -p "./rendered/nodes/"
mkdir -p "$OUTPUT_PATCHES_DIR"

patchesArray=()
for PATCH in $PATCHES; do
    mkdir -p "./rendered/patches/$(dirname $PATCH)"
    cat "$PATCH" | envsubst > "./rendered/patches/$PATCH"
    patchesArray+=(--config-patch @"rendered/patches/$PATCH")
done

talosctl gen config "$CLUSTER_NAME" "$CLUSTER_ENDPOINT" \
    --with-secrets "$SECRETS_DIR/talos.yaml" \
    --with-examples=false \
    --output "./rendered/nodes/$NODE" \
    "${patchesArray[@]}" \
    --config-patch "@$NODE_PATCH"
