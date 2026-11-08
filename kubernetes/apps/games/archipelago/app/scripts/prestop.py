"""preStop hook: stop WebHost without losing room progress.

WebHost 0.6.8 installs no SIGTERM handler, and as PID 1 the kernel drops
signals it doesn't handle, so the pod would wait out its grace period and then
be SIGKILLed, losing everything since the last autosave (up to a minute).

SIGINT works on the room hosters: each saves its rooms and exits. Then SIGINT
to WebHost returns its server loop. The order matters: WebHost waits forever
at exit for hosters that are still running. Drop this hook once the fork's
webhost-sigterm-save fix ships upstream.
"""
import os
import signal
import time

import psutil


def running(p: psutil.Process) -> bool:
    # WebHost never reaps its hosters, so one that has exited stays a zombie.
    try:
        return p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


hosters = [p for p in psutil.process_iter(["name"]) if p.info["name"].startswith("MultiHoster")]
for p in hosters:
    p.send_signal(signal.SIGINT)
deadline = time.monotonic() + 20
while any(running(p) for p in hosters) and time.monotonic() < deadline:
    time.sleep(0.2)
for p in hosters:
    if running(p):
        print(f"prestop: {p.info['name']} ({p.pid}) did not exit after SIGINT")
os.kill(1, signal.SIGINT)
