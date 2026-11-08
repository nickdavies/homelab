"""initContainer: copy the apworlds listed in apworlds.yaml from the NAS into
/app/custom_worlds (an emptyDir), checking each file's sha256 first.

The manifest and files are written by tools/archipelago-apworlds from a local
Archipelago instance of the same version. A missing manifest means no
apworlds. Files on the share that aren't listed are ignored, so only what the
manifest names ever runs.
"""
import hashlib
import os
import shutil
import sys

import yaml

from Utils import __version__ as ap_version


def fail(msg: str) -> None:
    sys.exit(f"install_apworlds: {msg}")


def main(src: str, dest: str) -> None:
    manifest_path = os.path.join(src, "apworlds.yaml")
    if not os.path.isfile(manifest_path):
        print(f"install_apworlds: no {manifest_path}, installing nothing")
        return
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f) or {}
    if manifest.get("ap_version") != ap_version:
        fail(f"manifest is for Archipelago {manifest.get('ap_version')}, this image is {ap_version}; "
             "regenerate it from a matching local instance")

    for entry in manifest.get("apworlds") or []:
        name = entry["file"]
        if os.path.basename(name) != name or not name.endswith(".apworld"):
            fail(f"{name!r} is not a plain .apworld file name")
        path = os.path.join(src, name)
        if not os.path.isfile(path):
            fail(f"{name} is listed but missing from {src}")
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest != entry["sha256"]:
            fail(f"{name} sha256 {digest} does not match the manifest's {entry['sha256']}")
        shutil.copyfile(path, os.path.join(dest, name))
        print(f"install_apworlds: {name} {entry.get('world_version', '?')} ({entry.get('source') or 'not in the index'})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
