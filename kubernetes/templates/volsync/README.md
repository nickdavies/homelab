Once minio is up:
```
kubectl port-forward service/minio 9000:9000 -n storage
```

Then you can setup the required keys:
```
export AWS_ACCESS_KEY_ID=$(op read 'op://homelab-k8s/minio/MINIO_ROOT_USER')
export AWS_SECRET_ACCESS_KEY=$(op read 'op://homelab-k8s/minio/MINIO_ROOT_PASSWORD')

# You probably want to write this to tmpfs
op read 'op://homelab-k8s/minio/RESTIC_PASSWORD' --out-file=restic_password
```

Once you have that setup you can init with:
```
restic --password-file=restic_password init -r s3:http://localhost:9000/restic --from-password-file=restic_password
```
