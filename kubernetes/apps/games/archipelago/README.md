# Archipelago WebHost

Stock upstream WebHost (`ghcr.io/archipelagomw/archipelago`) hosting the rooms
listed in `app/config/rooms.yaml`, plus their web and sphere trackers.

- `archipelago.internal.<domain>`: full UI, behind Authelia.
- `archipelago.<public domain>`: trackers, room pages and patch downloads only
  (allowlist in `app/route.yaml`).
- Each room: its own hostname, through the Cloudflare tunnel. Players connect
  with `wss://<room hostname>:443`. The `:443` matters, because clients
  default to 38281 when no port is given.

`rooms.yaml` is the source of truth. On every pod start the `reconcile`
initContainer uploads any new seed, pins each room to its port, and keeps it
from ever sleeping. A room removed from the file is put to sleep, with its
save kept. Changing `rooms.yaml` rolls the pod, which restarts every room, so
players get disconnected briefly.

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

3. Add `app/rooms/<name>.yaml` and list it in `app/kustomization.yaml`. Use
   the same port as `targetPort`, and pick the hostname. For a LAN-only room
   use the `internal` gateway and an `*.internal.${SECRET_DOMAIN}` name.

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
       - backendRefs:
           - kind: Service
             name: archipelago-oneblock-nov
             port: 38281
   ```

4. The tracker URL is printed in the `reconcile` container's log (`kubectl
   logs archipelago-0 -c reconcile`) and is on the room page.

## Owning rooms

Every reconciled room belongs to the session id in 1Password item
`archipelago`, field `ROOM_OWNER`. Visit
`https://archipelago.internal.<domain>/session/<ROOM_OWNER>` once in a
browser to adopt it. That browser can then send room commands and read room
logs.

## Workarounds for WebHost 0.6.8

Both are fixed on the fork (`nickdavies/Archipelago` branches
`webhost-sigterm-save` and `webhost-respawn-hosters`). Delete each
workaround when its fix ships in an upstream release.

- `scripts/prestop.py`: WebHost ignores SIGTERM as PID 1, so stopping the
  pod would SIGKILL the rooms after the grace period and lose up to a minute
  of progress.
- `scripts/rooms_alive.py` as the liveness probe: a room hoster that dies is
  never restarted, so its rooms stay down while the UI looks healthy.
