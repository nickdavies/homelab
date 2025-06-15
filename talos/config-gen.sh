#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(dirname "$0")
cd $SCRIPT_DIR

set -a; source talosenv; set +a

NODE="$1"

NODE_PATCH="./nodes/$NODE.yaml"
SECRETS_DIR="./output/secrets"

if [ ! -f "$NODE_PATCH" ]; then
    echo "Node patch file $NODE_PATCH doesn't exist"
    exit 1
fi

PATCHES=$(find ./patches | grep -e '.ya\?ml' | sed 's/^\.\///' | sort)
OUTPUT_PATCHES_DIR="./output/rendered/patches"
OUTPUT_NODES_DIR="output/rendered/nodes"

rm -f "$OUTPUT_NODES_DIR/${NODE}.yaml"
mkdir -p "$OUTPUT_NODES_DIR"
mkdir -p "$OUTPUT_PATCHES_DIR"

patchesArray=()
for PATCH in $PATCHES; do
    mkdir -p "$OUTPUT_PATCHES_DIR/$(dirname $PATCH)"
    cat "$PATCH" | envsubst > "$OUTPUT_PATCHES_DIR/$PATCH"
    patchesArray+=(--config-patch @"$OUTPUT_PATCHES_DIR/$PATCH")
done

talosctl gen config "$CLUSTER_NAME" "$CLUSTER_ENDPOINT" \
    --with-secrets "$SECRETS_DIR/talos.yaml" \
    --with-examples=false \
    --with-docs=false \
    --install-disk "" \
    --output-types="controlplane" \
    --output "$OUTPUT_NODES_DIR/${NODE}.yaml" \
    "${patchesArray[@]}" \
    --config-patch "$(cat $NODE_PATCH | envsubst)"
