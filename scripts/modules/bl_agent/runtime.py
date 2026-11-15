# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Main-thread scene adapter. This module never opens sockets or starts Python threads."""

import hashlib
import inspect
import json
import os
import time
import traceback
from array import array
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from _bpy import agent_bridge as native

_workspace = None
_allow_scripts = False
_recipes = {}
_epoch = 1
_revision = 0
_changes = deque(maxlen=128)
_previous = {}
_active_apply = None
_session = None
_last_ui_refresh = 0
_storage_limits = None


class DeferredResult:
    """The native job remains running while an owned subprocess works."""


def advance_jobs():
    global _last_ui_refresh
    import sys
    module = sys.modules.get('bl_agent.workers')
    if module is not None:
        module.advance()
    now = time.monotonic()
    if not bpy.app.background and now - _last_ui_refresh > 0.2:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D' and area.spaces.active.show_region_ui:
                    for region in area.regions:
                        if region.type == 'UI':
                            region.tag_redraw()
        _last_ui_refresh = now


def shutdown():
    import sys
    module = sys.modules.get('bl_agent.workers')
    if module is not None:
        module.shutdown()
    module = sys.modules.get('bl_agent.artifacts')
    if module is not None:
        module.close()


class OperationError(RuntimeError):
    def __init__(self, kind, message, *, details=None):
        self.kind = kind
        self.details = details or {}
        super().__init__(message)


def _info():
    return json.loads(native.info_json())


def _handle(data):
    global _session
    if _session is None:
        _session = _info()["session_id"]
    return f"{_session}:{_epoch}:{data.bl_rna.identifier}:{data.session_uid}"


def _resolve(handle, values):
    if not isinstance(handle, str):
        raise OperationError("INVALID_PARAMS", "Expected an ID handle")
    for value in values:
        if _handle(value) == handle:
            return value
    raise OperationError("STALE_HANDLE", "ID handle does not exist in this epoch")


@persistent
def _reset(*_):
    global _epoch, _previous
    _epoch += 1
    _previous = {}
    _record("reset", [])


def _record(category, handles):
    global _revision
    _revision += 1
    _changes.append({"revision": _revision, "category": category, "handles": handles})


@persistent
def _updated(scene, depsgraph):
    if scene.get("_agent_capture"):
        return
    handles = []
    for update in depsgraph.updates:
        if not (update.is_updated_geometry or update.is_updated_transform or update.is_updated_shading):
            continue
        data = update.id.original
        if hasattr(data, "session_uid"):
            handles.append(_handle(data))
    if handles:
        _record("depsgraph", sorted(set(handles)))


def _sync():
    """Cheap transform/structure scan complements depsgraph notifications, without writing IDs."""
    global _previous
    current = {}
    for obj in bpy.data.objects:
        current[_handle(obj)] = (obj.name, tuple(v for row in obj.matrix_world for v in row),
                                obj.data.session_uid if obj.data else None,
                                tuple(c.session_uid for c in obj.users_collection),
                                obj.hide_render, obj.hide_viewport, obj.instance_type,
                                obj.instance_collection.session_uid if obj.instance_collection else None)
    changed = [key for key in current.keys() | _previous.keys()
               if current.get(key) != _previous.get(key)]
    if changed:
        _previous = current
        _record("structure_transform", sorted(changed))


