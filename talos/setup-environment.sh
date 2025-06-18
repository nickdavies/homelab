#!/bin/bash

set -euo pipefail

OUTPUT_DIR="./output/"
MARKER_FILE="./$OUTPUT_DIR/.marker"

if [ -f "$MARKER_FILE" ]; then
    echo "Marker file exists in $OUTPUT_DIR, refusing to operate"
    exit 1
fi

sudo mount -o rw,nosuid,nodev,size=50M -t tmpfs none "$OUTPUT_DIR"
touch "$OUTPUT_DIR/.marker"

# We need this otherwise git status says we deleted it :'(
touch "$OUTPUT_DIR/.gitkeep"

mkdir "$OUTPUT_DIR/secrets"

op read 'op://Homelab/talos/secrets.yaml' --out-file "$OUTPUT_DIR/secrets/talos.yaml"
op read 'op://Homelab/talos/talosconfig' --out-file "$OUTPUT_DIR/secrets/talosconfig"
op read 'op://Homelab/talos/kubeconfig' --out-file "$OUTPUT_DIR/secrets/kubeconfig"

echo "Ready!"
