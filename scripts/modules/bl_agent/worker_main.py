# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Private subprocess entry point. No listener, Python network thread or live parent pointers."""

import hashlib
import json
import math
from array import array
import os
import resource as resource_limits
import errno
from pathlib import Path
import sys
import time
import traceback
import types

import bpy
from _bpy import agent_bridge as native


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x', encoding='utf8') as stream:
        json.dump(value, stream, separators=(',', ':'), allow_nan=False)
    temporary.replace(path)


class WorkerContext:
    def __init__(self, manifest, directory, scene):
        self.job_id = manifest['job_id']
        self.parameters = manifest['params'].get('parameters', {})
        self.scope = manifest['scope']
        self.scene = scene
        self.view_layer = scene.view_layers[manifest['view_layer']]
        self.workspace = Path(manifest['workspace']) if manifest['workspace'] else None
        self._manifest = manifest
        self._directory = directory
        self._last_progress = 0

    def check_cancelled(self):
        if (self._directory / 'cancel').exists():
            from bl_agent.runtime import OperationError
            raise OperationError('CANCELLED', 'Worker cancelled at a cooperative boundary')

    def progress(self, phase, fraction):
        self.check_cancelled()
        now = time.monotonic()
        if now - self._last_progress > 0.04:
            atomic_json(self._directory / 'progress.json', {'phase': str(phase)[:128], 'fraction': fraction})
            self._last_progress = now

    def resolve_object(self, handle):
        name = self._manifest['object_names'].get(handle)
        if name is None or name not in self.scene.objects:
            raise ValueError('Object handle is outside the frozen input scene')
        return self.scene.objects[name]

    def object_reference(self, obj):
        from bl_agent.workers import _key
        return {'domain': 'worker_candidate', 'name': obj.name,
                'key': _key(obj, self._manifest['descriptor']),
                'source_session': self._manifest['params']['session_id']}

    def read_source(self, path):
        source = self._manifest['sources'].get(str(Path(path).resolve()))
        if source is None:
            raise ValueError('Worker source was not declared and frozen')
        data = Path(source['frozen']).read_bytes()
        if hashlib.sha256(data).hexdigest() != source['sha256']:
            raise ValueError('Frozen source hash mismatch')
        return data

    def source_path(self, path):
        from bl_agent.artifacts import file_hash
        source = self._manifest['sources'].get(str(Path(path).resolve()))
        if source is None or file_hash(source['frozen']) != source['sha256']:
            raise ValueError('Frozen source is missing or changed')
        return Path(source['frozen'])


