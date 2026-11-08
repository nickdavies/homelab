"""Make the WebHost DB match rooms.yaml. Runs as the initContainer, before WebHost
starts (SQLite allows one writer), from /app with the same config WebHost uses.

rooms.yaml:
    rooms:
      <name>:
        zip: <path relative to --seeds-dir>
        port: <int>

Each name maps to a fixed Room/Seed id (uuid5), so reruns are idempotent and a
room keeps its save across restarts. Rooms owned by ROOM_OWNER that are no
longer listed are put to sleep, never deleted: deleting would drop their save.
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
import uuid
import zipfile

import yaml

NAMESPACE = uuid.UUID("6b1f2c1e-5c0a-4a52-9b8e-0f0d6a3e8a11")
# Longer than any game will run; MAX_ROOM_TIMEOUT in config.yaml must be >= this.
NEVER_SLEEP = 10 * 365 * 24 * 60 * 60


def fail(msg: str) -> None:
    print(f"reconcile: {msg}", file=sys.stderr)
    sys.exit(1)


def load_rooms(path: str, seeds_dir: str) -> dict:
    with open(path) as f:
        rooms = (yaml.safe_load(f) or {}).get("rooms") or {}
    ports = {}
    for name, spec in rooms.items():
        port = spec.get("port")
        if not isinstance(port, int) or not 0 < port < 65536:
            fail(f"{name}: port must be an int in 1-65535, got {port!r}")
        if port in ports:
            fail(f"{name}: port {port} already used by {ports[port]}")
        ports[port] = name
        zip_path = os.path.join(seeds_dir, spec["zip"])
        if not os.path.isfile(zip_path):
            fail(f"{name}: {zip_path} does not exist")
        with open(zip_path, "rb") as f:
            spec["sha256"] = hashlib.sha256(f.read()).hexdigest()
        spec["path"] = zip_path
    return rooms


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rooms", required=True)
    parser.add_argument("--seeds-dir", required=True)
    parser.add_argument("--owner", default=os.environ.get("ROOM_OWNER"),
                        help="session id that owns reconciled rooms (env ROOM_OWNER)")
    args, _ = parser.parse_known_args()  # WebHost.get_app() parses its own flags (--config_override) too
    if not args.owner:
        fail("--owner or ROOM_OWNER is required")
    owner = uuid.UUID(args.owner)
    if owner.int == 0:
        fail("owner must not be UUID(int=0): WebHost's cleanup() deletes those rooms on start")
    if owner.version != 4:
        fail("owner must be a version 4 UUID: /session/<id> forces version 4, so no browser could adopt it")

    rooms = load_rooms(args.rooms, args.seeds_dir)

    import WebHost
    app = WebHost.get_app()
    if app.config["MAX_ROOM_TIMEOUT"] < NEVER_SLEEP:
        fail(f"MAX_ROOM_TIMEOUT must be >= {NEVER_SLEEP} or rooms will sleep")

    from flask import get_flashed_messages
    from pony.orm import commit, db_session, select
    from Utils import utcnow
    from WebHostLib.models import Room, Seed
    from WebHostLib.upload import upload_zip_to_db

    now = utcnow()
    with app.test_request_context(), db_session:
        for name, spec in rooms.items():
            room_id = uuid.uuid5(NAMESPACE, f"room:{name}")
            seed_id = uuid.uuid5(NAMESPACE, f"seed:{name}")

            seed = Seed.get(id=seed_id)
            if seed:
                stored = json.loads(seed.meta).get("zip_sha256")
                if stored != spec["sha256"]:
                    fail(f"{name}: zip changed since the room was created; seeds are immutable, use a new name")
            else:
                with zipfile.ZipFile(spec["path"]) as zf:
                    seed = upload_zip_to_db(zf, owner=owner, sid=seed_id,
                                            meta={"race": False, "zip_sha256": spec["sha256"]})
                if not isinstance(seed, Seed):
                    fail(f"{name}: upload failed: {seed or get_flashed_messages()}")
                print(f"reconcile: {name}: created seed {seed_id}")

            room = Room.get(id=room_id)
            if not room:
                # tracker stays random: it is the only secret guarding the public tracker URL
                room = Room(id=room_id, seed=seed, owner=owner, tracker=uuid.uuid4())
                print(f"reconcile: {name}: created room {room_id}")
            room.owner = owner
            room.last_port = spec["port"]
            room.timeout = NEVER_SLEEP
            room.last_activity = now
            print(f"reconcile: {name}: port {spec['port']} room={room_id} tracker={room.tracker}")

        wanted = {uuid.uuid5(NAMESPACE, f"room:{name}") for name in rooms}
        for room in select(r for r in Room if r.owner == owner):
            if room.id not in wanted and room.timeout == NEVER_SLEEP:
                room.timeout = 2 * 60 * 60
                room.last_activity = now - datetime.timedelta(days=1)
                print(f"reconcile: room {room.id} no longer listed, put to sleep (save kept)")
        commit()


if __name__ == "__main__":
    main()
