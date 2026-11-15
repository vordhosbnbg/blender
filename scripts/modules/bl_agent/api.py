# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Public in-process API; transport and job ownership live in the compiled service."""


def start(*, workspace=None, allow_scripts=False, storage_limits=None):
    from _bpy import agent_bridge as native
    native.ensure_main_thread()
    from . import runtime
    runtime.configure(workspace=workspace, allow_scripts=allow_scripts, storage_limits=storage_limits)
    native.start()
    try:
        runtime.started()
    except Exception:
        native.stop()
        raise
    return status()


def stop():
    from _bpy import agent_bridge as native
    native.stop()


def status():
    import json
    from _bpy import agent_bridge as native
    return json.loads(native.info_json())


def poll():
    """Dispatch on the main thread in a headless process; the GUI uses a native timer."""
    from _bpy import agent_bridge as native
    native.poll()


def register_recipe(descriptor, function):
    from _bpy import agent_bridge as native
    native.ensure_main_thread()
    from . import runtime
    runtime.register_recipe(descriptor, function)


def request(method, params=None):
    """Accept/query/control a native job. Does not wait or dispatch recursively."""
    import json
    from _bpy import agent_bridge as native
    from .client import BridgeError
    result = json.loads(native.request_json(json.dumps({
        'jsonrpc': '2.0', 'id': 'in-process', 'method': method, 'params': params or {},
    }, separators=(',', ':'), allow_nan=False)))
    if 'error' in result:
        raise BridgeError(result['error'])
    return result['result']


def jobs(*, limit=20):
    return request('job.list', {'limit': limit})


def events(*, after=0, limit=64):
    return request('events.poll', {'after': after, 'limit': limit})


def inspect(params):
    from _bpy import agent_bridge as native
    native.ensure_main_thread()
    from . import runtime
    return runtime._inspect(params)


def recipes():
    from _bpy import agent_bridge as native
    native.ensure_main_thread()
    from . import runtime
    return runtime._dispatch('recipes.list', {})


def describe_recipe(recipe_id):
    import json
    from _bpy import agent_bridge as native
    native.ensure_main_thread()
    from . import runtime
    return json.loads(json.dumps(runtime._dispatch('recipes.describe', {'id': recipe_id})))
