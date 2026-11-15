# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Headless native service harness; poll on the main thread, without any Python networking thread."""

import json
import sys
import time
from pathlib import Path

import bpy

directory = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
sys.path.insert(0, str(Path(__file__).parent))
import fixture_recipes
import worker_recipes

assert not bpy.agent_bridge.status()["running"], "Explicit startup opt-out must leave the service stopped"
bpy.data.scenes.new("BridgeAlternate")
fixture_recipes.register()
worker_recipes.register()
status = bpy.agent_bridge.start(workspace=directory, allow_scripts=True)
(directory / "ready.json").write_text(json.dumps(status))
deadline = time.monotonic() + 180
try:
    while time.monotonic() < deadline and bpy.agent_bridge.status()["running"]:
        bpy.agent_bridge.poll()
        time.sleep(0.002)
finally:
    bpy.agent_bridge.stop()
assert time.monotonic() < deadline, "Harness timed out"
