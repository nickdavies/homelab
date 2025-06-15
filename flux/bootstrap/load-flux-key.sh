#!/bin/bash

set -euo pipefail

KEYS_DIR="./secrets"
MARKER_FILE="./$KEYS_DIR/.marker"

if [ -f "$MARKER_FILE" ]; then
    echo "Marker file exists in $KEYS_DIR, refusing to operate"
    exit 1
fi

sudo mount -o rw,nosuid,nodev,size=50M -t tmpfs none "$KEYS_DIR"
touch "$KEYS_DIR/.marker"

# We need this otherwise git status says we deleted it :'(
touch "$KEYS_DIR/.gitkeep"

op read 'op://Homelab/flux_deploy_key_v1/privatekey' --out-file "$KEYS_DIR/deploy_key.key"
op read 'op://Homelab/flux_deploy_key_v1/publickey' --out-file "$KEYS_DIR/deploy_key.pub"
op read 'op://Homelab/flux_deploy_key_v1/github_username' --out-file "$KEYS_DIR/github_username"

echo "Ready!"
