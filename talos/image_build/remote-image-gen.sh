#!/bin/bash

set -euo pipefail

# Check if customizations.yaml exists
if [[ ! -f "customizations.yaml" ]]; then
    echo "Error: customizations.yaml file not found in current directory"
    exit 1
fi

curl -X POST --data-binary @customizations.yaml https://factory.talos.dev/schematics
