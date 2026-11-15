# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""A tiny journal budget reports exhaustion, preserves whole records and never replays."""

import json
from pathlib import Path
import tempfile
import time

import bpy
from bl_agent import journal

with tempfile.TemporaryDirectory(prefix='agent-journal-limit-') as temporary:
    status = bpy.agent_bridge.start(workspace=temporary, storage_limits={
        'file_bytes': 65536, 'job_bytes': 65536, 'session_bytes': 262144, 'workspace_bytes': 524288})
    try:
        for index in range(30):
            accepted = bpy.agent_bridge.request('job.submit', {'operation': 'unsupported.fixture',
                'operation_key': str(index), 'parameters': {'value': 'x' * 1000}})
            bpy.agent_bridge.poll()
            result = bpy.agent_bridge.request('job.status', {'job_id': accepted['job_id']})
            assert result['state'] == 'failed', result
        status = bpy.agent_bridge.status()
        assert status['journal_state'] == 'limit_reached' and not status['journal_healthy'], status
    finally:
        bpy.agent_bridge.stop()
    path = Path(status['journal'])
    assert path.stat().st_size <= status['journal_limit_bytes']
    recovered = journal.read(path)
    assert not recovered['incomplete_tail'] and not recovered['automatic_resume']
    print('AGENT_JOURNAL_LIMIT_OK', json.dumps({'bytes': path.stat().st_size,
          'limit': status['journal_limit_bytes'], 'retained_jobs': len(recovered['jobs'])}))
