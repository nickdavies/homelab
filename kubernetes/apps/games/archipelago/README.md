# Archipelago WebHost

Stock upstream WebHost (`ghcr.io/archipelagomw/archipelago`) hosting the rooms
listed in `app/config/rooms.yaml`, plus their web and sphere trackers.

- `archipelago.<public domain>`: trackers, room pages and patch downloads only
  (allowlist in `app/route.yaml`), with cookies stripped.
- No full-UI route. Anything behind Authelia would hand this pod your
  Authelia session cookie, and it runs internet-facing game servers. Use
  `kubectl port-forward` for the rare admin task (see Owning rooms).
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
   use the `internal` gateway and an `*.internal.${SECRET_DOMAIN}` name. Keep
   the cookie filters either way. A browser-based client's websocket
   handshake carries Authelia's cookie for that domain.

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

4. The room and tracker paths are printed in the `reconcile` container's log
   (`kubectl logs archipelago-0 -c reconcile`). Prefix them with
   `https://archipelago.<public domain>`.

## Owning rooms

Every reconciled room belongs to the session id in 1Password item
`archipelago`, field `ROOM_OWNER`. To send room commands or read room logs:

```sh
kubectl -n games port-forward archipelago-0 8080
```

Then open `http://localhost:8080/session/<ROOM_OWNER>` once, and use the
room pages on `localhost:8080`. localhost never receives an Authelia cookie.
In-game, `!admin` with the seed's server password works without any of this.

## Workarounds for WebHost 0.6.8

Both are fixed on the fork (`nickdavies/Archipelago` branches
`webhost-sigterm-save` and `webhost-respawn-hosters`). Delete each
workaround when its fix ships in an upstream release.

- `scripts/prestop.py`: WebHost ignores SIGTERM as PID 1, so stopping the
  pod would SIGKILL the rooms after the grace period and lose up to a minute
  of progress.
- `scripts/rooms_alive.py` as the liveness probe: a room hoster that dies is
  never restarted, so its rooms stay down while the UI looks healthy.
