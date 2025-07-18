#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(dirname "$0")
cd $SCRIPT_DIR

set -a; source talosenv; set +a

NODE="$1"

NODE_PATCH="./nodes/$NODE.yaml"
NODE_ENV="./nodes/$NODE.env"
SECRETS_DIR="./output/secrets"

if [ ! -f "$NODE_PATCH" ]; then
    echo "Node patch file $NODE_PATCH doesn't exist"
    exit 1
fi

if [ ! -f "$NODE_ENV" ]; then
    echo "Node vars file $NODE_ENV doesn't exist"
    exit 1
fi

set -a; source $NODE_ENV; set +a

PATCHES=$(find ./patches | grep -e '.ya\?ml' | sed 's/^\.\///' | sort)
OUTPUT_PATCHES_DIR="./output/rendered/patches"
OUTPUT_NODES_DIR="output/rendered/nodes"

rm -f "$OUTPUT_NODES_DIR/${NODE}.raw.yaml"
rm -f "$OUTPUT_NODES_DIR/${NODE}.yaml"
mkdir -p "$OUTPUT_NODES_DIR"
mkdir -p "$OUTPUT_PATCHES_DIR"

VALID_VARS=$(cat talosenv $NODE_ENV | grep -v "^\s*$" | sed 's/\([^=]*\)=.*/${\1}/')

patchesArray=()
for PATCH in $PATCHES; do
    mkdir -p "$OUTPUT_PATCHES_DIR/$(dirname $PATCH)"
    cat "$PATCH" | envsubst "$VALID_VARS" > "$OUTPUT_PATCHES_DIR/$PATCH"
    patchesArray+=(--patch @"$OUTPUT_PATCHES_DIR/$PATCH")
done

# First generate a base config without any of the patches.
# This lets us scope down to only generating the controlplane at this point
# without having talosctl gen try and generate both (which may be incompatible with
# certain patches)
talosctl gen config "$CLUSTER_NAME" "$CLUSTER_ENDPOINT" \
    --with-secrets "$SECRETS_DIR/talos.yaml" \
    --with-examples=false \
    --with-docs=false \
    --install-disk "" \
    --output-types="controlplane" \
    --output "$OUTPUT_NODES_DIR/${NODE}.raw.yaml" \

# Next we apply all our patches on top to make the final config
talosctl machineconfig patch \
    "$OUTPUT_NODES_DIR/${NODE}.raw.yaml" \
    -o "$OUTPUT_NODES_DIR/${NODE}.yaml" \
    --patch "$(cat $NODE_PATCH | envsubst "$VALID_VARS")" \
    "${patchesArray[@]}"
