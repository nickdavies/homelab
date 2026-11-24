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

## Manual restic access

```sh
# on a host that mounts the NAS share
export RESTIC_REPOSITORY=/volume1/k8s/garage/meta-restic
# You probably want to write this to tmpfs
op read 'op://homelab-k8s/volsync-minio-template/RESTIC_PASSWORD' --out-file=restic_password

restic --password-file=restic_password snapshots
```