def _fingerprint(objects):
    digest = hashlib.sha256()
    for obj in sorted(objects, key=lambda value: value.session_uid):
        digest.update(repr((obj.session_uid, obj.name,
                            tuple(v for row in obj.matrix_world for v in row), obj.instance_type,
                            obj.instance_collection.session_uid if obj.instance_collection else None)).encode())
        if obj.type == 'MESH':
            mesh = obj.data
            coordinates = array('f', [0.0]) * (len(mesh.vertices) * 3)
            mesh.vertices.foreach_get("co", coordinates)
            digest.update(coordinates.tobytes())
            vertices = array('i', [0]) * len(mesh.loops)
            mesh.loops.foreach_get("vertex_index", vertices)
            digest.update(vertices.tobytes())
            for property_name in ("loop_start", "loop_total", "material_index"):
                values = array('i', [0]) * len(mesh.polygons)
                mesh.polygons.foreach_get(property_name, values)
                digest.update(values.tobytes())
            for uv in mesh.uv_layers:
                values = array('f', [0.0]) * (len(uv.data) * 2)
                uv.data.foreach_get("uv", values)
                digest.update(values.tobytes())
        digest.update(repr([(m.name, m.type) for m in obj.modifiers]).encode())
    return digest.hexdigest()


def configure(*, workspace=None, allow_scripts=False, storage_limits=None):
    global _workspace, _allow_scripts, _session, _previous, _storage_limits
    if _info()["running"]:
        raise RuntimeError("Stop the bridge before changing its workspace policy")
    _workspace = Path(workspace).resolve(strict=True) if workspace else None
    if _workspace and not _workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    _allow_scripts = bool(allow_scripts)
    from .storage import limits
    _storage_limits = limits(storage_limits)
    _session = None
    _previous = {}
    for handlers, function in (
        (bpy.app.handlers.load_post, _reset),
        (bpy.app.handlers.undo_post, _reset),
        (bpy.app.handlers.redo_post, _reset),
        (bpy.app.handlers.depsgraph_update_post, _updated),
    ):
        if function not in handlers:
            handlers.append(function)
    if not hasattr(bpy.types, "WM_OT_agent_bridge_apply"):
        bpy.utils.register_class(WM_OT_agent_bridge_apply)


