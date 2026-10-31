# Setup Deploy Key

Generates an ed25519 deploy key for a private GitHub repo with `flux create
secret git --export`, stores it in 1Password as `deploy_key_<repo>`, and prints
the public key to add on GitHub. It refuses to run if an item with that title
already exists in the vault, and reads the key back after writing to check the
newlines survived.

The key never touches disk or a command line: it moves between flux, jq and op
through pipes. `KUBECONFIG=/dev/null` keeps flux away from the cluster.

## Usage

```bash
./setup-deploy-key.sh nickdavies/nick-assistant-pipelines
# -> 1Password item deploy_key_nick_assistant_pipelines in homelab-k8s
VAULT=other-vault ./setup-deploy-key.sh nickdavies/some-repo
```

Then reference the item from an ExternalSecret next to the GitRepository, as
in `kubernetes/flux/repositories/private/repos/`:

```yaml
  dataFrom:
    - extract:
        key: deploy_key_nick_assistant_pipelines
```

## Requirements

`flux`, `op` (signed in), `jq`, and `yq` (the jq wrapper, which outputs JSON by default).
