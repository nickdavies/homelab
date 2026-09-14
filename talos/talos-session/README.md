# talos-session

Mints short-lived, role-scoped Talos API credentials from the `os:admin`
talosconfig held in 1Password, so that credential never sits on disk.

## Why

Talos authenticates with mTLS and has no revocation: a leaked client certificate
is valid until it expires, and the only remedy is rotating the Talos CA across
every node. The `os:admin` talosconfig is also the credential that can do
everything — `apply-config`, `upgrade`, `reset`.

So keep exactly one copy of it, in 1Password, and derive a narrow, short-lived
certificate whenever one is actually needed.

## Install

```bash
make install    # cargo install --path .
```

Then point `TALOSCONFIG` at the well-known session path, permanently:

```bash
# ~/.zshrc
export TALOSCONFIG="$XDG_RUNTIME_DIR/talos-session/config"
```

That path lives under the per-user tmpfs systemd already mounts
`mode=700,uid=<you>,nosuid,nodev` and clears at logout — the same guarantee the
old `sudo mount -t tmpfs` dance was hand-rolling, without needing root.

## Where the node and endpoint come from

Nowhere local — they ride along in the 1Password item. `talosctl config new`
needs both an endpoint to talk to and exactly one node to sign the certificate,
and it reads them straight out of the talosconfig when they are set:

```yaml
context: homelab-cluster
contexts:
    homelab-cluster:
        endpoints:
            - 192.168.254.10      # the control-plane VIP
        nodes:
            - 192.168.254.10
        ca: ...
```

With `nodes:` present in `op://Homelab/talos/talosconfig`, every command below
runs with no flags at all. `-e/--endpoint` and `-n/--node` exist to override for
a one-off (talking to a single node while the VIP is down, say).

Note that the *minted* credential inherits endpoints but not nodes — Talos only
copies endpoints into the config it generates — so node-targeted commands still
take `-n`, exactly as ordinary `talosctl` usage does.

## Use

Two modes.

**Ambient session** — mints into the path `$TALOSCONFIG` already points at, so
plain `talosctl` works afterwards with no wrapper:

```bash
talos-session use reader
talosctl get members            # just works
```

**Scoped to one command** — exposes secrets to a single child process and
leaves your shell untouched:

```bash
# mint a certificate, expose it as $TALOSCONFIG
talos-session exec admin -- talosctl -n k8s-node-1 apply -f <rendered>

# expose a stored secret verbatim as $TALOS_SECRETS
talos-session exec --secret TALOS_SECRETS=op://Homelab/talos/secrets.yaml -- ./config-gen.sh k8s-node-1

# both at once; each lands on its own descriptor
talos-session exec admin --secret TALOS_SECRETS=op://Homelab/talos/secrets.yaml -- ./maintenance.sh
```

Minting and fetching are the same pipeline — *fetch from 1Password → optionally
derive → expose on a descriptor → exec* — so a `ROLE` and a `--secret` compose
freely. A `ROLE` adds the derive step and always lands on `TALOSCONFIG`; each
`--secret NAME=REF` is exposed verbatim as `$NAME`.

Plus `talos-session status` and `talos-session clear [role]`.

Aliases make `exec` bearable:

```bash
alias tse='talos-session exec'
tse admin -- talosctl -n k8s-node-1 dmesg
```

## Roles and lifetimes

| Role | TTL | Covers |
|---|---|---|
| `reader` | 30d | `talosctl get`, `logs`, `dmesg`, `containers`, `services`, `version`, `disks` |
| `operator` | 48h | + `reboot`, `shutdown`, `service restart`, `etcd snapshot` |
| `admin` | 2h | + `apply-config`, `upgrade`, `reset`, `bootstrap`, and reading file *contents* |

Two Talos quirks worth knowing:

- `Read` and `Copy` are admin-only. `reader` can *list* files but not read them,
  so `talosctl read /etc/...` needs the admin tier.
- `ApplyConfiguration` allows `os:reader` only for nodes in maintenance mode
  (the `--insecure` first-boot path). Applying to a running node is admin.

Talos enforces no server-side ceiling on `--crt-ttl` — the handler rejects only
a non-positive duration — so these TTLs are the whole policy. They are
hard-coded in `src/policy.rs` rather than exposed as a flag, so a typo cannot
mint a year-long admin certificate.

## How the admin credential is handled

`talosctl` only accepts its credential as a path, so the admin config has to be
addressable as a file. Instead of writing it out — even briefly, even on a
tmpfs — it is held in an anonymous `memfd` and passed to the child on fd 3,
which `talosctl` opens as `/dev/fd/3`. It exists only in that process tree's
memory and is reaped when `talosctl` exits.

Every escalation goes back to 1Password, because Talos gates
`GenerateClientConfiguration` behind `os:admin`: a reader or operator
certificate cannot mint anything, not even another copy of itself. That is the
point — it puts a biometric prompt and a 1Password audit entry in front of each
escalation.

Uncached `exec` runs hand the *minted* credential to the command the same way,
so an uncached run touches the filesystem only for the instant between
`talosctl config new` writing the file and this tool reading it back and
deleting it. One consequence: the command sees `TALOSCONFIG=/dev/fd/3`, which is
alive only for that process tree. `exec ... -- bash` therefore gives you a shell
whose Talos credential dies with it; use `use` or `--cache` if you want one that
outlives a single command.

Both the memfd and the session tmpfs are swap-backed, so neither is proof
against a credential reaching persistent storage on a machine that has swap
enabled. This one has none (`swapon --show` is empty, no zram), which is what
makes the guarantee hold. Turning on a swapfile would weaken it, and the fix
would be `mlock`-ing the memfd — which needs `RLIMIT_MEMLOCK` headroom and is
not done here.

## Caching

`exec` does not cache by default: it mints a fresh credential, hands it to the
command on a descriptor, and leaves nothing on disk. One `op` unlock per
command, which is the right trade when the command is a single `apply`.

`--cache` opts into reuse for a batch of work. It applies only to a minted
certificate — there is no derivation to amortise for a raw `--secret`, only the
fetch, and keeping a root secret on disk for an hour is the thing this tool
exists to avoid. Batch those by wrapping a whole script instead:

```bash
talos-session exec --secret TALOS_SECRETS=<ref> -- ./maintenance.sh   # one unlock
```

With `--cache`, the credential is written to the session tmpfs and reused while
it is still valid, with a five-minute margin so nothing dies mid-flight:

```bash
talos-session exec operator --cache -- talosctl -n k8s-node-1 reboot
talos-session exec operator --cache -- talosctl -n k8s-node-2 reboot   # no unlock
talos-session clear operator                                           # done
```

Reuse runs to the certificate's own expiry rather than some shorter window. A
shorter one would cost extra `op` round-trips without reducing exposure: the
certificate stays valid until it expires whether or not the file is still there,
and anyone who could read it off the 0700 tmpfs already has a copy. The
certificate TTL *is* the exposure window, so it is also the right cache
lifetime — which is why `admin` is capped at two hours.

## Break-glass

If 1Password is unavailable, nothing here works. That is deliberate — the
fallback is to pull the admin talosconfig by hand:

```bash
TALOSCONFIG=<(op read 'op://Homelab/talos/talosconfig') talosctl -n <node> ...
```
