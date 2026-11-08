# Archipelago WebHost

Stock upstream WebHost (`ghcr.io/archipelagomw/archipelago`) hosting the rooms
listed in `app/config/rooms.yaml`, plus their web and sphere trackers.

- `ap.<public domain>` and `archipelago.<public domain>`: the primary room.
  - Players connect with `wss://ap.<public domain>:443`. The `:443` matters,
    because clients default to 38281 when no port is given.
  - `/` redirects to its room page, which links its trackers and patch
    downloads.
  - The trackers, room pages and patch downloads of every room are reachable
    here too (allowlist in `app/route.yaml`), with cookies stripped.
- Any other room that should be playable at the same time gets its own
  hostname (see Extra rooms).
- Admin tasks: `kubectl port-forward` (see Owning rooms).

`rooms.yaml` is the source of truth for what runs. On every pod start the
`reconcile` initContainer uploads any new seed, pins each room to its port,
and keeps it from ever sleeping. A room removed from the file is put to
sleep, with its save kept. Changing `rooms.yaml` rolls the pod, which
restarts every room, so players get disconnected briefly.

## Adding a room

1. Generate the seed locally and copy the zip to the NAS under
   `/volume1/k8s/archipelago/seeds/`. Seeds are immutable: reconcile refuses
   to start if a room's zip changes. A new zip needs a new room name.
2. Add it to `app/config/rooms.yaml` with an unused port in 38281-38299:

   ```yaml
   rooms:
     oneblock-nov:
       zip: oneblock-nov/AP_12345.zip
       port: 38281
   ```

3. Make it the primary room, or give it its own hostname, or neither (it runs
   but isn't reachable from outside).

The room and tracker paths are printed in the `reconcile` container's log
(`kubectl logs archipelago-0 -c reconcile`).

## Switching the primary room

In `app/route.yaml`, change the two values marked `PRIMARY`:

- the `archipelago-primary` Service's `targetPort`: the room's port;
- the `/` redirect's `replaceFullPath`: `/room/<id>`. The id comes from the
  room's name, so it can be set in the same change as adding the room:

  ```sh
  python3 -c "import uuid,base64,sys; print(base64.urlsafe_b64encode(uuid.uuid5(uuid.UUID('6b1f2c1e-5c0a-4a52-9b8e-0f0d6a3e8a11'), 'room:' + sys.argv[1]).bytes).rstrip(b'=').decode())" oneblock-nov
  ```

  It is also in the reconcile log once the room exists.

The previous primary keeps running on its port. Remove it from `rooms.yaml`
to put it to sleep, or give it its own hostname to keep it playable.

## Extra rooms

Add `app/rooms/<name>.yaml` and list it in `app/kustomization.yaml`. Use
the same port as `targetPort`, and pick the hostname. Players connect with
`wss://<hostname>:443`; its room and tracker pages are on
`ap.<public domain>` like every room's. For a LAN-only room use the
`internal` gateway and an `*.internal.${SECRET_DOMAIN}` name. Keep the
cookie filters either way (`app/route.yaml` says why).

```yaml
---
apiVersion: v1
kind: Service
metadata:
  name: archipelago-oneblock-nov
spec:
  selector:
    app: archipelago
  ports:
    - name: game
      port: 38281
      targetPort: 38281
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: archipelago-oneblock-nov
spec:
  hostnames:
    - "ap-oneblock.${SECRET_PUBLIC_DOMAIN}"
  parentRefs:
    - name: external
      namespace: network
  rules:
    - filters:
        - type: RequestHeaderModifier
          requestHeaderModifier:
            remove: [Cookie]
        - type: ResponseHeaderModifier
          responseHeaderModifier:
            remove: [Set-Cookie]
      backendRefs:
        - kind: Service
          name: archipelago-oneblock-nov
          port: 38281
```

A new hostname fails DNS for about a minute after the change applies, until
external-dns creates its record.

## Owning rooms

Every reconciled room belongs to the session id in 1Password item
`archipelago`, field `ROOM_OWNER`. To send room commands or read room logs:

```sh
kubectl -n games port-forward archipelago-0 8080
```

Then open `http://localhost:8080/session/<ROOM_OWNER>` once, and use the
room pages on `localhost:8080`. If the seed was generated with a server
password, `!admin` from any client works without any of this.

## Workarounds for WebHost 0.6.8

Both are fixed on the fork (`nickdavies/Archipelago` branches
`webhost-sigterm-save` and `webhost-respawn-hosters`). Delete each
workaround when its fix ships in an upstream release.

- `scripts/prestop.py`: WebHost ignores SIGTERM as PID 1, so stopping the
  pod would SIGKILL the rooms after the grace period and lose up to a minute
  of progress.
- `scripts/rooms_alive.py` as the liveness probe: a room hoster that dies is
  never restarted, so its rooms stay down while the UI looks healthy.
