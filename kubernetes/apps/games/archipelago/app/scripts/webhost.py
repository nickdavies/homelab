"""Entry point for the webhost container: stock /app/WebHost.py, after two patches.

1. Remove the "Generate Single-Player Game" button from the options pages.
   Generation is off (SELFGEN: false, MAX_ROLL: 0 refuses it server side);
   the options creator is only for downloading YAMLs.
2. Give apworlds without tutorials an empty list. WebHost drops every world
   lacking web.tutorials, options page included, and WebWorld declares it
   without a default. Only apworlds: for folder worlds WebHost lists a docs/
   directory that may not exist, while apworlds take the zip branch.

WebHost then runs as __main__ and reuses the already-imported world registry.
WebHost starts its room hosters with multiprocessing "spawn", which re-imports
this file as __mp_main__; everything stays under the __main__ guard because the
hosters refuse to run with worlds loaded.
"""
import os
import runpy
import sys

APP = "/app"
TEMPLATES = [
    "WebHostLib/templates/playerOptions/playerOptions.html",
    "WebHostLib/templates/weightedOptions/weightedOptions.html",
]
GENERATE_BUTTON = 'name="intent-generate"'
# Left in place of the button, so a container restart (same writable layer)
# finds the template already patched.
MARKER = "{# intent-generate button removed by scripts/webhost.py #}\n"


def strip_generate_buttons() -> None:
    for template in TEMPLATES:
        path = os.path.join(APP, template)
        with open(path) as f:
            lines = f.readlines()
        if MARKER in lines:
            continue
        hits = [i for i, line in enumerate(lines) if GENERATE_BUTTON in line]
        if len(hits) != 1:
            sys.exit(f"webhost.py: expected one {GENERATE_BUTTON} line in {template}, found {len(hits)}; "
                     "the upstream template changed, update this patch")
        lines[hits[0]] = MARKER
        with open(path, "w") as f:
            f.writelines(lines)


def default_apworld_tutorials() -> None:
    from worlds import AutoWorldRegister
    for name, world in AutoWorldRegister.world_types.items():
        if world.zip_path and not hasattr(world.web, "tutorials"):
            world.web.tutorials = []
            print(f"webhost.py: {name} has no tutorials, serving its options pages anyway")


if __name__ == "__main__":
    os.chdir(APP)
    sys.path.insert(0, APP)
    strip_generate_buttons()
    default_apworld_tutorials()
    sys.argv[0] = os.path.join(APP, "WebHost.py")
    runpy.run_path(sys.argv[0], run_name="__main__")
