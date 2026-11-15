# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Verify owned worker termination on graceful service stop and actual parent process death."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import test_protocol as harness
from bl_agent.client import Client
from bl_agent import journal


def terminal(pid):
    path = Path(f'/proc/{pid}/stat')
    try:
        return path.read_text().split(') ', 1)[1].split()[0] == 'Z'
    except FileNotFoundError:
        return True


class WorkerLifetimeTests(unittest.TestCase):
    def exercise(self, kill_parent):
        with tempfile.TemporaryDirectory(prefix='agent-lifetime-') as temporary:
            root = Path(temporary)
            with (root / 'server.log').open('w') as log:
                process = subprocess.Popen([
                    str(harness.args.blender.resolve()), '--background', '--factory-startup', '--disable-agent-bridge', '--threads', '2',
                    '--python-exit-code', '1', '--python', str(Path(__file__).with_name('server.py')),
                    '--', str(root)], cwd=harness.ROOT, stdout=log, stderr=subprocess.STDOUT)
                client = None
                worker_pid = None
                try:
                    deadline = time.monotonic() + 30
                    while not (root / 'ready.json').exists() and time.monotonic() < deadline:
                        self.assertIsNone(process.poll())
                        time.sleep(0.02)
                    ready = json.loads((root / 'ready.json').read_text())
                    client = Client(ready['descriptor'], client_id='worker-lifetime')
                    context = {'scene': client.hello['scene'], 'view_layer': client.hello['view_layer']}
                    current = client.call('scene.inspect', {'context': context, 'fingerprint': True})
                    recipe = client.call('recipes.describe', {'id': 'fixture.worker'})
                    client.call('job.submit', {
                        'operation': 'recipe.execute', 'operation_key': 'lifetime', 'execution_mode': 'worker_candidate',
                        'context': context, 'scope': recipe['scope'], 'parameters': {'delay_s': 20},
                        'session_id': current['session_id'], 'scene_epoch': current['scene_epoch'],
                        'expected_revision': current['revision'], 'expected_fingerprint': current['fingerprint'],
                        'recipe': {key: recipe[key] for key in ('id', 'version', 'source_sha256')},
                    })
                    deadline = time.monotonic() + 10
                    children = Path(f'/proc/{process.pid}/task/{process.pid}/children')
                    while worker_pid is None and time.monotonic() < deadline:
                        for value in children.read_text().split():
                            pid = int(value)
                            try:
                                command = Path(f'/proc/{pid}/cmdline').read_bytes()
                            except FileNotFoundError:
                                continue
                            if b'worker_main.py' in command:
                                worker_pid = pid
                        time.sleep(0.02)
                    self.assertIsNotNone(worker_pid)
                    # Wait until the worker reaches cooperative code, proving its parent watch was installed.
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        statuses = client.call('job.list', {'limit': 1})
                        if statuses[0]['phase'] == 'deliberate_test_delay':
                            break
                        time.sleep(0.02)
                    self.assertEqual(statuses[0]['phase'], 'deliberate_test_delay')
                    if kill_parent:
                        process.kill()
                    else:
                        client.call('bridge.stop')
                    process.wait(timeout=10)
                    deadline = time.monotonic() + 5
                    while not terminal(worker_pid) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(terminal(worker_pid), 'Owned worker outlived its parent/service')
                    if not kill_parent:
                        self.assertEqual(process.returncode, 0)
                    recovered = journal.read(ready['journal'])
                    self.assertFalse(recovered['automatic_resume'])
                    self.assertEqual(len(recovered['jobs']), 1)
                    self.assertEqual(recovered['jobs'][0]['state'], 'interrupted')
                    self.assertEqual(recovered['closed'], not kill_parent)
                finally:
                    if client:
                        client.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)

    def test_service_stop_joins_owned_worker(self):
        self.exercise(False)

    def test_parent_death_terminates_owned_worker(self):
        self.exercise(True)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *harness.remaining], verbosity=2)
