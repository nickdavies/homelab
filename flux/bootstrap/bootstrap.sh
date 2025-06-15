
set -euo pipefail

KEYS_DIR="./secrets"
MARKER_FILE="./$KEYS_DIR/.marker"

KUBECONFIG="../../talos/output/secrets/kubeconfig"
PRIVATE_KEY="$KEYS_DIR/deploy_key.key"
GITHUB_USERNAME="$(cat $KEYS_DIR/github_username)"

if [ ! -f "$MARKER_FILE" ]; then
    echo "Secrets appear not to be mounted please run load-flux-key.sh first"
    exit 1
fi

if [ ! -f "$PRIVATE_KEY" ]; then
    echo "Private key expected at $PRIVATE_KEY but was not found"
    exit 1
fi

if [ ! -f "$KUBECONFIG" ]; then
    echo "Kubeconfig expected at $PRIVATE_KEY but was not found. Perhaps you need to run setup-environment.sh"
    exit 1
fi

if [ -z "$GITHUB_USERNAME" ]; then
    echo "Expected non-empty github username got: $GITHUB_USERNAME"
    exit 1
fi

flux bootstrap git \
    --url="ssh://git@github.com/$GITHUB_USERNAME/homelab.git" \
    --branch=main \
    --path=flux/clusters/homelab \
    --private-key-file="$PRIVATE_KEY" \
    --kubeconfig="$KUBECONFIG" \
    --silent # assumes the key is already setup it doesn't squash output
