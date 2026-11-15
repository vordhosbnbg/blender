# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Main-thread ownership adapter for native deferred jobs and private Blender subprocesses."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import bpy
from _bpy import agent_bridge as native

from . import artifacts
from . import runtime as rt

_workers = {}
_candidates = {}
_checkpoints = {}
_db_names = ('objects', 'collections', 'meshes', 'curves', 'materials', 'node_groups', 'images',
             'lights', 'cameras', 'actions', 'armatures', 'lattices', 'worlds')


def _closure(root):
    dependencies = {}
    for dependency, owners in bpy.data.user_map().items():
        for owner in owners:
            dependencies.setdefault(owner, set()).add(dependency)
    visited, pending = set(), [root]
    while pending:
        value = pending.pop()
        if value in visited:
            continue
        visited.add(value)
        pending.extend(dependencies.get(value, ()))
    return visited


def _hash(path):
    return artifacts.file_hash(path)


def _snapshot(scene, path):
    bpy.data.libraries.write(str(path), {scene}, path_remap='ABSOLUTE', fake_user=True, compress=True)
    artifacts.check(path.parent.name)
    return _hash(path)


def _scope(params, descriptor):
    expected = descriptor['scope']
    name = expected.get('collection_name')
    if not name or params.get('scope') != expected:
        raise rt.OperationError('INVALID_SCOPE', 'Worker scope must match the registered collection scope')
    return expected


def _freeze_sources(directory, descriptor, path):
    records = [{'path': str(path), 'sha256': descriptor['source_sha256']}, *descriptor['dependencies']]
    sources = {}
    for index, record in enumerate(records):
        frozen, _ = artifacts.copy_input(directory.name, record['path'],
                                          f"source-{index}{Path(record['path']).suffix}", record['sha256'])
        sources[record['path']] = {'sha256': record['sha256'], 'frozen': str(frozen)}
    return sources


def _freeze_resources(snapshot, directory):
    records = []
    with bpy.data.libraries.load(str(snapshot), link=False) as (source, destination):
        names = list(source.images)
    for index, name in enumerate(names):
        image = bpy.data.images[name]
        if image.is_dirty:
            raise rt.OperationError('UNSAVED_IMAGE', 'Save or pack edited image pixels before taking a worker snapshot')
        if image.packed_file or image.packed_files:
            continue
        if image.source == 'GENERATED':
            continue
        if image.source != 'FILE' or '<UDIM>' in image.filepath:
            raise rt.OperationError('UNSUPPORTED_RESOURCE', 'Worker input needs packed or single-file images')
        source = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve(strict=True)
        frozen, digest = artifacts.copy_input(directory.name, source, f'image-{index}{source.suffix}')
        records.append({'image': name, 'source': str(source), 'frozen': str(frozen),
                        'sha256': digest})
    return records