def run(manifest_path):
    directory = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    native.worker_parent_watch(manifest['parent_pid'])
    if bpy.agent_bridge.status()['running']:
        raise RuntimeError('A private worker must never start a listener')
    started = time.perf_counter()
    from bl_agent import artifacts
    artifact_root = directory / 'worker-artifacts'
    artifacts.configure(artifact_root, manifest['storage'], workspace=artifact_root)
    resource_limits.setrlimit(resource_limits.RLIMIT_FSIZE, (manifest['storage']['file_bytes'], manifest['storage']['file_bytes']))
    try:
        snapshot = Path(manifest['snapshot'])
        if artifacts.file_hash(snapshot) != manifest['snapshot_sha256']:
            raise RuntimeError('Frozen scene hash mismatch')
        bpy.ops.wm.open_mainfile(filepath=str(snapshot), load_ui=False, use_scripts=False)
        scene = bpy.data.scenes[manifest['scene']]
        if bpy.context.window:
            bpy.context.window.scene = scene
            bpy.context.window.view_layer = scene.view_layers[manifest['view_layer']]
        for resource in manifest['resources']:
            image = bpy.data.images.get(resource['image'])
            if image:
                image.filepath = resource['frozen']
                image.pack()
                image.filepath = resource['source']
        for addon in manifest['descriptor'].get('required_addons', []):
            import addon_utils
            if addon_utils.enable(addon, default_set=False, persistent=False) is None:
                raise RuntimeError('Required worker add-on could not be loaded: ' + addon)
        context = WorkerContext(manifest, directory, scene)
        context.check_cancelled()
        source_path = manifest['source_path']
        module_name = '_bl_agent_worker_recipe'
        module = types.ModuleType(module_name)
        module.__file__ = source_path
        module.__package__ = manifest.get('package', '')
        sys.modules[module_name] = module
        for path in reversed(manifest['python_paths']):
            sys.path.insert(0, path)
        exec(compile(context.read_source(source_path), source_path, 'exec'), module.__dict__)
        function = getattr(module, manifest['function'])
        scene.view_layers[manifest['view_layer']].update()
        result = function(context)
        context.check_cancelled()
        collection = bpy.data.collections.get(manifest['scope']['collection_name'])
        if collection is None:
            raise RuntimeError('Recipe did not create its declared output collection')
        if collection.children:
            raise RuntimeError('This apply adapter currently requires a flat output collection')
        members = set(collection.objects)
        from bl_agent.workers import _closure, _members
        dependencies = _closure(collection)
        if any(isinstance(data, bpy.types.Object) and data not in members for data in dependencies):
            raise RuntimeError('Candidate depends on object IDs outside its owned collection')
        from bl_agent.runtime import OperationError
        for data in dependencies:
            if data.library or getattr(data, 'animation_data', None):
                raise OperationError('UNSUPPORTED_SCOPE', 'Candidate needs local, non-animated dependencies')
            if isinstance(data, bpy.types.Image):
                if data.source not in {'FILE', 'GENERATED'}:
                    raise OperationError('UNSUPPORTED_RESOURCE', 'Candidate images must be packed file/generated images')
                if not (data.packed_file or data.packed_files):
                    data.pack()
        for obj in members:
            if obj.type not in {'MESH', 'EMPTY'} or (obj.type == 'MESH' and obj.data.shape_keys):
                raise OperationError('UNSUPPORTED_SCOPE', 'Candidate supports mesh/empty parts without shape keys')
            if not all(math.isfinite(value) for row in obj.matrix_world for value in row):
                raise OperationError('INVALID_CANDIDATE', 'Candidate has a non-finite transform')
            if obj.type == 'MESH':
                coordinates = array('f', [0.0]) * (3 * len(obj.data.vertices))
                obj.data.vertices.foreach_get('co', coordinates)
                if not all(map(math.isfinite, coordinates)) or obj.data.validate(clean_customdata=False):
                    raise OperationError('INVALID_CANDIDATE', 'Candidate mesh contains invalid coordinates or topology')
            if obj.parent and obj.parent not in members:
                raise RuntimeError('Candidate contains a parent outside its owned collection')
            for constraint in obj.constraints:
                if getattr(constraint, 'target', None) and constraint.target not in members:
                    raise RuntimeError('Candidate constraint targets an object outside its collection')
            for modifier in obj.modifiers:
                for prop in modifier.bl_rna.properties:
                    if prop.type == 'POINTER' and isinstance(getattr(modifier, prop.identifier, None), bpy.types.Object):
                        if getattr(modifier, prop.identifier) not in members:
                            raise RuntimeError('Candidate modifier references an external object')
        # Raw meshes can be valid while a supported modifier produces invalid coordinates.
        context.view_layer.update()
        graph = context.view_layer.depsgraph
        for obj in members:
            if obj.type == 'MESH':
                evaluated = obj.evaluated_get(graph)
                mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
                try:
                    coordinates = array('f', [0.0]) * (3 * len(mesh.vertices))
                    mesh.vertices.foreach_get('co', coordinates)
                    if not all(map(math.isfinite, coordinates)):
                        raise OperationError('INVALID_CANDIDATE', 'Evaluated candidate has non-finite coordinates')
                finally:
                    evaluated.to_mesh_clear()
        candidate = directory / 'candidate.blend'
        bpy.data.libraries.write(str(candidate), {collection}, path_remap='RELATIVE_ALL',
                                 fake_user=True, compress=True)
        previews = []
        preview = manifest['params'].get('preview')
        if preview:
            from bl_agent import capture
            preview_scene = bpy.data.scenes.new('AgentCandidatePreview')
            preview_scene.collection.children.link(collection)
            previous_scene, previous_layer = context.scene, context.view_layer
            context.scene, context.view_layer = preview_scene, preview_scene.view_layers[0]
            context.view_layer.update()
            try:
                previews = capture.render(context, preview, {
                    'stage': 'candidate', 'source_session': manifest['params']['session_id'],
                    'source_epoch': manifest['params']['scene_epoch'],
                    'source_revision': manifest['params']['expected_revision'],
                    'worker_build_hash': bpy.app.build_hash.decode(),
                })
            finally:
                context.scene, context.view_layer = previous_scene, previous_layer
                bpy.data.scenes.remove(preview_scene)
        context.check_cancelled()
        candidate_record = {
            'path': str(candidate), 'sha256': artifacts.file_hash(candidate),
            'collection': collection.name, 'objects': len(collection.objects),
            'members': _members(collection, manifest['descriptor']),
        }
        artifacts.seal(manifest['job_id'])
        artifacts.close()
        atomic_json(directory / 'worker-result.json', {
            'ok': True, 'result': result, 'candidate': candidate_record,
            'artifacts': [{'record': item, 'path': str(artifacts.path_for(item['artifact_id']))}
                          for item in artifacts.list_records()],
            'artifact_root': str(directory / 'worker-artifacts'),
            'elapsed_ms': (time.perf_counter() - started) * 1000, 'pid': os.getpid(),
        })
    except Exception as exc:
        atomic_json(directory / 'worker-result.json', {
            'ok': False, 'kind': 'RESOURCE_LIMIT' if isinstance(exc, OSError) and exc.errno == errno.EFBIG else
                               getattr(exc, 'kind', 'WORKER_FAILED'), 'message': str(exc)[:2048],
            'traceback': traceback.format_exc()[-8192:], 'elapsed_ms': (time.perf_counter() - started) * 1000,
        })


if __name__ == '__main__':
    run(Path(sys.argv[sys.argv.index('--') + 1]).resolve(strict=True))
