# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Normal GUI startup fixture: the service must already be running before this script."""

import json
from pathlib import Path
import sys

import bpy

directory = Path(sys.argv[sys.argv.index('--') + 1])
status = bpy.agent_bridge.status()
assert status['running'], 'Default GUI startup did not start the bridge'
addons = sorted(bpy.context.preferences.addons.keys())
status.update({'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
               'enabled_addons': addons, 'panel_registered': hasattr(bpy.types, 'VIEW3D_PT_agent_bridge')})
(directory / 'ready.json').write_text(json.dumps(status))


def finish():
    if not (directory / 'quit').exists():
        return .02
    assert addons == sorted(bpy.context.preferences.addons.keys())
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(finish, first_interval=.02)
