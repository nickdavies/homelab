# Archipelago WebHost

Stock upstream WebHost (`ghcr.io/archipelagomw/archipelago`) hosting the rooms
listed in `app/config/rooms.yaml`, plus their web and sphere trackers.

- **Playing:** `ap.<public domain>:<room port>`. The room on 38281 needs no
  port at all, because clients default to 38281. Players can also click
  their name on the room page, which opens the Archipelago Launcher already
  connected.
  - The address resolves straight to the home IP (not through Cloudflare).
    The router forwards 38281-38290 to `192.168.254.203`, the
    `archipelago-game` LoadBalancer.
  - On the LAN, blocky resolves it to that IP directly.
  - Rooms speak TLS with a Let's Encrypt cert. Clients handle that whether or
    not players type `wss://`.
- **Web pages:** `ap-tracker.<public domain>`, through the Cloudflare tunnel.
  - `/` redirects to the primary room's page, which links its trackers and
    patch downloads.
  - Every room's tracker and room pages are reachable (allowlist in
    `app/route.yaml`), with cookies stripped.
  - `/games` is the options creator, for every built-in game plus the
    apworlds you install (see Apworlds). It only downloads YAMLs:
    `MAX_ROLL: 0` refuses generation, and `scripts/webhost.py` removes the
    "Generate Single-Player Game" button.
- **Admin tasks:** `kubectl port-forward` (see Owning rooms).

`rooms.yaml` is the source of truth for what runs. On every pod start the
`reconcile` initContainer uploads any new seed, pins each room to its port,
and keeps it from ever sleeping. A room removed from the file is put to
sleep, with its save kept. Changing `rooms.yaml` rolls the pod, which
restarts every room, so players get disconnected briefly.

## Adding a room

1. Generate the seed locally and copy the zip to the NAS under
   `/volume1/k8s/archipelago/seeds/`. Seeds are immutable: reconcile refuses
   to start if a room's zip changes. A new zip needs a new room name.
2. Add it to `app/config/rooms.yaml` with an unused port in 38281-38290.
   Use 38281 for the main game, so players can leave the port off:

   ```yaml
   rooms:
     oneblock-nov:
       zip: oneblock-nov/AP_12345.zip
       port: 38281
   ```

That's all a room needs to be playable. The room and tracker paths are
printed in the `reconcile` container's log
(`kubectl logs archipelago-0 -c reconcile`).

## Switching the primary room

The primary room is only where `ap-tracker.<public domain>/` redirects. In
`app/route.yaml`, set the redirect's `replaceFullPath` (marked `PRIMARY`) to
`/room/<id>`. The id comes from the room's name, so it can go in the same
change as adding the room:

```sh
python3 -c "import uuid,base64,sys; print(base64.urlsafe_b64encode(uuid.uuid5(uuid.UUID('6b1f2c1e-5c0a-4a52-9b8e-0f0d6a3e8a11'), 'room:' + sys.argv[1]).bytes).rstrip(b'=').decode())" oneblock-nov
```

It is also in the reconcile log once the room exists.

## Apworlds

Custom games for the options creator. Rooms don't need them, because a seed
carries its own game data.

They come from the `archipelago/apworlds` share, published by
`tools/archipelago-apworlds` from a local Archipelago instance where you
install them with the APWorld Manager (see that tool's README). On every pod
start the `apworlds` initContainer copies the files listed in the share's
`apworlds.yaml` into `/app/custom_worlds`, after checking each sha256. A
mismatch, or a manifest for a different Archipelago version, fails the start.
Nothing else on the share runs.

An apworld is Python that runs inside this pod, with its cert key, session
key and DB, so only install ones you trust. The NetworkPolicy still blocks
all outbound traffic.

Apworlds without tutorials get an empty list (`scripts/webhost.py`), so
their options pages work. Their info and guide pages are missing.

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
