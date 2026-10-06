# Postgres offsite copy

An hourly CronJob mirrors the barman-cloud buckets (`cloudnative-pg`,
`cloudnative-pg-immich`) from garage into an rclone crypt remote on R2. CNPG
can only archive to one ObjectStore, so the clusters keep writing to garage
and this copies garage offsite.

Inside the crypt remote:

- `current/<bucket>/` — exact copy of the garage bucket
- `trash/<bucket>/` — files a sync deleted or overwrote, purged after 30 days

Alerts: `PostgresOffsiteStale`, `PostgresOffsiteNeverSucceeded`.

## Prerequisites

1Password item `cloudnative-pg-offsite` (fields documented in
`externalsecret.yaml`), plus `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in
`cloudflare-r2`, whose token must be allowed on `R2_BUCKET`.

The garage key is read-only so the job cannot damage the source:

```sh
kubectl -n storage exec deploy/garage -- /garage key create pg-offsite-reader
kubectl -n storage exec deploy/garage -- /garage bucket allow --read cloudnative-pg --key pg-offsite-reader
kubectl -n storage exec deploy/garage -- /garage bucket allow --read cloudnative-pg-immich --key pg-offsite-reader
```

## Restoring from offsite

Restore goes back through garage, so the clusters' normal bootstrap
(`bootstrap.recovery` from their ObjectStore) works unchanged.

1. Get garage running with a read-write key.
2. Write an `rclone.conf` from the 1Password values. The crypt settings must
   match `helmrelease.yaml`, and the passwords go in as stored (obscured):

   ```ini
   [r2]
   type = s3
   provider = Cloudflare
   endpoint = <R2_ENDPOINT>
   access_key_id = <AWS_ACCESS_KEY_ID>
   secret_access_key = <AWS_SECRET_ACCESS_KEY>

   [offsite]
   type = crypt
   remote = r2:<R2_BUCKET>
   filename_encryption = standard
   directory_name_encryption = true
   password = <CRYPT_PASSWORD_OBSCURED>
   password2 = <CRYPT_SALT_OBSCURED>

   [garage]
   type = s3
   provider = Other
   endpoint = http://localhost:3900  # e.g. kubectl -n storage port-forward svc/garage 3900
   region = us-east-1
   access_key_id = <read-write key>
   secret_access_key = <read-write key>
   ```

3. Copy each bucket back: `rclone copy offsite:current/cloudnative-pg garage:cloudnative-pg`
   (and the same for `cloudnative-pg-immich`). To undo a bad delete instead,
   copy the missing files from `offsite:trash/<bucket>/`.
