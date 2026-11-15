# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Start/stop/restart checks. Launch with --disable-agent-bridge for explicit workspace setup."""

import json
from contextlib import ExitStack
from pathlib import Path
import tempfile

import bpy
from bl_agent import artifacts, journal, runtime

with tempfile.TemporaryDirectory(prefix='agent-lifecycle-') as temporary, ExitStack() as cleanup:
    workspace = Path(temporary)
    bpy.ops.wm.save_as_mainfile(filepath=str(workspace / 'inspection.blend'))
    assert not bpy.data.is_dirty
    query = {'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name},
             'fingerprint': True}
    assert not bpy.agent_bridge.status()['running']
    first = bpy.agent_bridge.start(workspace=workspace)
    cleanup.callback(bpy.agent_bridge.stop)
    inspected = bpy.agent_bridge.inspect(query)
    assert not bpy.data.is_dirty, 'Inspection dirtied the saved scene'
    first_handles = {row['handle'] for row in inspected['objects']}
    archived_path = artifacts.output_path('archived-fixture', 'checkpoint.blend')
    archived_path.write_bytes(b'checkpoint fixture')
    archived_artifact = artifacts.register('archived-fixture', archived_path, 'application/octet-stream',
                                          {'role': 'checkpoint'})
    artifacts.seal('archived-fixture')
    bpy.agent_bridge.stop()
    assert not Path(first['descriptor']).exists()
    assert journal.read(first['journal'])['closed']
    bpy.ops.wm.open_mainfile(filepath=str(workspace / 'inspection.blend'), use_scripts=False)
    assert not bpy.agent_bridge.status()['running'], 'Loading a file enabled the service'
    second = bpy.agent_bridge.start(workspace=workspace)
    assert first['session_id'] != second['session_id']
    current = bpy.agent_bridge.inspect(query)
    assert current['revision'] == inspected['revision']
    assert not first_handles.intersection(row['handle'] for row in current['objects'])
    archived = runtime._dispatch('artifacts.list', {'session_id': first['session_id']})
    assert archived == [archived_artifact]
    prune = {'context': query['context'], 'session_id': current['session_id'],
             'scene_epoch': current['scene_epoch'], 'expected_revision': current['revision'],
             'expected_fingerprint': current['fingerprint'], 'target_session_id': first['session_id'],
             'target_job_id': 'archived-fixture'}
    try:
        runtime._dispatch('storage.prune', prune)
    except Exception as error:
        assert error.kind == 'PINNED'
    else:
        raise AssertionError('Archived checkpoint released without explicit authorization')
    preview = runtime._dispatch('storage.prune', {**prune, 'release_checkpoints': True})
    assert preview['value']['dry_run'] and archived_path.exists()
    # A live unpacked image is a dependency even when the session that wrote it stopped.
    image = bpy.data.images.new('ArchiveDependency', width=1, height=1)
    image.filepath = str(archived_path)
    image.source = 'FILE'
    current = bpy.agent_bridge.inspect(query)
    prune.update(expected_revision=current['revision'], expected_fingerprint=current['fingerprint'])
    try:
        runtime._dispatch('storage.prune', {**prune, 'dry_run': False, 'release_checkpoints': True})
    except Exception as error:
        assert error.kind == 'PINNED'
    else:
        raise AssertionError('Live file dependency was deleted')
    bpy.data.images.remove(image)
    current = bpy.agent_bridge.inspect(query)
    prune.update(expected_revision=current['revision'], expected_fingerprint=current['fingerprint'])
    runtime._dispatch('storage.prune', {**prune, 'dry_run': False, 'release_checkpoints': True})
    assert not archived_path.exists()
    assert not runtime._dispatch('artifacts.list', {'session_id': first['session_id']})
    bpy.agent_bridge.stop()
    # A truncated final append keeps valid earlier summaries and cannot resume the edit.
    path = workspace / 'truncated.jsonl'
    prefix = {'cursor': 1, 'session_id': 'test', 'type': 'job.started',
              'job': {'job_id': 'job-1', 'state': 'applying'}}
    path.write_bytes(json.dumps(prefix).encode() + b'\n{"cursor":2')
    recovered = journal.read(path)
    assert recovered['incomplete_tail'] and not recovered['automatic_resume']
    assert recovered['jobs'][0]['state'] == 'interrupted'
    assert recovered['jobs'][0]['effects_unknown']
print('AGENT_API_LIFECYCLE_OK')
