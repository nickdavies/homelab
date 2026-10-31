#!/usr/bin/env bash
# Generate a Flux deploy key for a private GitHub repo and store it in 1Password
# as the item the deploy-key ExternalSecrets extract (identity, identity.pub,
# known_hosts), then print the public key to add on GitHub.
#
# Key material only ever lives in shell variables and pipes: flux prints it to
# stdout, and op reads the item JSON from stdin. Assignment-statement arguments
# (`identity=...`) would put the key in argv, visible to other processes, and
# hand-pasting into the 1Password UI is what flattened the newlines before.
set -euo pipefail

# flux falls back to applying the secret if --export is ever dropped; give it
# no cluster to reach.
export KUBECONFIG=/dev/null

usage() {
    echo "usage: $0 <owner>/<repo>" >&2
    echo "  creates 1Password item deploy_key_<repo> (dashes -> underscores)" >&2
    echo "  VAULT overrides the vault (default: homelab-k8s)" >&2
    exit 2
}

die() {
    echo "error: $*" >&2
    exit 1
}

[[ $# -eq 1 && $1 =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage
repo=$1
vault=${VAULT:-homelab-k8s}
name=${repo#*/}
name=${name,,}
title="deploy_key_${name//[^a-z0-9]/_}"
url="ssh://git@github.com/${repo}.git"

for cmd in flux op yq jq; do
    command -v "$cmd" >/dev/null || die "$cmd not found"
done

items=$(op item list --vault "$vault" --format json)
if printf '%s' "$items" | jq -e --arg t "$title" 'any(.[]; .title == $t)' >/dev/null; then
    die "an item named '$title' already exists in vault '$vault'; not touching it"
fi

secret=$(flux create secret git deploy-key --export --url="$url" --ssh-key-algorithm=ed25519 | yq .stringData)
identity=$(printf '%s' "$secret" | jq -r .identity)
pub=$(printf '%s' "$secret" | jq -r '.["identity.pub"]')

printf '%s' "$secret" | jq --arg title "$title" --arg url "$url" '{
    title: $title,
    category: "SECURE_NOTE",
    fields: [
        {label: "identity", type: "CONCEALED", value: .identity},
        {label: "identity.pub", type: "STRING", value: .["identity.pub"]},
        {label: "known_hosts", type: "STRING", value: .known_hosts},
        {label: "repo_url", type: "STRING", value: $url}
    ]
}' | op item create --vault "$vault" - >/dev/null

# Read the key back to prove 1Password kept the newlines.
stored=$(op item get "$title" --vault "$vault" --reveal --format json |
    jq -r '.fields[] | select(.label == "identity") | .value')
[[ $stored == "$identity" ]] ||
    die "'$title' was created but its identity doesn't match the generated key; delete it and rerun"

printf 'Created %s in vault %s.\n\n' "$title" "$vault"
printf 'Add as a deploy key (leave write access off) at https://github.com/%s/settings/keys/new\n\n' "$repo"
printf '%s\n' "$pub"
