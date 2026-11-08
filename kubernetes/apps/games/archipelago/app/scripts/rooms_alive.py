"""Startup/liveness probe: every room in rooms.yaml accepts connections.

WebHost 0.6.8 never restarts a room hoster that dies, and a room that crashes
stays down, while the web UI keeps answering. Restarting the pod re-runs
reconcile, which re-pins every room's port. Drop this for a TCP check of the
web port once the fork's webhost-respawn-hosters fix ships upstream.
"""
import socket
import sys

import yaml

with open("/config/rooms.yaml") as f:
    rooms = (yaml.safe_load(f) or {}).get("rooms") or {}

dead = []
for name, spec in rooms.items():
    try:
        socket.create_connection(("127.0.0.1", spec["port"]), timeout=3).close()
    except OSError:
        dead.append(f"{name}:{spec['port']}")

if dead:
    print("not listening:", ", ".join(dead))
    sys.exit(1)
