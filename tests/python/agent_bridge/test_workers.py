# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Actual native deferred jobs, frozen inputs, subprocesses and scoped apply/checkpoints."""

import base64
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest

import test_protocol as harness
from bl_agent.client import BridgeError


class WorkerTests(harness.ProtocolTests):
    def prepare(self, *, apply_mode='preview', **parameters):
        descriptor = self.client.call('recipes.describe', {'id': 'fixture.worker'})
        params = self.submission(
            recipe={key: descriptor[key] for key in ('id', 'version', 'source_sha256')},
            execution_mode='worker_candidate', apply_mode=apply_mode, scope=descriptor['scope'],
            parameters=parameters)
        accepted = self.client.call('job.submit', params)
        return accepted, params

    def script(self, code, parameters=None):
        path = self.directory / 'worker-test-edit.py'
        path.write_text(code)
        result = self.run_job(self.submission('script.execute', path=path.name,
                                              source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                              parameters=parameters or {}))
        self.assertEqual(result['state'], 'succeeded', result)
        return result['outcome']['result']['value']

    def ready(self, **parameters):
        accepted, _ = self.prepare(**parameters)
        result = self.client.wait(accepted['job_id'], timeout=20)
        if result['state'] != 'ready':
            records = self.client.call('artifacts.list', {'job_id': result['job_id']})
            import base64
            for record in records:
                if record['metadata'].get('role') == 'worker_log':
                    data = self.client.call('artifacts.read', {'artifact_id': record['artifact_id']})
                    print(base64.b64decode(data['data']).decode()[-6000:], file=sys.stderr)
        self.assertEqual(result['state'], 'ready', result)
        self.assertFalse(result['outcome']['result']['value']['listener_started'])
        return result

    def apply(self, ready):
        self.client.call('job.apply', {'job_id': ready['job_id']})
        result = self.client.wait(ready['job_id'], timeout=20)
        self.assertEqual(result['state'], 'succeeded', result)
        return result

    def test_14_worker_preview_apply_preserves_ids_shared_mesh_and_parent(self):
        before = self.inspect(fingerprint=True)
        candidate = self.ready(width=1.4)
        after = self.inspect(fingerprint=True)
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(before['total'], after['total'])
        self.apply(candidate)
        block = next(obj for obj in self.inspect()['objects'] if obj['name'] == 'WorkerBlock')
        self.script("target = job.resolve_object(params['handle'])\n"
                    "child = bpy.data.objects.new('OutsideFollower', None)\n"
                    "job.scene.collection.objects.link(child)\nchild.parent = target\n"
                    "shared = bpy.data.objects.new('OutsideSharedMesh', target.data)\n"
                    "job.scene.collection.objects.link(shared)\nresult = True\n", {'handle': block['handle']})
        second = self.ready(width=2.1)
        applied = self.apply(second)
        current = self.inspect()
        block_after = next(obj for obj in current['objects'] if obj['name'] == 'WorkerBlock')
        self.assertEqual(block_after['handle'], block['handle'])
        checks = self.script("target = job.resolve_object(params['handle'])\n"
                             "other = bpy.data.objects['OutsideSharedMesh']\n"
                             "result = {'parent_kept': bpy.data.objects['OutsideFollower'].parent == target,\n"
                             "'shared_split': target.data != other.data,\n"
                             "'outside_width': max(v.co.x for v in other.data.vertices)}\n", {'handle': block['handle']})
        self.assertTrue(checks['parent_kept'])
        self.assertTrue(checks['shared_split'])
        self.assertAlmostEqual(checks['outside_width'], 1.4, places=5)
        checkpoint = applied['outcome']['result']['value']['checkpoint_id']
        restored = self.run_job(self.submission('checkpoint.restore', checkpoint_id=checkpoint,
                                                scope={'collection_name': 'WorkerFixture'}))
        self.assertEqual(restored['state'], 'succeeded', restored)
        restored_block = next(obj for obj in self.inspect()['objects'] if obj['name'] == 'WorkerBlock')
        self.assertEqual(restored_block['handle'], block['handle'])
        self.assertAlmostEqual(restored_block['dimensions'][0], 1.4, places=5)

    def test_15_worker_inspection_and_cancellation_stay_responsive(self):
        before = self.inspect(fingerprint=True)
        accepted, _ = self.prepare(width=1.9, delay_s=3)
        time.sleep(0.1)
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            self.inspect()
            timings.append(time.perf_counter() - started)
        self.assertLess(max(timings), 0.5)
        self.client.call('job.cancel', {'job_id': accepted['job_id']})
        result = self.client.wait(accepted['job_id'], timeout=10)
        self.assertEqual(result['state'], 'cancelled', result)
        self.assertEqual(before['fingerprint'], self.inspect(fingerprint=True)['fingerprint'])

    def test_16_stale_candidate_is_never_applied(self):
        candidate = self.ready(width=2.3)
        self.script("bpy.data.objects['WorkerBlock'].location.x += 0.2\nresult = True\n")
        before = self.inspect(fingerprint=True)
        self.client.call('job.apply', {'job_id': candidate['job_id']})
        result = self.client.wait(candidate['job_id'])
        self.assertEqual(result['state'], 'conflict', result)
        self.assertEqual(before['fingerprint'], self.inspect(fingerprint=True)['fingerprint'])

    def test_17_discard_does_not_change_source(self):
        before = self.inspect(fingerprint=True)
        candidate = self.ready(width=2.3)
        self.client.call('job.discard', {'job_id': candidate['job_id']})
        result = self.client.wait(candidate['job_id'])
        self.assertEqual(result['state'], 'discarded', result)
        self.assertEqual(before['fingerprint'], self.inspect(fingerprint=True)['fingerprint'])
        with self.assertRaises(BridgeError):
            self.client.call('job.apply', {'job_id': candidate['job_id']})

    def test_18_removed_external_target_is_rejected(self):
        candidate = self.ready(remove=True)
        before = self.inspect(fingerprint=True)
        self.client.call('job.apply', {'job_id': candidate['job_id']})
        result = self.client.wait(candidate['job_id'])
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['outcome']['error']['data']['kind'], 'EXTERNAL_REFERENCE')
        self.assertEqual(before['fingerprint'], self.inspect(fingerprint=True)['fingerprint'])

    def test_19_unsupported_stack_change_cleans_staged_ids(self):
        candidate = self.ready(modifier=True)
        before = self.inspect(fingerprint=True)
        self.client.call('job.apply', {'job_id': candidate['job_id']})
        result = self.client.wait(candidate['job_id'])
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['outcome']['error']['data']['kind'], 'UNSUPPORTED_SCOPE')
        after = self.inspect(fingerprint=True)
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(before['total'], after['total'])

    def test_20_authorized_auto_apply_uses_the_same_candidate_path(self):
        accepted, _ = self.prepare(width=1.75, apply_mode='auto_if_valid')
        result = self.client.wait(accepted['job_id'], timeout=20)
        self.assertEqual(result['state'], 'succeeded', result)
        block = next(obj for obj in self.inspect()['objects'] if obj['name'] == 'WorkerBlock')
        self.assertAlmostEqual(block['dimensions'][0], 1.75, places=5)

    def test_21_serialized_scene_guard_catches_render_settings(self):
        candidate = self.ready(width=1.8)
        self.script("job.scene.render.resolution_x += 1\nresult = True\n")
        self.client.call('job.apply', {'job_id': candidate['job_id']})
        result = self.client.wait(candidate['job_id'])
        self.assertEqual(result['state'], 'conflict', result)
        self.assertEqual(result['outcome']['error']['data']['kind'], 'REVISION_CONFLICT')

    def test_22_in_process_submission_and_scene_thread_guard(self):
        accepted = self.script("import threading\nerrors = []\n"
            "for function in (bpy.agent_bridge.poll, bpy.agent_bridge.stop):\n"
            "    try: function()\n"
            "    except RuntimeError: pass\n"
            "    else: raise AssertionError('Recursive dispatch/stop was accepted')\n"
            "query = {'context': {'scene': job.scene.name, 'view_layer': job.view_layer.name}}\n"
            "def off_thread():\n"
            "    try: bpy.agent_bridge.inspect(query)\n"
            "    except RuntimeError: errors.append('rejected')\n"
            "t = threading.Thread(target=off_thread)\nt.start()\nt.join(timeout=1)\n"
            "assert errors == ['rejected'] and not t.is_alive()\n"
            "current = bpy.agent_bridge.inspect({'context': {'scene': job.scene.name, 'view_layer': job.view_layer.name}, 'fingerprint': True})\n"
            "recipe = bpy.agent_bridge.describe_recipe('fixture.create')\n"
            "result = bpy.agent_bridge.request('job.submit', {\n"
            "'operation': 'recipe.execute', 'operation_key': 'in-process-fixture', 'execution_mode': 'live',\n"
            "'context': {'scene': job.scene.name, 'view_layer': job.view_layer.name},\n"
            "'session_id': current['session_id'], 'scene_epoch': current['scene_epoch'],\n"
            "'expected_revision': current['revision'], 'expected_fingerprint': current['fingerprint'],\n"
            "'recipe': {k: recipe[k] for k in ('id', 'version', 'source_sha256')},\n"
            "'scope': {'collection_name': 'BridgeFixture'}})\n")
        result = self.client.wait(accepted['job_id'], timeout=10)
        self.assertEqual(result['state'], 'succeeded', result)
        jobs = self.client.call('job.list', {'limit': 2})
        self.assertEqual(jobs[0]['job_id'], accepted['job_id'])
        self.assertNotIn('outcome', jobs[0])

    def test_23_cancel_ready_candidate_cannot_apply(self):
        candidate = self.ready(width=1.8)
        self.client.call('job.cancel', {'job_id': candidate['job_id']})
        with self.assertRaises(BridgeError):
            self.client.call('job.apply', {'job_id': candidate['job_id']})

    def test_24_existing_modifier_fields_survive_candidate_apply(self):
        self.script("obj = bpy.data.objects['WorkerBlock']\n"
                    "m = obj.modifiers.new('Supported bevel', 'BEVEL')\nm.width = 0.02\nresult = True\n")
        candidate = self.ready(width=1.8, bevel=True)
        self.apply(candidate)
        result = self.script("m = bpy.data.objects['WorkerBlock'].modifiers['Supported bevel']\n"
                             "result = {'width': m.width, 'segments': m.segments, 'limit': m.limit_method}\n")
        self.assertAlmostEqual(result['width'], 0.04, places=5)
        self.assertEqual(result['segments'], 2)
        self.assertEqual(result['limit'], 'ANGLE')

    def test_25_overdue_worker_is_stopped_without_a_live_edit(self):
        before = self.inspect(fingerprint=True)
        accepted, _ = self.prepare(width=1.8, delay_s=20)
        result = self.client.wait(accepted['job_id'], timeout=10)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['outcome']['error']['data']['kind'], 'WORKER_TIMEOUT')
        self.assertEqual(before['fingerprint'], self.inspect(fingerprint=True)['fingerprint'])

    def test_26_rejected_nested_result_finishes_without_applying(self):
        before = self.inspect(fingerprint=True)
        accepted, _ = self.prepare(nested_result=True)
        result = self.client.wait(accepted['job_id'], timeout=10)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['outcome']['error']['data']['kind'], 'INVALID_RESULT')
        self.assertEqual(before['fingerprint'], self.inspect(fingerprint=True)['fingerprint'])

    def test_27_release_preserves_checkpoints_until_explicitly_released(self):
        candidate = self.ready(width=1.7, bevel=True)
        applied = self.apply(candidate)
        target = candidate['job_id']
        rejected = self.run_job(self.submission('storage.prune', target_job_id=target, dry_run=False))
        self.assertEqual(rejected['state'], 'failed', rejected)
        self.assertEqual(rejected['outcome']['error']['data']['kind'], 'PINNED')
        released = self.run_job(self.submission('storage.prune', target_job_id=target, dry_run=False,
                                                release_checkpoints=True))
        self.assertEqual(released['state'], 'succeeded', released)
        status = self.client.call('job.status', {'job_id': target})
        self.assertTrue(status['resources_released'])
        self.assertNotIn('outcome', status)
        self.assertEqual(self.client.call('artifacts.list', {'job_id': target}), [])

    def test_28_release_refuses_ready_candidates_and_preserves_retry_key(self):
        accepted, params = self.prepare(width=1.8, bevel=True)
        candidate = self.client.wait(accepted['job_id'], timeout=20)
        self.assertEqual(candidate['state'], 'ready', candidate)
        result = self.run_job(self.submission('storage.prune', target_job_id=candidate['job_id'], dry_run=False))
        self.assertEqual(result['outcome']['error']['data']['kind'], 'PINNED')
        self.client.call('job.discard', {'job_id': candidate['job_id']})
        self.client.wait(candidate['job_id'])
        preview = self.run_job(self.submission('storage.prune', target_job_id=candidate['job_id']))
        self.assertTrue(preview['outcome']['result']['value']['dry_run'])
        released = self.run_job(self.submission('storage.prune', target_job_id=candidate['job_id'], dry_run=False))
        self.assertEqual(released['state'], 'succeeded', released)
        repeated = self.client.call('job.submit', params)
        self.assertEqual(repeated['job_id'], candidate['job_id'])
        self.assertEqual(repeated['state'], 'discarded')
        self.assertTrue(repeated['resources_released'])

    def test_29_worker_file_limit_is_enforced_without_source_edits(self):
        before = self.inspect(fingerprint=True)
        accepted, _ = self.prepare(write_bytes=2097152)
        result = self.client.wait(accepted['job_id'], timeout=15)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['outcome']['error']['data']['kind'], 'RESOURCE_LIMIT')
        self.assertEqual(before['fingerprint'], self.inspect(fingerprint=True)['fingerprint'])
        self.assertEqual(self.client.call('artifacts.list', {'job_id': accepted['job_id']}), [])

    def test_38_invalid_mesh_is_rejected_before_becoming_a_candidate(self):
        before = self.inspect(fingerprint=True)
        accepted, _ = self.prepare(invalid_mesh=True)
        result = self.client.wait(accepted['job_id'], timeout=15)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['outcome']['error']['data']['kind'], 'INVALID_CANDIDATE')
        self.assertEqual(before['fingerprint'], self.inspect(fingerprint=True)['fingerprint'])

    def test_39_large_ready_result_is_an_artifact_not_a_status_payload(self):
        recipe = self.client.call('recipes.describe', {'id': 'fixture.worker_artifacts'})
        params = self.submission(recipe={key: recipe[key] for key in ('id', 'version', 'source_sha256')},
                                 scope=recipe['scope'], execution_mode='worker_candidate')
        accepted = self.client.call('job.submit', params)
        result = self.client.wait(accepted['job_id'], timeout=30)
        self.assertEqual(result['state'], 'ready', result)
        self.assertLess(len(json.dumps(result)), 8192)
        inline = result['outcome']['result']
        self.assertTrue(inline['externalized'])
        chunk = self.client.call('artifacts.read', {'artifact_id': inline['artifact']['artifact_id']})
        payload = json.loads(base64.b64decode(chunk['data']))
        self.assertEqual(len(payload['result']['artifacts']), 21)
        page = self.client.call('artifacts.list', {'job_id': result['job_id'], 'offset': 0, 'limit': 5})
        self.assertEqual(len(page), 5)
        self.client.call('job.discard', {'job_id': result['job_id']})
        self.assertEqual(self.client.wait(result['job_id'])['state'], 'discarded')


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *harness.remaining], verbosity=2)
