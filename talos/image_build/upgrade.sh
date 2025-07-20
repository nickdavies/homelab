#!/bin/bash

set -euo pipefail

# Check if correct number of arguments provided
if [[ $# -ne 3 ]]; then
    echo "Usage: $0 <ID> <VERSION> <NODE_IP>"
    echo "  ID: Factory schematic ID"
    echo "  VERSION: Talos version"
    echo "  NODE_IP: IP address of the node to upgrade"
    exit 1
fi

ID="$1"
VERSION="$2"
NODE="$3"

# Validate ID is not empty
if [[ -z "$ID" ]]; then
    echo "Error: ID cannot be empty"
    exit 1
fi

# Validate VERSION is not empty
if [[ -z "$VERSION" ]]; then
    echo "Error: VERSION cannot be empty"
    exit 1
fi

# Basic IP address validation
if [[ ! "$NODE" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    echo "Error: NODE must be a valid IP address (e.g., 192.168.1.100)"
    exit 1
fi

talosctl upgrade --image "factory.talos.dev/metal-installer/${ID}:${VERSION}" --nodes "$NODE"