def submit(params, descriptor, function, path):
    descriptor = json.loads(json.dumps(descriptor, allow_nan=False))
    rt._preconditions(params, allowed_modes=('worker_candidate',))
    scope = _scope(params, descriptor)
    if len(_workers) >= 2:
        raise rt.OperationError('BUSY', 'Two private workers are already running')
    threads = descriptor.get('worker_threads', 2)
    if not isinstance(threads, int) or isinstance(threads, bool) or not 1 <= threads <= 8:
        raise rt.OperationError('INVALID_PARAMS', 'Worker threads must be an integer from 1 to 8')
    if params.get('apply_mode', 'preview') not in {'preview', 'auto_if_valid'}:
        raise rt.OperationError('INVALID_PARAMS', 'Choose preview or auto_if_valid')
    timeout = descriptor.get('worker_timeout_s', 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
        raise rt.OperationError('INVALID_PARAMS', 'Worker timeout must be from 1 to 3600 seconds')
    scene, view_layer = rt._context(params)
    collection = bpy.data.collections.get(scope['collection_name'])
    if collection and (collection.children or collection.library):
        raise rt.OperationError('UNSUPPORTED_SCOPE', 'Initial adapter requires a local flat collection')
    scene_ids = _closure(scene)
    if collection and collection not in scene_ids:
        raise rt.OperationError('INVALID_SCOPE', 'Collection is outside the selected scene')
    if any(data.library for data in scene_ids):
        raise rt.OperationError('UNSUPPORTED_RESOURCE', 'Worker snapshot currently requires local dependencies')
    artifacts.admit(params['_job_id'], descriptor.get('storage_budget_bytes'))
    directory = artifacts.job_directory(params['_job_id'])
    snapshot = directory / 'input.blend'
    snapshot_hash = _snapshot(scene, snapshot)
    sources = _freeze_sources(directory, descriptor, path)
    resources = _freeze_resources(snapshot, directory)
    artifacts.output_directory(params['_job_id'], 'worker-artifacts')
    job_id = params['_job_id']
    manifest = {
        'job_id': job_id, 'params': params, 'descriptor': descriptor, 'scope': scope,
        'parent_pid': os.getpid(), 'snapshot': str(snapshot), 'snapshot_sha256': snapshot_hash,
        'scene': scene.name, 'view_layer': view_layer.name,
        'workspace': str(rt._workspace) if rt._workspace else '',
        'source_path': str(path), 'function': function.__name__,
        'package': function.__module__.rpartition('.')[0],
        'python_paths': [str(path.parent), *descriptor.get('python_paths', [])],
        'sources': sources, 'resources': resources,
        'object_names': {rt._handle(obj): obj.name for obj in scene.objects},
        'target_uid': collection.session_uid if collection else None,
        'storage': artifacts.worker_policy(job_id),
    }
    manifest_path = directory / 'worker-input.json'
    manifest_path.write_text(json.dumps(manifest, separators=(',', ':')), encoding='utf8')
    artifacts.register(job_id, manifest_path, 'application/json', {'role': 'execution_manifest',
                                                                 'snapshot_sha256': snapshot_hash})
    if native.cancelled(job_id):
        raise rt.OperationError('CANCELLED', 'Cancelled before starting worker')
    log = (directory / 'worker.log').open('wb')
    command = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-agent-bridge', '--disable-autoexec',
               '--threads', str(descriptor.get('worker_threads', 2)), '--python-exit-code', '1',
               '--python', str(Path(__file__).with_name('worker_main.py')), '--', str(manifest_path)]
    try:
        process = subprocess.Popen(command, cwd=str(rt._workspace) if rt._workspace else os.getcwd(),
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except Exception:
        log.close()
        raise
    _workers[job_id] = {'process': process, 'log': log, 'directory': directory, 'manifest': manifest,
                        'cancel_since': None, 'last_progress': None,
                        'deadline': time.monotonic() + timeout, 'stop_error': None}
    native.progress(job_id, 'worker_starting', 0.0)
    return rt.DeferredResult()


def _response(params, result=None, error=None):
    value = {'jsonrpc': '2.0', 'id': params.get('_response_id')}
    if error is None:
        value['result'] = result
    else:
        value['error'] = {'code': -32000, 'message': str(error)[:2048],
                          'data': {'kind': getattr(error, 'kind', 'WORKER_FAILED'),
                                   'recovery': getattr(error, 'details', {})}}
    return json.dumps(value, separators=(',', ':'), allow_nan=False)


def _finish_error(job_id, params, error, state='failed'):
    try:
        if getattr(error, 'kind', '') == 'RESOURCE_LIMIT':
            artifacts.abort(job_id)
        else:
            artifacts.seal(job_id)
    except Exception as storage_error:
        if hasattr(error, 'details'):
            error.details['storage_cleanup'] = str(storage_error)[:256]
    native.finish(job_id, state, _response(params, error=error))


def _accept_outputs(record):
    directory, manifest = record['directory'], record['manifest']
    report_path = directory / 'worker-result.json'
    if record['process'].returncode != 0 or not report_path.exists():
        raise rt.OperationError('WORKER_FAILED', f"Worker exited {record['process'].returncode}; inspect its log")
    if report_path.stat().st_size > 8 * 1024 * 1024:
        raise rt.OperationError('RESULT_TOO_LARGE', 'Worker report exceeds 8 MiB')
    report = json.loads(report_path.read_text())
    if not report.get('ok'):
        raise rt.OperationError(report.get('kind', 'WORKER_FAILED'), report.get('message', 'Worker failed'))
    candidate = report['candidate']
    path = Path(candidate['path']).resolve(strict=True)
    if path.parent != directory or _hash(path) != candidate['sha256']:
        raise rt.OperationError('INVALID_ARTIFACT', 'Candidate path/hash mismatch')
    outputs = [artifacts.register(manifest['job_id'], path, 'application/x-blender', {
        'role': 'candidate', 'collection': candidate['collection'], 'sha256': candidate['sha256']})]
    translated_artifacts = {}
    for item in report['artifacts']:
        source = Path(item['path']).resolve(strict=True)
        if not source.is_relative_to(directory) or _hash(source) != item['record']['sha256']:
            raise rt.OperationError('INVALID_ARTIFACT', 'Worker artifact path/hash mismatch')
        registered = artifacts.register(manifest['job_id'], source, item['record']['media_type'],
                                         item['record'].get('metadata'))
        outputs.append(registered)
        translated_artifacts[item['record']['artifact_id']] = registered
    report_artifact = record['report_artifact']
    def translate(value):
        if isinstance(value, dict):
            if value.get('artifact_id') in translated_artifacts:
                return translated_artifacts[value['artifact_id']]
            return {key: translate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [translate(item) for item in value]
        return value
    value = translate(report['result'])
    if len(json.dumps(value)) > 32 * 1024:
        value = {'externalized': True, 'artifact': report_artifact}
    return {'manifest': manifest, 'directory': directory, 'candidate': candidate, 'outputs': outputs,
            'result': value, 'worker_ms': report['elapsed_ms'], 'report': report_artifact}


def advance():
    for job_id, record in list(_workers.items()):
        process = record['process']
        if process.poll() is None and time.monotonic() - record.get('last_storage_check', 0) > .5:
            record['last_storage_check'] = time.monotonic()
            try:
                artifacts.check(job_id)
            except Exception as error:
                record['stop_error'] = error
        if time.monotonic() > record['deadline'] and process.poll() is None and record['stop_error'] is None:
            record['stop_error'] = rt.OperationError('WORKER_TIMEOUT', 'Worker exceeded its registered wall-time limit',
                                                     details={'effects': 'source_unchanged'})
        if native.cancelled(job_id) or record['stop_error'] is not None:
            (record['directory'] / 'cancel').touch(exist_ok=True)
            if record['cancel_since'] is None:
                record['cancel_since'] = time.monotonic()
            if process.poll() is None and time.monotonic() - record['cancel_since'] > 1.0:
                process.terminate()
            if process.poll() is None and time.monotonic() - record['cancel_since'] > 2.0:
                process.kill()
        progress_path = record['directory'] / 'progress.json'
        if progress_path.exists() and progress_path.stat().st_size < 4096:
            changed = progress_path.stat().st_mtime_ns
            if changed != record['last_progress']:
                try:
                    progress = json.loads(progress_path.read_text())
                    native.progress(job_id, str(progress['phase']), float(progress['fraction']))
                except (OSError, ValueError, KeyError, TypeError):
                    # A corrupt progress hint must not hide actual subprocess termination.
                    pass
                record['last_progress'] = changed
        if process.poll() is None:
            continue
        record['log'].close()
        del _workers[job_id]
        params = record['manifest']['params']
        log_path = record['directory'] / 'worker.log'
        try:
            artifacts.register(job_id, log_path, 'text/plain', {'role': 'worker_log', 'exit_code': process.returncode})
            report_path = record['directory'] / 'worker-result.json'
            if report_path.exists() and report_path.stat().st_size <= 8 * 1024 * 1024:
                record['report_artifact'] = artifacts.register(job_id, report_path, 'application/json',
                                                               {'role': 'worker_report'})
            if record['stop_error'] is not None:
                _finish_error(job_id, params, record['stop_error'])
                continue
            if native.cancelled(job_id):
                _finish_error(job_id, params, rt.OperationError('CANCELLED', 'Worker stopped'), 'cancelled')
                continue
            candidate = _accept_outputs(record)
            artifacts.seal(job_id)
            _candidates[job_id] = candidate
            result = {'value': candidate['result'], 'candidate': candidate['outputs'][0],
                      'artifacts': candidate['outputs'], 'worker_ms': candidate['worker_ms'],
                      'source_revision': params['expected_revision']}
            encoded = _response(params, result)
            if len(encoded.encode('utf8')) > 64 * 1024:
                path = artifacts.output_path(job_id, 'ready-result.json')
                path.write_text(encoded, encoding='utf8')
                report = artifacts.register(job_id, path, 'application/json', {'role': 'ready_result'})
                result = {'externalized': True, 'artifact': report, 'candidate': candidate['outputs'][0],
                          'artifacts': [candidate['outputs'][0], report], 'artifact_count': len(candidate['outputs']),
                          'value': {'externalized': True, 'artifact': report}}
                artifacts.seal(job_id)
            if not native.finish(job_id, 'ready', _response(params, result)):
                raise rt.OperationError('INVALID_RESULT', 'Worker result exceeds the native JSON contract',
                                        details={'effects': 'source_unchanged'})
        except Exception as error:
            _candidates.pop(job_id, None)
            kind = getattr(error, 'kind', '')
            state = 'conflict' if kind in {'REVISION_CONFLICT', 'STALE_HANDLE'} else 'failed'
            _finish_error(job_id, params, error, state)
    for job_id in list(_candidates):
        if native.cancelled(job_id):
            del _candidates[job_id]


def shutdown():
    for job_id, record in list(_workers.items()):
        (record['directory'] / 'cancel').touch(exist_ok=True)
        process = record['process']
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.5)
        record['log'].close()
        _finish_error(job_id, record['manifest']['params'], rt.OperationError('INTERRUPTED', 'Service stopped its owned worker'), 'interrupted')
        del _workers[job_id]
    _candidates.clear()
    _checkpoints.clear()


def _key(obj, descriptor):
    prop = descriptor.get('object_key')
    return 'property:' + str(obj[prop]) if prop and prop in obj else 'name:' + obj.name


def _members(collection, descriptor):
    rows = [{'key': _key(obj, descriptor), 'name': obj.name} for obj in collection.objects]
    if len({row['key'] for row in rows}) != len(rows):
        raise rt.OperationError('INVALID_SCOPE', 'Collection has duplicate semantic object keys')
    return rows


def _target(candidate):
    manifest = candidate['manifest']
    target = bpy.data.collections.get(manifest['scope']['collection_name'])
    if (target.session_uid if target else None) != manifest['target_uid']:
        raise rt.OperationError('REVISION_CONFLICT', 'Target collection was created, renamed or replaced')
    return target


def _verify(candidate):
    manifest = candidate['manifest']
    rt._preconditions(manifest['params'], allowed_modes=('worker_candidate',))
    target = _target(candidate)
    scene, view_layer = rt._context(manifest['params'])
    probe = candidate['directory'] / 'current-input.blend'
    if probe.exists():
        probe.unlink()
    if _snapshot(scene, probe) != manifest['snapshot_sha256']:
        raise rt.OperationError('REVISION_CONFLICT', 'Serialized source scene changed since worker preparation')
    for original, source in manifest['sources'].items():
        if _hash(original) != source['sha256']:
            raise rt.OperationError('RECIPE_CHANGED', 'Recipe/dependency changed before apply')
    for resource in manifest['resources']:
        if _hash(resource['source']) != resource['sha256']:
            raise rt.OperationError('REVISION_CONFLICT', 'External image changed before apply')
    if _hash(candidate['candidate']['path']) != candidate['candidate']['sha256']:
        raise rt.OperationError('INVALID_ARTIFACT', 'Candidate changed after validation')
    if native.cancelled(manifest['job_id']):
        raise rt.OperationError('CANCELLED', 'Candidate was cancelled')
    return target


def _before_ids():
    return {name: set(getattr(bpy.data, name)) for name in _db_names}


def _clean_import(before):
    for name in _db_names:
        values = getattr(bpy.data, name)
        for value in list(values):
            if value not in before[name]:
                values.remove(value, do_unlink=True)


def _load(path, collection_name, members):
    with bpy.data.libraries.load(str(path), link=False) as (source, destination):
        destination.collections = [collection_name]
        destination.objects = [row['name'] for row in members]
    collection = destination.collections[0]
    if collection is None or any(obj is None for obj in destination.objects):
        raise rt.OperationError('INVALID_ARTIFACT', 'Candidate members could not be imported')
    objects = dict(zip((row['key'] for row in members), destination.objects))
    if set(collection.objects) != set(objects.values()):
        raise rt.OperationError('INVALID_ARTIFACT', 'Candidate collection membership differs from its report')
    return collection, objects


def _copy_properties(target, source):
    try:
        source_keys = list(source.keys())
        target_keys = list(target.keys())
    except TypeError:
        return
    for key in target_keys:
        if key not in source:
            del target[key]
    for key in source_keys:
        value = source[key]
        target[key] = value.to_dict() if hasattr(value, 'to_dict') else value.to_list() if hasattr(value, 'to_list') else value


def _copy_rna(target, source):
    for prop in source.bl_rna.properties:
        if prop.identifier in {'rna_type', 'name', 'type'}:
            continue
        value = getattr(source, prop.identifier)
        if prop.type == 'COLLECTION':
            destination = getattr(target, prop.identifier)
            if len(destination) != len(value):
                raise rt.OperationError('UNSUPPORTED_SCOPE', 'Nested modifier/constraint structure changed')
            for a, b in zip(destination, value):
                _copy_rna(a, b)
        elif not prop.is_readonly:
            setattr(target, prop.identifier, value[:] if getattr(prop, 'is_array', False) else value)
    _copy_properties(target, source)


def _copy_object(target, source):
    if target.type != source.type or source.type not in {'MESH', 'EMPTY'}:
        raise rt.OperationError('UNSUPPORTED_SCOPE', 'Matched IDs require compatible mesh/empty types')
    if target.animation_data or source.animation_data or (source.type == 'MESH' and source.data.shape_keys):
        raise rt.OperationError('UNSUPPORTED_SCOPE', 'Animation and shape keys need a dedicated apply adapter')
    if [(m.name, m.type) for m in target.modifiers] != [(m.name, m.type) for m in source.modifiers]:
        raise rt.OperationError('UNSUPPORTED_SCOPE', 'Apply modifiers in the worker before replacing the mesh')
    if [(c.name, c.type) for c in target.constraints] != [(c.name, c.type) for c in source.constraints]:
        raise rt.OperationError('UNSUPPORTED_SCOPE', 'Constraint structure changes need a dedicated adapter')
    target.data = source.data
    target.parent = source.parent
    for name in ('parent_type', 'parent_bone', 'parent_vertices', 'matrix_parent_inverse',
                 'rotation_mode', 'location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle',
                 'scale', 'delta_location', 'delta_rotation_euler', 'delta_rotation_quaternion', 'delta_scale',
                 'color', 'hide_render', 'display_type', 'show_in_front', 'show_wire', 'show_all_edges',
                 'empty_display_type', 'empty_display_size', 'pass_index', 'visible_camera',
                 'visible_diffuse', 'visible_glossy', 'visible_transmission', 'visible_volume_scatter', 'visible_shadow'):
        setattr(target, name, getattr(source, name))
    for a, b in zip(target.modifiers, source.modifiers):
        _copy_rna(a, b)
    for a, b in zip(target.constraints, source.constraints):
        _copy_rna(a, b)
    for a, b in zip(target.material_slots, source.material_slots):
        a.link = b.link
        a.material = b.material
    _copy_properties(target, source)


def _swap(scene, target, path, collection_name, members, descriptor):
    before = _before_ids()
    committed = False
    try:
        imported, new = _load(path, collection_name, members)
        old = {_key(obj, descriptor): obj for obj in target.objects} if target else {}
        removed = set(old) - set(new)
        users = bpy.data.user_map(subset={old[key] for key in removed})
        for key in removed:
            if users.get(old[key], set()) - {target, *old.values()}:
                raise rt.OperationError('EXTERNAL_REFERENCE', 'A removed part has users outside the owned collection')
        # Rehearse field copying on unlinked objects before touching a source ID.
        for key in set(old) & set(new):
            probe = old[key].copy()
            try:
                _copy_object(probe, new[key])
            finally:
                bpy.data.objects.remove(probe, do_unlink=True)
        committed = True
        for key in set(old) & set(new):
            _copy_object(old[key], new[key])
        for key in set(old) & set(new):
            new[key].user_remap(old[key])
            bpy.data.objects.remove(new[key], do_unlink=True)
            new[key] = old[key]
        if target is None:
            target = imported
            scene.collection.children.link(target)
        else:
            for obj in new.values():
                if obj.name not in target.objects:
                    target.objects.link(obj)
            for key in removed:
                bpy.data.objects.remove(old[key], do_unlink=True)
            _copy_properties(target, imported)
            bpy.data.collections.remove(imported, do_unlink=True)
        for row in members:
            new[row['key']].name = row['name']
        return target, {key: rt._handle(obj) for key, obj in new.items()}
    except Exception as error:
        if not committed:
            _clean_import(before)
        if not isinstance(error, rt.OperationError):
            error = rt.OperationError('APPLY_FAILED', str(error))
        error.details['effects'] = 'may_have_changed' if committed else 'source_unchanged'
        raise error


def _apply(candidate):
    target = _verify(candidate)
    manifest = candidate['manifest']
    params = dict(manifest['params'], execution_mode='live')
    scene, view_layer = rt._context(params)
    descriptor = manifest['descriptor']
    checkpoint = {'target_existed': target is not None, 'scope': manifest['scope'],
                  'descriptor': descriptor, 'scene': scene.name, 'context': params['context']}
    if target:
        checkpoint_path = artifacts.output_path(manifest['job_id'], 'checkpoint.blend')
        checkpoint['members'] = _members(target, descriptor)
        bpy.data.libraries.write(str(checkpoint_path), {target}, path_remap='ABSOLUTE', fake_user=True, compress=True)
        checkpoint['path'] = str(checkpoint_path)
        checkpoint['artifact'] = artifacts.register(manifest['job_id'], checkpoint_path, 'application/x-blender',
                                                    {'role': 'checkpoint', 'scope': manifest['scope']})
    checkpoint_id = manifest['job_id'] + ':before-apply'
    _checkpoints[checkpoint_id] = checkpoint
    def commit(context):
        result, handles = _swap(scene, target, candidate['candidate']['path'], candidate['candidate']['collection'],
                                candidate['candidate']['members'], descriptor)
        checkpoint['target_uid'] = result.session_uid
        return {'collection': result.name, 'object_map': handles, 'checkpoint_id': checkpoint_id,
                'checkpoint_artifact': checkpoint.get('artifact')}
    try:
        return rt._execute(params, commit)
    except rt.OperationError as error:
        error.details['checkpoint_id'] = checkpoint_id
        error.details['checkpoint_artifact'] = checkpoint.get('artifact')
        raise


def control(method, params):
    job_id = params.get('job_id')
    candidate = _candidates.get(job_id)
    if candidate is None:
        raise rt.OperationError('JOB_STATE', 'Candidate resources are not available')
    if method == 'job.discard':
        del _candidates[job_id]
        return {'discarded': True}
    result = _apply(candidate)
    result['worker_result'] = candidate['result']
    result['artifacts'] = candidate['outputs']
    del _candidates[job_id]
    return result


def restore(params):
    checkpoint = _checkpoints.get(params.get('checkpoint_id'))
    if checkpoint is None:
        raise rt.OperationError('UNKNOWN_CHECKPOINT', 'Checkpoint is not available in this session')
    if params.get('scope') != checkpoint['scope']:
        raise rt.OperationError('INVALID_SCOPE', 'Restore scope must match the checkpoint')
    if checkpoint.get('artifact'):
        path = artifacts.path_for(checkpoint['artifact']['artifact_id'])
        if artifacts.file_hash(path) != checkpoint['artifact']['sha256']:
            raise rt.OperationError('INVALID_ARTIFACT', 'Checkpoint changed after registration')
    scene, view_layer = rt._context(params)
    if scene.name != checkpoint['scene']:
        raise rt.OperationError('INVALID_SCOPE', 'Checkpoint belongs to a different scene')
    target = bpy.data.collections.get(checkpoint['scope']['collection_name'])
    if target is None or target.session_uid != checkpoint.get('target_uid'):
        raise rt.OperationError('REVISION_CONFLICT', 'Checkpoint target was renamed or replaced')
    def commit(context):
        if checkpoint['target_existed']:
            result, handles = _swap(scene, target, checkpoint['path'], checkpoint['scope']['collection_name'],
                                    checkpoint['members'], checkpoint['descriptor'])
            return {'restored': True, 'collection': result.name, 'object_map': handles}
        users = bpy.data.user_map(subset=set(target.objects))
        if any(values - {target, *target.objects} for values in users.values()):
            raise rt.OperationError('EXTERNAL_REFERENCE', 'New parts acquired users outside the owned collection')
        for obj in list(target.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(target, do_unlink=True)
        return {'restored': True, 'removed_new_collection': True}
    return rt._execute(params, commit)