def register_recipe(descriptor, function):
    if not callable(function):
        raise TypeError("Recipe must be callable")
    descriptor = dict(descriptor)
    for key in ("id", "version", "parameters", "scope"):
        if key not in descriptor:
            raise ValueError(f"Recipe descriptor requires {key}")
    path = inspect.getsourcefile(function)
    if not path:
        raise ValueError("Recipe must have a reproducible source file")
    descriptor["source_sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    dependencies = []
    for value in descriptor.get("dependencies", []):
        dependency = Path(value).resolve(strict=True)
        dependencies.append({"path": str(dependency),
                             "sha256": hashlib.sha256(dependency.read_bytes()).hexdigest()})
    descriptor["dependencies"] = dependencies
    modes = descriptor.setdefault("execution_modes", ["live"])
    if not modes or any(mode not in {"live", "worker_candidate"} for mode in modes):
        raise ValueError("Unsupported recipe execution mode")
    if "worker_candidate" in modes and (function.__name__ != function.__qualname__ or function.__closure__):
        raise ValueError("Worker recipes require a top-level file-backed function without closures")
    descriptor = json.loads(json.dumps(descriptor, allow_nan=False))
    descriptor['schema_sha256'] = hashlib.sha256(json.dumps(descriptor, sort_keys=True,
                                                           separators=(',', ':')).encode()).hexdigest()
    _recipes[descriptor["id"]] = (descriptor, function, Path(path).resolve())


def started():
    global _epoch, _revision, _previous, _session
    _epoch, _revision = 1, 0
    _previous, _session = {}, None
    _changes.clear()
    from . import artifacts
    info = _info()
    if _workspace:
        base = _workspace / '.cache/native-agent'
    else:
        cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
        if not cache.is_absolute():
            cache = Path.home() / '.cache'
        base = cache / 'blender/native-agent'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = base / info['session_id']
    artifacts.configure(root, _storage_limits)
    native.set_journal(str(root / 'journal.jsonl'), artifacts._store.metadata_reserve() // 4)
    native.update_metadata(json.dumps(_dispatch("bridge.status", {}), separators=(",", ":")))


def _validate_parameters(value, schema, path="parameters"):
    """Small documented schema subset, without a new runtime package dependency."""
    kind = schema.get("type")
    valid = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "boolean": lambda: isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        None: lambda: True,
    }
    if kind not in valid or not valid[kind]():
        raise OperationError("INVALID_PARAMS", f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise OperationError("INVALID_PARAMS", f"{path}: unsupported value")
    if kind == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise OperationError("INVALID_PARAMS", f"{path}.{key}: required")
        for key, item in value.items():
            if key not in properties:
                if schema.get("additionalProperties") is False:
                    raise OperationError("INVALID_PARAMS", f"{path}.{key}: unknown parameter")
            else:
                _validate_parameters(item, properties[key], f"{path}.{key}")
    elif kind == "array":
        if len(value) > schema.get("maxItems", 100000) or len(value) < schema.get("minItems", 0):
            raise OperationError("INVALID_PARAMS", f"{path}: invalid item count")
        for index, item in enumerate(value):
            _validate_parameters(item, schema.get("items", {}), f"{path}[{index}]")
    elif kind in {"number", "integer"}:
        if value < schema.get("minimum", float('-inf')) or value > schema.get("maximum", float('inf')):
            raise OperationError("INVALID_PARAMS", f"{path}: outside allowed range")


class JobContext:
    def __init__(self, params, scene, view_layer):
        self.job_id = params["_job_id"]
        self.parameters = params.get("parameters", {})
        self.scope = params.get("scope", {})
        self.scene = scene
        self.view_layer = view_layer
        self.workspace = _workspace

    def check_cancelled(self):
        if native.cancelled(self.job_id):
            raise OperationError("CANCELLED", "Cancellation requested at a cooperative boundary")

    def progress(self, phase, fraction):
        self.check_cancelled()
        native.progress(self.job_id, phase, fraction)

    def resolve_object(self, handle):
        return _resolve(handle, self.scene.objects)

    def object_reference(self, obj):
        return {'domain': 'live', 'handle': _handle(obj), 'name': obj.name}

    def read_source(self, path):
        return Path(path).read_bytes()

    def source_path(self, path):
        return Path(path).resolve(strict=True)


class WM_OT_agent_bridge_apply(bpy.types.Operator):
    bl_idname = "wm.agent_bridge_apply"
    bl_label = "Agent Bridge Edit"
    bl_options = {'UNDO', 'INTERNAL'}

    def execute(self, context):
        try:
            _active_apply["result"] = _active_apply["function"]()
        except Exception as exc:
            # Preserve the original structured error across the bpy.ops exception boundary.
            # Finish the undo step even for partial live edits; this is not automatic rollback.
            _active_apply["exception"] = exc
        return {'FINISHED'}


def _context(params):
    selector = params.get("context", {})
    scene_key = selector.get("scene")
    if not scene_key:
        raise OperationError("INVALID_PARAMS", "Scene operations require explicit context.scene")
    scene = bpy.data.scenes.get(scene_key)
    if scene is None:
        scene = _resolve(scene_key, bpy.data.scenes)
    view_layer = scene.view_layers.get(selector.get("view_layer", ""))
    if view_layer is None:
        raise OperationError("CONTEXT_UNAVAILABLE", "Explicit view_layer does not exist")
    return scene, view_layer


@contextmanager
def _scene_context(scene, view_layer):
    """Some native operators read window.scene directly, bypassing a scene-only override."""
    window = bpy.context.window
    if window is None:
        window = next(iter(bpy.context.window_manager.windows), None)
    previous = (window.scene, window.view_layer) if window else None
    active = view_layer.objects.active
    try:
        if window:
            if window.scene != scene:
                window.scene = scene
            if window.view_layer != view_layer:
                window.view_layer = view_layer
        override = {"scene": scene, "view_layer": view_layer}
        if window:
            override["window"] = window
        with bpy.context.temp_override(**override):
            yield
    finally:
        try:
            restored = active if active is not None and active.name in view_layer.objects else None
            if view_layer.objects.active != restored:
                view_layer.objects.active = restored
        except ReferenceError:
            view_layer.objects.active = None
        if window:
            if window.scene != previous[0]:
                window.scene = previous[0]
            if window.view_layer != previous[1]:
                window.view_layer = previous[1]


def _preconditions(params, allowed_modes=("live",)):
    scene, view_layer = _context(params)
    view_layer.update()
    _sync()
    if params.get("session_id") != _info()["session_id"] or params.get("scene_epoch") != _epoch:
        raise OperationError("STALE_HANDLE", "Session or scene epoch has changed")
    if params.get("expected_revision") != _revision:
        raise OperationError("REVISION_CONFLICT", f"Expected current revision {_revision}")
    if params.get("expected_fingerprint") != _fingerprint(scene.objects):
        raise OperationError("REVISION_CONFLICT", "Geometry/transform read-set fingerprint changed or missing")
    if bpy.context.mode != 'OBJECT' or bpy.app.is_job_running('RENDER'):
        raise OperationError("BUSY", "Live editing requires Object mode and no active render")
    if any(window.modal_operators for window in bpy.context.window_manager.windows):
        raise OperationError('BUSY', 'Finish the active modal operator before a scene operation')
    if params.get("execution_mode", "live") not in allowed_modes:
        raise OperationError("UNSUPPORTED_MODE", "Execution mode is not allowed for this operation")


def _execute(params, function, provenance=None):
    global _active_apply
    _preconditions(params)
    scene, view_layer = _context(params)
    before = {obj.session_uid for obj in bpy.data.objects}
    context = JobContext(params, scene, view_layer)
    context.check_cancelled()
    execution_artifact = None
    if provenance:
        from . import artifacts
        path = artifacts.output_path(context.job_id, 'execution.json')
        from .storage import atomic_json
        atomic_json(path, {**provenance, 'job_id': context.job_id, 'parameters': context.parameters,
                           'blender_build_hash': bpy.app.build_hash.decode(),
                           'scene': scene.name, 'view_layer': view_layer.name,
                           'frame': scene.frame_current, 'subframe': scene.frame_subframe,
                           'declared_resources_only': True})
        execution_artifact = artifacts.register(context.job_id, path, 'application/json',
                                                {'role': 'execution_manifest'})
    _active_apply = {"function": lambda: function(context)}
    try:
        with _scene_context(scene, view_layer):
            # Python operator calls default to suppressing undo; explicitly opt in here.
            bpy.ops.wm.agent_bridge_apply('EXEC_DEFAULT', True)
            view_layer.update()
        if "exception" in _active_apply:
            raise _active_apply["exception"]
        result = _active_apply.get("result")
    except Exception as exc:
        # A live Python recipe can edit before raising. Preserve a truthful recovery contract
        # rather than implying that an operator exception restored the source.
        if not isinstance(exc, OperationError):
            exc = OperationError(getattr(exc, 'kind', 'EXECUTION_FAILED'), str(exc),
                                 details=getattr(exc, 'details', {}))
        exc.details.setdefault('effects', 'may_have_changed')
        if execution_artifact:
            exc.details['execution_artifact_id'] = execution_artifact['artifact_id']
        exc.details['undo_available'] = not bpy.app.background and bpy.context.preferences.edit.use_global_undo
        raise exc
    finally:
        _active_apply = None
        _sync()
    return {"value": result, "scene_epoch": _epoch, "revision": _revision,
            "created": [_handle(obj) for obj in bpy.data.objects if obj.session_uid not in before],
            **({'execution_artifact_id': execution_artifact['artifact_id']} if execution_artifact else {}),
            "undo_available": not bpy.app.background and bpy.context.preferences.edit.use_global_undo}


def _inspect(params):
    scene, view_layer = _context(params)
    view_layer.update()
    _sync()
    limit = max(1, min(int(params.get("limit", 40)), 200))
    offset = max(0, int(params.get("offset", 0)))
    objects = list(scene.objects)
    if params.get("collection"):
        collection = _resolve(params["collection"], bpy.data.collections)
        objects = [obj for obj in objects if obj.name in collection.all_objects]
    if params.get("handles"):
        objects = [_resolve(handle, objects) for handle in params["handles"]]
    records = []
    for obj in objects[offset:offset + limit]:
        record = {"handle": _handle(obj), "name": obj.name, "type": obj.type,
                  "location": list(obj.matrix_world.translation), "dimensions": list(obj.dimensions),
                  "hidden_render": obj.hide_render,
                  "materials": [slot.material.name if slot.material else None for slot in list(obj.material_slots)[:32]],
                  'material_slots_total': len(obj.material_slots)}
        if obj.type == 'MESH':
            record["mesh"] = {"vertices": len(obj.data.vertices), "faces": len(obj.data.polygons),
                              "triangles": len(obj.data.loops) - 2 * len(obj.data.polygons),
                              "uv_layers": [layer.name for layer in obj.data.uv_layers]}
        records.append(record)
    result = {"session_id": _info()["session_id"], "scene_epoch": _epoch, "revision": _revision,
            "scene": scene.name, "scene_handle": _handle(scene), "view_layer": view_layer.name,
            "units": {"system": scene.unit_settings.system, "scale_length": scene.unit_settings.scale_length},
            "frame": scene.frame_current, "total": len(objects), "objects": records,
            "collections": [{"handle": _handle(c), "name": c.name}
                            for c in list(scene.collection.children)[max(0, int(params.get('collection_offset', 0))):
                                                                   max(0, int(params.get('collection_offset', 0))) + limit]],
            'collection_total': len(scene.collection.children),
            "next_offset": offset + limit if offset + limit < len(objects) else None,
            "fingerprint": _fingerprint(objects) if params.get("fingerprint") else None}
    if params.get('include_instances'):
        from . import occurrences
        selected = set(objects) if params.get('handles') or params.get('collection') else None
        instances = []
        instance_offset = max(0, int(params.get('instance_offset', 0)))
        try:
            rows = [row for row in occurrences.meshes(scene, view_layer, selected) if row['is_instance']]
        except ValueError as exc:
            raise OperationError('SCOPE_TOO_LARGE', str(exc)) from exc
        count = len(rows)
        for row in rows[instance_offset:instance_offset + limit]:
            original, parent = row['original'], row['parent']
            instances.append({'source_handle': _handle(original), 'source_name': original.name,
                              'instancer_handle': _handle(parent) if parent else None,
                              'instancer_name': parent.name if parent else None,
                              'persistent_id': row['persistent_id'],
                              'matrix_world': [list(axis) for axis in row['matrix']],
                              'vertices': row['vertices'], 'faces': row['faces']})
        result.update({'instances': instances, 'instance_total': count,
                       'instance_next_offset': instance_offset + limit if instance_offset + limit < count else None,
                       'evaluation_mode': view_layer.depsgraph.mode})
    return result


def _dispatch(method, params):
    if method in {"bridge.hello", "bridge.status"}:
        _sync()
        return {**_info(), "protocol_major": 1, "protocol_minor": 0,
                "bridge_version": "0.1.0-dev",
                "scene_epoch": _epoch, "revision": _revision,
                "blender_version": bpy.app.version_string, "build_hash": bpy.app.build_hash.decode(),
                "file": bpy.data.filepath, "scene": bpy.context.scene.name,
                "view_layer": bpy.context.view_layer.name,
                "capabilities": ["scene.inspect", "scene.changes", "recipes.list", "recipes.describe",
                                 "job.submit", "job.status", "job.wait", "job.list", "job.cancel", "recipe.execute", "bridge.stop",
                                 "capture.render", "artifacts.list", "artifacts.read",
                                 "capture.pick",
                                 "storage.stats", "storage.prune",
                                 "worker_candidate", "job.apply", "job.discard", "checkpoint.restore",
                                 "events.poll", "history.sessions", "history.job"] +
                                (["script.execute"] if _allow_scripts else [])}
    if method == "scene.inspect":
        return _inspect(params)
    if method == "artifacts.list":
        from . import artifacts
        records = artifacts.list_records(params.get("job_id"), session_id=params.get('session_id'))
        offset, limit = max(0, int(params.get('offset', 0))), max(1, min(128, int(params.get('limit', 64))))
        return records[offset:offset + limit]
    if method == 'storage.stats':
        from . import artifacts
        return artifacts.stats()
    if method == 'storage.prune':
        from . import retention
        _preconditions(params)
        return {'value': retention.prune(params), 'scene_epoch': _epoch, 'revision': _revision}
    if method == 'history.sessions':
        from . import journal, artifacts
        root = artifacts._store.root.parent
        return journal.history(root, active_session=_info()['session_id'], limit=params.get('limit', 8))
    if method == 'history.job':
        from . import journal, artifacts
        return journal.job(artifacts._store.root.parent, params.get('session_id', _info()['session_id']),
                           params.get('job_id'), active_session=_info()['session_id'])
    if method == "artifacts.read":
        from . import artifacts
        return artifacts.read(params["artifact_id"], params.get("offset", 0), params.get("limit", 1024 * 1024))
    if method == "capture.render":
        from . import capture
        _preconditions(params)
        scene, view_layer = _context(params)
        context = JobContext(params, scene, view_layer)
        provenance = {"session_id": _info()["session_id"], "scene_epoch": _epoch, "revision": _revision,
                      "frame": scene.frame_current, "build_hash": bpy.app.build_hash.decode()}
        return {"artifacts": capture.render(context, params.get("parameters", {}), provenance)}
    if method == 'capture.pick':
        from . import capture_data
        return capture_data.pick(params)
    if method == "scene.changes":
        _sync()
        revision = int(params.get("revision", -1))
        if params.get("scene_epoch") != _epoch or revision > _revision or (
                _changes and revision < _changes[0]["revision"] - 1):
            raise OperationError("RESET_REQUIRED", "Cursor is outside retained scene history")
        return {"scene_epoch": _epoch, "revision": _revision,
                "changes": [change for change in _changes if change["revision"] > revision]}
    if method == "recipes.list":
        return [{"id": key, "version": value[0]["version"], 'schema_sha256': value[0]['schema_sha256']}
                for key, value in _recipes.items()]
    if method == "recipes.describe":
        if params.get("id") not in _recipes:
            raise OperationError("UNKNOWN_RECIPE", "Recipe is not registered")
        descriptor = _recipes[params['id']][0]
        if params.get('if_schema_sha256') == descriptor['schema_sha256']:
            return {key: descriptor[key] for key in ('id', 'version', 'schema_sha256')} | {'not_modified': True}
        return descriptor
    if method == "recipe.execute":
        spec = params.get("recipe", {})
        if spec.get("id") not in _recipes:
            raise OperationError("UNKNOWN_RECIPE", "Recipe is not registered")
        descriptor, function, path = _recipes[spec["id"]]
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if (spec.get("version") != descriptor["version"] or
                spec.get("source_sha256") != descriptor["source_sha256"] or
                actual_hash != descriptor["source_sha256"]):
            raise OperationError("RECIPE_CHANGED", "Recipe version/source hash does not match")
        for dependency in descriptor["dependencies"]:
            if hashlib.sha256(Path(dependency["path"]).read_bytes()).hexdigest() != dependency["sha256"]:
                raise OperationError("RECIPE_CHANGED", "Recipe dependency hash has changed")
        _validate_parameters(params.get("parameters", {}), descriptor["parameters"])
        mode = params.get("execution_mode", "live")
        if mode not in descriptor["execution_modes"]:
            raise OperationError("UNSUPPORTED_MODE", "Recipe does not support this execution mode")
        if mode == "worker_candidate":
            from . import workers
            return workers.submit(params, descriptor, function, path)
        return _execute(params, function, {'recipe': descriptor, 'source_path': str(path)})
    if method in {"job.apply", "job.discard"}:
        from . import workers
        return workers.control(method, params)
    if method == "checkpoint.restore":
        from . import workers
        return workers.restore(params)
    if method == "script.execute":
        if not _allow_scripts or _workspace is None:
            raise OperationError("UNAUTHORIZED", "Workspace script execution is disabled")
        path = (_workspace / params.get("path", "")).resolve()
        if not path.is_relative_to(_workspace) or not path.is_file():
            raise OperationError("INVALID_PARAMS", "Script path escapes workspace or is not a file")
        code = path.read_bytes()
        if hashlib.sha256(code).hexdigest() != params.get("source_sha256"):
            raise OperationError("RECIPE_CHANGED", "Script source hash does not match")
        def run(context):
            namespace = {"__file__": str(path), "__name__": "__agent_job__", "bpy": bpy,
                         "job": context, "params": context.parameters}
            exec(compile(code, str(path), 'exec'), namespace)
            return namespace.get("result")
        return _execute(params, run, {'script_path': str(path), 'source_sha256': params['source_sha256']})
    raise OperationError("METHOD_NOT_FOUND", f"Unsupported method {method}")


def dispatch_json(request):
    start = time.perf_counter()
    request = json.loads(request)
    response = {"jsonrpc": "2.0", "id": request.get("id")}
    job_id, deferred = None, False
    try:
        params = dict(request.get("params", {}))
        job_id = params.get('_job_id') or (params.get('job_id') if request['method'] in {'job.apply', 'job.discard'} else None)
        params["_response_id"] = request.get("id")
        result = _dispatch(request["method"], params)
        if isinstance(result, DeferredResult):
            deferred = True
            response["_agent_deferred"] = True
            return json.dumps(response, separators=(",", ":"), allow_nan=False)
        response["result"] = result
        # Fail here if a recipe returned a nonportable object, not later in the transport thread.
        serialized = json.dumps(response, separators=(",", ":"), allow_nan=False)
        if job_id and len(serialized.encode('utf8')) > 64 * 1024:
            from . import artifacts
            path = artifacts.output_path(job_id, 'result.json')
            path.write_text(serialized, encoding='utf8')
            record = artifacts.register(job_id, path, 'application/json')
            response["result"] = {"externalized": True, "artifact": record,
                                  "summary": "Result exceeds the 64 KiB inline budget"}
            serialized = json.dumps(response, separators=(",", ":"), allow_nan=False)
        if job_id:
            from . import artifacts
            artifacts.seal(job_id)
        return serialized
    except Exception as exc:
        response.pop("result", None)
        response["error"] = {
            "code": -32601 if isinstance(exc, OperationError) and exc.kind == "METHOD_NOT_FOUND" else -32000,
            "message": str(exc)[:2048],
            "data": {"kind": getattr(exc, 'kind', "EXECUTION_FAILED"),
                     "recovery": getattr(exc, 'details', {}),
                     "elapsed_ms": (time.perf_counter() - start) * 1000,
                     "traceback": traceback.format_exc()[-4096:]},
        }
        if job_id and not deferred:
            try:
                from . import artifacts
                if getattr(exc, 'kind', '') == 'RESOURCE_LIMIT' and params.get('execution_mode') == 'worker_candidate':
                    artifacts.abort(job_id)
                else:
                    artifacts.seal(job_id)
            except Exception as storage_error:
                response['error']['data']['storage_error'] = str(storage_error)[:256]
        return json.dumps(response, separators=(",", ":"), allow_nan=False)
