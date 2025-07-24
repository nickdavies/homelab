#!/bin/bash

set -euo pipefail

FILE="$1"
VERSION="${2:-<VERSION>}"

# Check if customizations.yaml exists
if [[ ! -f "$FILE" ]]; then
    echo "Error: $FILE file not found in current directory"
    exit 1
fi

OUTPUT=$(curl -X POST --data-binary @${FILE} https://factory.talos.dev/schematics)
echo $OUTPUT

ID="$(echo $OUTPUT | yq '.id' -r)"

echo "https://factory.talos.dev/image/${ID}/${VERSION}/metal-amd64.iso"
echo "https://factory.talos.dev/image/${ID}/${VERSION}/metal-arm64.raw.xz"
