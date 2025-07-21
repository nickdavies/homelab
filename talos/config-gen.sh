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
PATCHES_CONTROL_PLANE=""
if [ -d "./patches-control-plane" ]; then
    PATCHES_CONTROL_PLANE=$(find ./patches-control-plane | grep -e '.ya\?ml' | sed 's/^\.\///' | sort || true)
fi
PATCHES_WORKERS=""
if [ -d "./patches-workers" ]; then
    PATCHES_WORKERS=$(find ./patches-workers | grep -e '.ya\?ml' | sed 's/^\.\///' | sort || true)
fi
OUTPUT_PATCHES_DIR="./output/rendered/patches"
OUTPUT_PATCHES_CONTROL_PLANE_DIR="./output/rendered/patches-control-plane"
OUTPUT_PATCHES_WORKERS_DIR="./output/rendered/patches-workers"
OUTPUT_NODE_DIR="output/rendered/nodes/$NODE"

rm -rf "$OUTPUT_NODE_DIR"
mkdir -p "$OUTPUT_NODE_DIR"
mkdir -p "$OUTPUT_PATCHES_DIR"
mkdir -p "$OUTPUT_PATCHES_CONTROL_PLANE_DIR"
mkdir -p "$OUTPUT_PATCHES_WORKERS_DIR"

VALID_VARS=$(cat talosenv $NODE_ENV | grep -v "^\s*$" | sed 's/\([^=]*\)=.*/${\1}/')

patchesArray=()
for PATCH in $PATCHES; do
    mkdir -p "$OUTPUT_PATCHES_DIR/$(dirname $PATCH)"
    cat "$PATCH" | envsubst "$VALID_VARS" > "$OUTPUT_PATCHES_DIR/$PATCH"
    patchesArray+=(--config-patch @"$OUTPUT_PATCHES_DIR/$PATCH")
done

patchesControlPlaneArray=()
for PATCH in $PATCHES_CONTROL_PLANE; do
    mkdir -p "$OUTPUT_PATCHES_CONTROL_PLANE_DIR/$(dirname $PATCH)"
    cat "$PATCH" | envsubst "$VALID_VARS" > "$OUTPUT_PATCHES_CONTROL_PLANE_DIR/$PATCH"
    patchesControlPlaneArray+=(--config-patch-control-plane @"$OUTPUT_PATCHES_CONTROL_PLANE_DIR/$PATCH")
done

patchesWorkersArray=()
for PATCH in $PATCHES_WORKERS; do
    mkdir -p "$OUTPUT_PATCHES_WORKERS_DIR/$(dirname $PATCH)"
    cat "$PATCH" | envsubst "$VALID_VARS" > "$OUTPUT_PATCHES_WORKERS_DIR/$PATCH"
    patchesWorkersArray+=(--config-patch-worker @"$OUTPUT_PATCHES_WORKERS_DIR/$PATCH")
done

# Generate both controlplane and worker configs with all patches applied
talosctl gen config "$CLUSTER_NAME" "$CLUSTER_ENDPOINT" \
    --with-secrets "$SECRETS_DIR/talos.yaml" \
    --with-examples=false \
    --with-docs=false \
    --install-disk "" \
    --output-types="controlplane,worker" \
    --output "$OUTPUT_NODE_DIR" \
    --config-patch "$(cat $NODE_PATCH | envsubst "$VALID_VARS")" \
    "${patchesArray[@]}" \
    "${patchesControlPlaneArray[@]}" \
    "${patchesWorkersArray[@]}"
