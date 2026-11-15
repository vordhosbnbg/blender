# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Explicit release after live dependencies, job state and ownership have been checked."""

import json
from pathlib import Path

import bpy
from _bpy import agent_bridge as native

from . import artifacts
from .storage import Store, StoreError


def _inside(path, directory):
    try:
        return Path(path).resolve().is_relative_to(directory)
    except (ValueError, OSError):
        return False


def prune(params):
    from . import workers
    target = params.get('target_job_id')
    session_id = params.get('target_session_id', artifacts._store.root.name)
    archived = session_id != artifacts._store.root.name
    if archived:
        store = Store.open_inactive(artifacts.archive_root(session_id), workspace=artifacts._store.workspace)
        if target not in store.jobs:
            raise StoreError('UNKNOWN_JOB', 'Archived job is unavailable')
    else:
        store = artifacts._store
        status = json.loads(native.request_json(json.dumps({'jsonrpc': '2.0', 'id': 'retention',
            'method': 'job.status', 'params': {'job_id': target, 'include_result': False}})))
        if 'error' in status:
            raise StoreError('UNKNOWN_JOB', 'Release needs a retained job from this session')
        if status['result']['state'] not in {'succeeded', 'failed', 'cancelled', 'conflict', 'discarded', 'interrupted'}:
            raise StoreError('PINNED', 'Only a finished job can release its artifacts')
        if target in workers._workers:
            raise StoreError('PINNED', 'Owned worker has not stopped')
    directory = store.path(target)
    for record in [*workers._workers.values(), *workers._candidates.values()]:
        manifest = record['manifest']
        if not archived and manifest['job_id'] == target:
            continue
        paths = [*manifest['sources'], *(item['source'] for item in manifest['resources'])]
        if any(_inside(path, directory) for path in paths):
            raise StoreError('PINNED', 'Another candidate still depends on these source files')
    paths = [bpy.data.filepath]
    # Appending leaves weak library provenance IDs behind. Only genuinely linked IDs
    # (and their parent libraries) need the source file for the current Blender data.
    libraries = {data.library for data in bpy.data.user_map() if data.library}
    pending = list(libraries)
    while pending:
        parent = pending.pop().parent
        if parent and parent not in libraries:
            libraries.add(parent)
            pending.append(parent)
    paths.extend(bpy.path.abspath(library.filepath) for library in libraries)
    paths.extend(bpy.path.abspath(image.filepath, library=image.library) for image in bpy.data.images
                 if image.source == 'FILE' and not (image.packed_file or image.packed_files))
    if any(path and _inside(path, directory) for path in paths):
        raise StoreError('PINNED', 'Blender still uses a file in this job directory')
    checkpoints = ([key for key, item in store.records.items()
                    if item['record']['job_id'] == target and item['record']['metadata'].get('role') == 'checkpoint']
                   if archived else [key for key in workers._checkpoints if key.startswith(target + ':')])
    if checkpoints and not params.get('release_checkpoints', False):
        raise StoreError('PINNED', 'Job retains a recovery checkpoint; release_checkpoints must be explicit')
    plan = {**store.release_plan(target), 'session_id': session_id}
    if params.get('dry_run', True):
        return {**plan, 'dry_run': True, 'checkpoints': checkpoints}
    if not archived and not native.release_result(target):
        raise StoreError('PINNED', 'Native job cannot release its result references')
    if not archived:
        workers._candidates.pop(target, None)
        for key in checkpoints:
            del workers._checkpoints[key]
    result = store.release(target)
    if archived:
        artifacts._archives.pop(session_id, None)
    return {**result, 'dry_run': False, 'checkpoint_ids_released': checkpoints,
            'session_id': session_id, 'operation_key_retained': not archived}
