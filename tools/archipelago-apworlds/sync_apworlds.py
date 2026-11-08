#!/usr/bin/env python3
"""Publish the apworlds installed in a local Archipelago instance to the
WebHost's apworlds share, with a manifest the pod checks them against.

The local instance is where you pick apworlds (APWorld Manager, "Install/Update
Apworlds" in its Launcher). This copies them, minus the Manager itself, to the
share and writes apworlds.yaml listing each file's sha256. The pod's apworlds
initContainer (kubernetes/apps/games/archipelago/app/scripts/install_apworlds.py)
installs only files that match it.

Each apworld is looked up by hash in the APWorld index to record where it was
published; a file not in the index (a local build, say) is still published,
marked as such. The local instance must run the same Archipelago version as the
server image, so the Manager only offers compatible worlds.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATEFULSET = os.path.join(REPO, "kubernetes/apps/games/archipelago/app/statefulset.yaml")
INDEX_URL = "https://raw.githubusercontent.com/silasary/apworlds/refs/heads/main/index.json"
SKIP = {"apworld_manager.apworld"}  # the Manager is a Launcher tool, not a game


def fail(msg: str) -> None:
    sys.exit(f"sync_apworlds: {msg}")


def server_version() -> str:
    with open(STATEFULSET) as f:
        match = re.search(r"ghcr\.io/archipelagomw/archipelago:(\d+\.\d+\.\d+)@", f.read())
    if not match:
        fail(f"no archipelago image tag found in {STATEFULSET}")
    return match.group(1)


def instance_version(worlds_dir: str) -> str:
    utils = os.path.join(os.path.dirname(os.path.abspath(worlds_dir)), "Utils.py")
    with open(utils) as f:
        match = re.search(r'^__version__ = "([^"]+)"', f.read(), re.MULTILINE)
    if not match:
        fail(f"no __version__ in {utils}")
    return match.group(1)


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", v)[:3])


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def manifest_of(path: str) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            name = next((n for n in zf.namelist() if n.endswith("/archipelago.json") or n == "archipelago.json"), None)
            return json.loads(zf.read(name)) if name else {}
    except (zipfile.BadZipFile, json.JSONDecodeError) as e:
        fail(f"{os.path.basename(path)} is not a readable apworld: {e}")


def load_index(source: str) -> dict[str, dict]:
    if os.path.isfile(source):
        with open(source) as f:
            data = json.load(f)
    else:
        with urllib.request.urlopen(source, timeout=60) as r:
            data = json.load(r)
    return {w["hash_sha256"]: w for w in data["worlds"] if w.get("hash_sha256")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--worlds-dir", default=os.path.expanduser("~/workspaces/homelab/apworld-manager/custom_worlds"),
                        help="custom_worlds of the local Archipelago instance")
    parser.add_argument("--dest", default=os.path.expanduser("~/mnt/mount1/archipelago/apworlds"),
                        help="the apworlds share, as mounted locally")
    parser.add_argument("--index", default=INDEX_URL, help="APWorld index URL or local index.json")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument("--restart", action="store_true", help="delete the pod afterwards so it loads the new set")
    args = parser.parse_args()

    version = server_version()
    local = instance_version(args.worlds_dir)
    if local != version:
        fail(f"local instance is Archipelago {local}, the server runs {version}")
    if not os.path.isdir(args.dest):
        fail(f"{args.dest} does not exist (is the share mounted?)")

    index = load_index(args.index)
    entries = []
    for name in sorted(os.listdir(args.worlds_dir)):
        path = os.path.join(args.worlds_dir, name)
        if not name.endswith(".apworld") or name in SKIP or not os.path.isfile(path):
            continue
        digest = sha256(path)
        meta = manifest_of(path)
        indexed = index.get(digest)
        info = {**(indexed or {}).get("metadata", {}), **meta}
        for bound, ok in (("minimum_ap_version", lambda b: version_tuple(version) >= version_tuple(b)),
                          ("maximum_ap_version", lambda b: version_tuple(version) <= version_tuple(b))):
            if info.get(bound) and not ok(info[bound]):
                fail(f"{name} needs {bound} {info[bound]}, the server runs {version}")
        entries.append({
            "file": name,
            "sha256": digest,
            "game": info.get("game"),
            "world_version": info.get("world_version"),
            "source": indexed["world"] if indexed else None,
        })

    wanted = {e["file"] for e in entries}
    for e in entries:
        dest = os.path.join(args.dest, e["file"])
        changed = not os.path.isfile(dest) or sha256(dest) != e["sha256"]
        print(f"{'copy' if changed else 'same'}  {e['file']:40} {e['world_version'] or '?':12} "
              f"{e['source'] or 'NOT IN THE INDEX'}")
        if changed and not args.dry_run:
            shutil.copyfile(os.path.join(args.worlds_dir, e["file"]), dest + ".tmp")
            os.replace(dest + ".tmp", dest)
    for name in sorted(os.listdir(args.dest)):
        if name.endswith(".apworld") and name not in wanted:
            print(f"remove {name}")
            if not args.dry_run:
                os.remove(os.path.join(args.dest, name))

    manifest = {"ap_version": version, "apworlds": entries}
    if args.dry_run:
        print("dry run: nothing written")
        return
    tmp = os.path.join(args.dest, "apworlds.yaml.tmp")
    with open(tmp, "w") as f:
        f.write("# Written by tools/archipelago-apworlds/sync_apworlds.py; don't edit by hand.\n")
        yaml.safe_dump(manifest, f, sort_keys=False)
    os.replace(tmp, os.path.join(args.dest, "apworlds.yaml"))
    print(f"wrote apworlds.yaml: {len(entries)} apworlds for Archipelago {version}")

    if args.restart:
        subprocess.run(["kubectl", "-n", "games", "delete", "pod", "archipelago-0"], check=True)
    else:
        print("The pod loads apworlds at start: kubectl -n games delete pod archipelago-0 (or rerun with --restart)")


if __name__ == "__main__":
    main()
