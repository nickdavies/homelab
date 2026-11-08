# Archipelago apworlds

Publishes the apworlds for the WebHost's options creator
(`kubernetes/apps/games/archipelago`). It reads them from a local Archipelago
instance where you install them with the APWorld Manager, and writes them,
plus a manifest of their sha256s, to the `archipelago/apworlds` share. The
pod only installs files that match that manifest.

## The local instance

`~/workspaces/homelab/apworld-manager`: upstream Archipelago at the server's
version (currently 0.6.8), with the
[APWorld Manager](https://github.com/silasary/Archipelago/releases?q=Manager)
in its `custom_worlds`. Its `custom_worlds` is the server's apworld set, so
keep only the games you want there.

```sh
cd ~/workspaces/homelab/apworld-manager
.venv/bin/python Launcher.py   # then "Install/Update Apworlds"
```

When the server image moves to a new Archipelago version, move this instance
too (`git fetch --depth 1 origin tag <version> && git checkout <version>`,
then `.venv/bin/python ModuleUpdate.py -y`). The tool refuses to run while
the two differ.

## Publishing

```sh
tools/archipelago-apworlds/sync_apworlds.py --dry-run   # see what changes
tools/archipelago-apworlds/sync_apworlds.py --restart   # publish, then restart the pod
```

Each apworld is matched by hash against the
[APWorld index](https://github.com/silasary/apworlds) to record where it was
published. A file not in the index is still published, flagged
`NOT IN THE INDEX`. An apworld whose `minimum_ap_version` /
`maximum_ap_version` excludes the server's version is refused.

The defaults assume the share is mounted at `~/mnt/mount1` (`/volume1/k8s`).
`--worlds-dir`, `--dest` and `--index` override them.
