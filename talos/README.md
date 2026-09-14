## Credentials

Nothing here stages secrets into the checkout. `talosctl` credentials come from
[`talos-session`](talos-session/), which mints short-lived role-scoped
certificates from the admin talosconfig in 1Password:

```
talos-session use reader          # ambient, 30 days
talos-session exec admin -- ...   # one command, 2 hours, nothing left on disk
```

`config-gen.sh` and `secrets-bootstrap.sh` use the same mechanism. Each re-runs
itself under `talos-session exec --secret ...`, which holds what they need in
memory and hands it over as `/dev/fd/<n>`, so both work standalone:

```
./config-gen.sh k8s-node-1          # unlocks 1Password itself
```

and both compose under a wrapper when you want one unlock to cover a batch:

```
talos-session exec --secret TALOS_SECRETS=op://Homelab/talos/secrets.yaml -- ./maintenance.sh
```

Nothing here stages a secret on any filesystem, so there is no cleanup to get
wrong and no `sudo` anywhere.

## Booting / Init

First boot the machine into maintenance mode (for details see the
[`image_build` README](image_build/README.md)), then generate a config:

```
./config-gen.sh k8s-node-1
```

Several nodes can be rendered in one invocation, sharing a single 1Password
unlock:

```
./config-gen.sh k8s-node-1 k8s-node-2 k8s-node-3
```

Output goes to `$XDG_RUNTIME_DIR/talos-render/nodes/<node>/<type>.yaml`, where
`<type>` is `controlplane` or `worker` depending on `NODE_TYPE` in the node's
`.env`. It is written there rather than into the checkout because a rendered
control-plane config embeds the cluster CA private keys.

The script prints the exact apply command for each node it renders. For a node
already in the cluster:

```
talos-session exec admin -- talosctl apply -f <rendered> -n k8s-node-1
```

For a node in maintenance mode, which has no credential to authenticate
against yet:

```
talosctl apply -f <rendered> -n <NODE-IP> --insecure
```
