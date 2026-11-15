# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Automatic application startup, explicit opt-out, flag order and normal GUI preferences."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import test_protocol as harness
from bl_agent.client import Client


class StartupTests(unittest.TestCase):
    def run_expression(self, code, flags=(), trailing=()):
        result = subprocess.run([str(harness.args.blender.resolve()), '--background', '--factory-startup',
            '--threads', '2', '--python-exit-code', '1', *flags, '--python-expr', code, *trailing],
            cwd=harness.ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('STARTUP_POLICY_OK', result.stdout)

    def test_default_background_service_is_active_before_python(self):
        self.run_expression("import bpy; from pathlib import Path; "
            "s=bpy.agent_bridge.status(); assert s['running']; assert Path(s['descriptor']).is_file(); "
            "print('STARTUP_POLICY_OK')")

    def test_opt_out_preserves_manual_start_and_stop(self):
        self.run_expression("import bpy; assert not bpy.agent_bridge.status()['running']; "
            "bpy.agent_bridge.start(); assert bpy.agent_bridge.status()['running']; "
            "bpy.agent_bridge.stop(); assert not bpy.agent_bridge.status()['running']; "
            "print('STARTUP_POLICY_OK')", flags=['--disable-agent-bridge'])

    def test_last_startup_flag_wins(self):
        for flags, active in [(['--disable-agent-bridge', '--agent-bridge'], True),
                              (['--agent-bridge', '--disable-agent-bridge'], False),
                              (['--agent-bridge', '--agent-bridge'], True)]:
            with self.subTest(flags=flags):
                self.run_expression(f"import bpy; assert bpy.agent_bridge.status()['running'] is {active!r}; "
                                    "print('STARTUP_POLICY_OK')", flags=flags)

    def test_startup_flags_are_processed_before_scripts_and_respect_separator(self):
        self.run_expression("import bpy; assert not bpy.agent_bridge.status()['running']; "
                            "print('STARTUP_POLICY_OK')", trailing=['--disable-agent-bridge'])
        self.run_expression("import bpy; assert bpy.agent_bridge.status()['running']; "
                            "print('STARTUP_POLICY_OK')", trailing=['--', '--disable-agent-bridge'])

    def test_loading_a_file_does_not_restart_a_stopped_service(self):
        with tempfile.TemporaryDirectory(prefix='agent-startup-file-') as temporary:
            path = str(Path(temporary) / 'saved.blend')
            self.run_expression("import bpy; from pathlib import Path; "
                "s=bpy.agent_bridge.status(); assert s['running']; "
                f"bpy.ops.wm.save_as_mainfile(filepath={path!r}); "
                "bpy.agent_bridge.stop(); assert not Path(s['descriptor']).exists(); "
                f"bpy.ops.wm.open_mainfile(filepath={path!r}, use_scripts=False); "
                "assert not bpy.agent_bridge.status()['running']; print('STARTUP_POLICY_OK')")

    @unittest.skipUnless(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'), 'GUI display unavailable')
    def test_normal_gui_startup_preserves_addons_and_dispatches(self):
        with tempfile.TemporaryDirectory(prefix='agent-default-gui-') as temporary:
            directory = Path(temporary)
            with (directory / 'gui.log').open('w+') as log:
                process = subprocess.Popen([str(harness.args.blender.resolve()), '--no-window-focus',
                    '--threads', '2', '--python-exit-code', '1', '--python',
                    str(Path(__file__).with_name('gui_startup.py')), '--', str(directory)],
                    cwd=harness.ROOT, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 45
                    while not (directory / 'ready.json').exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    log.flush()
                    log.seek(0)
                    self.assertTrue((directory / 'ready.json').is_file(), log.read())
                    ready = json.loads((directory / 'ready.json').read_text())
                    self.assertTrue(ready['running'] and ready['panel_registered'])
                    with Client(ready['descriptor'], client_id='default-startup-test') as client:
                        state = client.call('scene.inspect', {'context': {'scene': ready['scene'],
                                                                         'view_layer': ready['view_layer']}})
                        self.assertEqual(state['session_id'], ready['session_id'])
                    (directory / 'quit').touch()
                    process.wait(timeout=20)
                    self.assertEqual(process.returncode, 0)
                    self.assertFalse(Path(ready['descriptor']).exists())
                    print('DEFAULT_GUI_ADDONS', len(ready['enabled_addons']))
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *harness.remaining], verbosity=2)
