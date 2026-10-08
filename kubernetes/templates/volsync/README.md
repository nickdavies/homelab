# VolSync backup template

Backs up one PVC to two restic repositories and restores it on creation.

| File | Creates |
|---|---|
| `claim.yaml` | PVC `${APP}`, populated from `${APP}-dst` via `dataSourceRef` |
| `garage.yaml` | Hourly backup to in-cluster Garage (`s3:http://garage.storage.svc.cluster.local:3900/restic/${APP}`), plus the `${APP}-dst` ReplicationDestination that restores from it |
| `r2.yaml` | Daily offsite backup to Cloudflare R2 (`<REPOSITORY_TEMPLATE>/${APP}`) |

Each app gets its own restic repository per backend. VolSync initialises an
empty repository on first backup, so a new app needs no manual setup.

## Using it

Add the template to the app's `kustomization.yaml`:

```yaml
resources:
  - ../../../../templates/volsync
```

and in its Flux `ks.yaml`:

```yaml
spec:
  dependsOn:
    - name: volsync # the PVC's dataSourceRef needs the VolSync CRDs
  postBuild:
    substitute:
      APP: <pvc name>
      VOLSYNC_CAPACITY: 5Gi
```

Optional overrides: `VOLSYNC_SCHEDULE`, `VOLSYNC_R2_SCHEDULE`,
`VOLSYNC_STORAGECLASS`, `VOLSYNC_ACCESSMODES`, `VOLSYNC_SNAPSHOTCLASS`,
`VOLSYNC_COPYMETHOD`, `VOLSYNC_CACHE_CAPACITY`, `VOLSYNC_CACHE_SNAPSHOTCLASS`,
`VOLSYNC_CACHE_ACCESSMODES`. Defaults are inline in the templates.

## Restoring

A fresh PVC is populated from `${APP}-dst`'s latest restored snapshot. That
ReplicationDestination only runs when its `trigger.manual` value changes, so
once the app exists its snapshot is from whenever it last ran, **not** the
latest backup. To restore the latest backup into a running app:

```sh
flux suspend kustomization <app>
kubectl -n <ns> scale <deployment|statefulset>/<app> --replicas=0

# Re-run the restore and wait until status.lastManualSync matches
kubectl -n <ns> patch replicationdestination <APP>-dst --type merge \
  -p '{"spec":{"trigger":{"manual":"restore-'"$(date +%s)"'"}}}'
kubectl -n <ns> get replicationdestination <APP>-dst -w

kubectl -n <ns> delete pvc <APP>
flux resume kustomization <app> # recreates the PVC from the new snapshot
```

To restore from R2 instead, also patch `spec.restic.repository` on
`<APP>-dst` to `<APP>-volsync-r2-secret` before triggering. Flux reverts both
patches on resume, which triggers one more (harmless) restore run.

## Manual restic access

```sh
kubectl -n storage port-forward svc/garage 3900:3900

export AWS_ACCESS_KEY_ID=$(op read 'op://homelab-k8s/garage/GARAGE_ACCESS_KEY_ID')
export AWS_SECRET_ACCESS_KEY=$(op read 'op://homelab-k8s/garage/GARAGE_SECRET_ACCESS_KEY')
export RESTIC_REPOSITORY=s3:http://localhost:3900/restic/<APP>
# You probably want to write this to tmpfs
op read 'op://homelab-k8s/volsync-minio-template/RESTIC_PASSWORD' --out-file=restic_password

restic --password-file=restic_password snapshots
```

For R2, the same four values come from the `cloudflare-r2` and
`volsync-r2-template` items (see `r2.yaml`); the repository is
`REPOSITORY_TEMPLATE/<APP>`.
