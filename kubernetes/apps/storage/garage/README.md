# Garage

Single-node Garage (`replication_factor = 1`) holding the restic repositories
for `templates/volsync` and the CNPG WAL archives.

| Path | Storage | Holds |
|---|---|---|
| `/data` | NFS `/volume1/k8s/garage/data` | object blocks |
| `/meta` | `garage-meta` ceph-block PVC | LMDB catalog, `node_key`, `cluster_layout` |
| `/metadata-snapshots` | NFS `/volume1/k8s/garage/metadata-snapshots` | Garage's own 6h LMDB snapshots (DB only, last 2) |

LMDB can't live on NFS, so `/meta` is on Ceph and needs its own backup. It
can't go into Garage, which is what it would be restoring.

## Metadata backup

`app/volsync.yaml` backs up the whole `garage-meta` PVC every 15 minutes to a
restic repository on the NAS (`/volume1/k8s/garage/meta-restic`), mounted
into the mover with `moverVolumes`. The NAS is the right place: the metadata is
useless without `/data`, which is already there.

The backup is a crash-consistent RBD snapshot. LMDB survives that because
`metadata_fsync = true`.

Restored metadata is up to 15 minutes older than `/data`. Objects written in
that window are lost. Anything deleted in that window was removed from
`/data` 10 minutes later (`BLOCK_GC_DELAY`), so a restic repository pruned
in that window can fail to restore. Restore that app from R2 instead.

## Restore

`garage-meta` has a `dataSourceRef` to `garage-meta-dst`, which restores the
latest NAS backup when it's created. On a rebuilt cluster Garage comes back
with its own node ID, layout, keys and buckets and no manual steps, and the
app PVCs and CNPG clusters then restore from it.

The `require-meta` initContainer stops Garage from starting if `/meta/node_key`
is missing. Without it, a restore that found no backup would start an empty
Garage, and the next 15-minute backup would store that empty state.

Like the app PVCs, `garage-meta-dst` only runs when its `trigger.manual` value
changes. On a running cluster its snapshot is from whenever it last ran, so
recreating `garage-meta` restores that old snapshot unless the destination is
re-triggered first.

### First-ever install

There is no backup to restore, so `require-meta` blocks the pod. Remove the
initContainer for one commit and let Garage generate its node key. Assign the
layout (`garage layout assign -z dc1 -c <capacity> <node-id>`, then
`garage layout apply --version 1`) and create the keys and buckets. Once one
backup of `garage-meta` has succeeded, restore the initContainer.

## Manual restic access

```sh
# on a host that mounts the NAS share
export RESTIC_REPOSITORY=/volume1/k8s/garage/meta-restic
# You probably want to write this to tmpfs
op read 'op://homelab-k8s/volsync-minio-template/RESTIC_PASSWORD' --out-file=restic_password

restic --password-file=restic_password snapshots
```
