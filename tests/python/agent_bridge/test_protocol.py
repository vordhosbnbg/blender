# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Run outside Blender: python3 tests/python/agent_bridge/test_protocol.py --blender <binary>."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/modules"))
from bl_agent.client import BridgeError, Client

parser = argparse.ArgumentParser()
parser.add_argument("--blender", type=Path, default=ROOT / "build-system/bin/blender")
args, remaining = parser.parse_known_args()


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="blender-agent-test-")
        cls.directory = Path(cls.temporary.name)
        cls.log = (cls.directory / "blender.log").open("w+")
        cls.process = subprocess.Popen([
            str(args.blender.resolve()), "--background", "--factory-startup", "--disable-agent-bridge", "--threads", "2",
            "--python-exit-code", "1", "--python", str(Path(__file__).with_name("server.py")),
            "--", str(cls.directory),
        ], cwd=ROOT, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        ready = cls.directory / "ready.json"
        while not ready.exists() and time.monotonic() < deadline:
            if cls.process.poll() is not None:
                break
            time.sleep(0.05)
        if not ready.exists():
            cls.log.seek(0)
            raise RuntimeError("Blender service did not start:\n" + cls.log.read())
        cls.descriptor = Path(json.loads(ready.read_text())["descriptor"])
        cls.credentials = json.loads(cls.descriptor.read_text())
        cls.client = Client(cls.descriptor, client_id="protocol-suite")
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.call("bridge.stop")
            cls.client.close()
            cls.process.wait(timeout=15)
            if cls.process.returncode != 0:
                cls.log.seek(0)
                raise AssertionError(cls.log.read())
            if cls.descriptor.exists():
                raise AssertionError("Session descriptor was not removed on shutdown")
        finally:
            if cls.process.poll() is None:
                cls.process.terminate()
                cls.process.wait(timeout=10)
            cls.log.close()
            cls.temporary.cleanup()

    def inspect(self, **kwargs):
        return self.client.call("scene.inspect", {
            "context": {"scene": self.client.hello["scene"], "view_layer": self.client.hello["view_layer"]},
            **kwargs,
        })

    def submission(self, operation="recipe.execute", **kwargs):
        current = self.inspect(fingerprint=True)
        type(self).counter += 1
        result = {"operation_key": f"operation-{self.counter}", "operation": operation,
                  "session_id": current["session_id"], "scene_epoch": current["scene_epoch"],
                  "expected_revision": current["revision"], "execution_mode": "live",
                  "expected_fingerprint": current["fingerprint"],
                  "context": {"scene": current["scene"], "view_layer": current["view_layer"]},
                  "scope": {"collection_name": "BridgeFixture"}}
        if operation == "recipe.execute":
            description = self.client.call("recipes.describe", {"id": "fixture.create"})
            result["recipe"] = {key: description[key] for key in ("id", "version", "source_sha256")}
        result.update(kwargs)
        return result

    def run_job(self, params):
        accepted = self.client.call("job.submit", params)
        return self.client.wait(accepted["job_id"], timeout=10)

    def test_01_private_endpoint_and_bounded_inspection(self):
        self.assertEqual(stat.S_IMODE(self.descriptor.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.descriptor.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(self.credentials["endpoint"]).stat().st_mode), 0o600)
        self.assertNotIn("token", self.client.hello)
        before = self.inspect(limit=1, fingerprint=True)
        after = self.inspect(limit=1, fingerprint=True)
        self.assertEqual(len(before["objects"]), 1)
        self.assertIsNotNone(before["next_offset"])
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(before["revision"], after["revision"])

    def test_02_authentication_and_fragmented_frames(self):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3)
            connection.connect(self.credentials["endpoint"])
            request = {"jsonrpc": "2.0", "id": 998, "method": "bridge.hello", "params": {
                "token": "wrong", "client_id": "bad", "protocol_major": 1}}
            payload = json.dumps(request).encode()
            framed = struct.pack("!I", len(payload)) + payload
            for part in (framed[:1], framed[1:3], framed[3:8], framed[8:]):
                connection.sendall(part)
            length, = struct.unpack("!I", self._receive(connection, 4))
            response = json.loads(self._receive(connection, length))
            self.assertEqual(response["id"], 998)
            self.assertEqual(response["error"]["data"]["kind"], "UNAUTHORIZED")

    @staticmethod
    def _receive(connection, length):
        data = bytearray()
        while len(data) < length:
            chunk = connection.recv(length - len(data))
            if not chunk:
                raise ConnectionError("Unexpected EOF")
            data.extend(chunk)
        return data

    def test_03_retry_reconnect_does_not_duplicate_geometry(self):
        params = self.submission(parameters={"offset": 2.5})
        accepted = self.client.call("job.submit", params)
        finished = self.client.wait(accepted["job_id"])
        self.assertEqual(finished["state"], "succeeded", finished)
        before = self.inspect()["total"]
        with Client(self.descriptor, client_id="protocol-suite") as reconnected:
            repeated = reconnected.call("job.submit", params)
            self.assertEqual(repeated["job_id"], accepted["job_id"])
            reordered = dict(reversed(params.items()))
            reordered['recipe'] = dict(reversed(params['recipe'].items()))
            self.assertEqual(reconnected.call('job.submit', reordered)['job_id'], accepted['job_id'])
        self.assertEqual(self.inspect()["total"], before)
        with self.assertRaises(BridgeError) as caught:
            self.client.call("job.submit", {**params, "parameters": {"offset": 3}})
        self.assertEqual(caught.exception.error["data"]["kind"], "IDEMPOTENCY_CONFLICT")

    def test_04_stale_revision_fails_before_mutation(self):
        params = self.submission()
        before = self.inspect()["total"]
        result = self.run_job({**params, "expected_revision": -10})
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["outcome"]["error"]["data"]["kind"], "REVISION_CONFLICT")
        self.assertEqual(before, self.inspect()["total"])

    def test_05_source_hash_is_pinned(self):
        params = self.submission()
        params["recipe"]["source_sha256"] = "0" * 64
        result = self.run_job(params)
        self.assertEqual(result["outcome"]["error"]["data"]["kind"], "RECIPE_CHANGED")

    def test_06_cancellation_stays_responsive_during_python(self):
        description = self.client.call("recipes.describe", {"id": "fixture.cooperative"})
        params = self.submission(recipe={key: description[key] for key in ("id", "version", "source_sha256")})
        accepted = self.client.call("job.submit", params)
        time.sleep(0.08)
        started = time.perf_counter()
        cancelled = self.client.call("job.cancel", {"job_id": accepted["job_id"]})
        self.assertTrue(cancelled["cancel_requested"])
        self.assertLess(time.perf_counter() - started, 0.5)
        result = self.client.wait(accepted["job_id"])
        self.assertEqual(result["state"], "cancelled", result)

    def test_07_handle_rename_and_change_cursor(self):
        before = self.inspect()
        obj = before["objects"][0]
        code = ("target = job.resolve_object(params['handle'])\n"
                "target.name = 'RenamedByBridge'\n"
                "target.location.x += 0.125\n"
                "result = {'renamed': target.name}\n")
        path = self.directory / "rename.py"
        path.write_text(code)
        params = self.submission("script.execute", path="rename.py",
                                 source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                 parameters={"handle": obj["handle"]})
        result = self.run_job(params)
        self.assertEqual(result["state"], "succeeded", result)
        after = self.inspect(handles=[obj["handle"]])
        self.assertEqual(after["objects"][0]["name"], "RenamedByBridge")
        self.assertEqual(after["objects"][0]["handle"], obj["handle"])
        changes = self.client.call("scene.changes", {
            "scene_epoch": before["scene_epoch"], "revision": before["revision"]})
        self.assertTrue(changes["changes"])

    def test_08_scripts_reject_traversal_and_untracked_execution(self):
        params = self.submission("script.execute", path="../outside.py", source_sha256="0" * 64)
        result = self.run_job(params)
        self.assertEqual(result["state"], "failed")
        with self.assertRaises(BridgeError):
            self.client.call("script.execute", params)

    def test_09_capture_artifacts_preserve_source_geometry(self):
        before = self.inspect(fingerprint=True)
        params = self.submission("capture.render", parameters={"views": ["oblique"], "resolution": 128})
        result = self.run_job(params)
        self.assertEqual(result["state"], "succeeded", result)
        record = result["outcome"]["result"]["artifacts"][0]
        matrix = record['metadata']['camera_matrix']
        self.assertGreater(sum(matrix[axis][3] ** 2 for axis in range(3)), 0.1)
        self.assertNotEqual(matrix[:3], [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]])
        chunk = self.client.call("artifacts.read", {"artifact_id": record["artifact_id"], "limit": 8})
        self.assertEqual(base64.b64decode(chunk["data"]), b'\x89PNG\r\n\x1a\n')
        self.assertEqual(chunk["next_offset"], 8)
        self.assertEqual(self.client.call("artifacts.list", {"job_id": result["job_id"]}), [record])
        after = self.inspect(fingerprint=True)
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(before["total"], after["total"])

    def test_10_parameter_types_fail_before_mutation(self):
        before = self.inspect()["total"]
        result = self.run_job(self.submission(parameters={"offset": "wrong"}))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["outcome"]["error"]["data"]["kind"], "INVALID_PARAMS")
        self.assertEqual(before, self.inspect()["total"])

    @staticmethod
    def receive_frame(connection):
        def exact(size):
            data = bytearray()
            while len(data) < size:
                chunk = connection.recv(size - len(data))
                if not chunk:
                    raise EOFError('Frame ended early')
                data.extend(chunk)
            return data
        size = struct.unpack('!I', exact(4))[0]
        return json.loads(exact(size))

    def test_30_combined_frames_and_malformed_inputs(self):
        def frame(payload):
            return struct.pack('!I', len(payload)) + payload
        hello = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'bridge.hello', 'params': {
            'token': self.credentials['token'], 'client_id': 'malformed-test', 'protocol_major': 1}}).encode()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3)
            connection.connect(self.credentials['endpoint'])
            payloads = (hello, b'{', b'{"bad":"\xff"}', b'{} trailing', b'[' * 70 + b']' * 70, hello)
            connection.sendall(b''.join(frame(payload) for payload in payloads))
            responses = [self.receive_frame(connection) for _ in payloads]
        self.assertIn('result', responses[0])
        self.assertIn('result', responses[-1])
        for response in responses[1:-1]:
            self.assertEqual(response['error']['data']['kind'], 'PARSE_ERROR')

    def test_31_invalid_lengths_disconnect_only_the_offending_client(self):
        for size in (0, 8 * 1024 * 1024 + 1):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(3)
                connection.connect(self.credentials['endpoint'])
                connection.sendall(struct.pack('!I', size))
                self.assertEqual(connection.recv(1), b'')
        self.assertTrue(self.client.call('bridge.status')['running'])

    def test_32_slow_partial_frame_does_not_block_other_clients(self):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(self.credentials['endpoint'])
            connection.sendall(struct.pack('!I', 1000) + b'{')
            started = time.perf_counter()
            self.inspect()
            self.assertLess(time.perf_counter() - started, .5)

    def test_33_compact_event_history_and_cursor_validation(self):
        cursor = self.client.call('bridge.status')['event_cursor']
        accepted = self.client.call('job.submit', self.submission(parameters={'offset': .15}))
        completed = self.client.wait(accepted['job_id'], timeout=10)
        self.assertEqual(completed['state'], 'succeeded', completed)
        events = self.client.call('events.poll', {'after': cursor, 'limit': 256})
        selected = [event for event in events['events'] if event.get('job', {}).get('job_id') == accepted['job_id']]
        self.assertEqual(selected[0]['type'], 'job.accepted')
        self.assertEqual(selected[-1]['job']['state'], 'succeeded')
        self.assertTrue(all('outcome' not in event['job'] for event in selected))
        self.assertEqual(sorted(event['cursor'] for event in selected), [event['cursor'] for event in selected])
        with self.assertRaises(BridgeError) as failure:
            self.client.call('events.poll', {'after': -1})
        self.assertEqual(failure.exception.error['data']['kind'], 'RESET_REQUIRED')

    def test_34_private_provenance_is_separate_from_compact_events(self):
        status = self.client.call('bridge.status')
        self.assertTrue(status['journal_healthy'])
        path = Path(status['journal'])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text()
        self.assertNotIn(self.credentials['token'], text)
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(records[0]['type'], 'session.started')
        accepted = next(record for record in records if 'accepted_parameters' in record)
        self.assertIn('expected_fingerprint', accepted['accepted_parameters'])
        details = self.client.call('history.job', {'job_id': accepted['job']['job_id']})
        self.assertEqual(details['accepted_parameters'], accepted['accepted_parameters'])
        self.assertFalse(details['automatic_resume'])
        events = self.client.call('events.poll', {'after': 0, 'limit': 256})
        self.assertTrue(all('accepted_parameters' not in event and 'outcome' not in event
                            for event in events['events']))
        history = self.client.call('history.sessions', {'limit': 2})
        self.assertTrue(any(record['session_id'] == status['session_id'] and record['active'] for record in history))

    def test_35_live_failure_reports_possible_partial_effects(self):
        before = self.inspect()['total']
        recipe = self.client.call('recipes.describe', {'id': 'fixture.partial_failure'})
        result = self.run_job(self.submission(recipe={key: recipe[key] for key in ('id', 'version', 'source_sha256')}))
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['outcome']['error']['data']['recovery']['effects'], 'may_have_changed')
        self.assertEqual(before + 1, self.inspect()['total'])

    def test_40_recipe_cache_revalidates_without_sending_the_schema(self):
        first = self.client.describe_recipe('fixture.create')
        before = self.client.received_bytes
        second = self.client.describe_recipe('fixture.create')
        cached_bytes = self.client.received_bytes - before
        self.assertEqual(first, second)
        self.assertLess(cached_bytes, len(json.dumps(first)))
        second['version'] = 'client-side-edit'
        self.assertEqual(self.client.describe_recipe('fixture.create')['version'], first['version'])

    def test_41_native_wait_returns_once_and_handles_deadlines(self):
        description = self.client.call('recipes.describe', {'id': 'fixture.cooperative'})
        accepted = self.client.call('job.submit', self.submission(
            recipe={key: description[key] for key in ('id', 'version', 'source_sha256')}))
        deadline = self.client.call('job.wait', {'job_id': accepted['job_id'], 'timeout_ms': 1})
        self.assertTrue(deadline['wait_timed_out'], deadline)
        before = self.client.sequence
        result = self.client.wait(accepted['job_id'])
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(self.client.sequence - before, 1)
        self.assertFalse(result['wait_timed_out'])
        again = self.client.call('job.wait', {'job_id': accepted['job_id']})
        self.assertEqual(result['outcome'], again['outcome'])

    def test_42_waiter_limit_and_disconnect_leave_job_control_available(self):
        descriptor = self.client.call('recipes.describe', {'id': 'fixture.cooperative'})
        accepted = self.client.call('job.submit', self.submission(
            recipe={key: descriptor[key] for key in ('id', 'version', 'source_sha256')}))
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3)
            connection.connect(self.credentials['endpoint'])
            def frame(request):
                payload = json.dumps(request).encode()
                return struct.pack('!I', len(payload)) + payload
            connection.sendall(frame({'jsonrpc': '2.0', 'id': 0, 'method': 'bridge.hello', 'params': {
                'token': self.credentials['token'], 'client_id': 'waiter-limit', 'protocol_major': 1}}))
            self.receive_frame(connection)
            connection.sendall(b''.join(frame({'jsonrpc': '2.0', 'id': index, 'method': 'job.wait',
                'params': {'job_id': accepted['job_id']}}) for index in range(1, 10)))
            rejected = self.receive_frame(connection)
            self.assertEqual(rejected['id'], 9)
            self.assertEqual(rejected['error']['data']['kind'], 'BUSY')
        self.client.call('job.cancel', {'job_id': accepted['job_id']})
        result = self.client.wait(accepted['job_id'])
        self.assertEqual(result['state'], 'cancelled', result)

    def test_43_request_ids_are_small_scalars(self):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3)
            connection.connect(self.credentials['endpoint'])
            for value in (['array'], {'nested': 'object'}, 'x' * 257):
                payload = json.dumps({'jsonrpc': '2.0', 'id': value, 'method': 'bridge.hello'}).encode()
                connection.sendall(struct.pack('!I', len(payload)) + payload)
                result = self.receive_frame(connection)
                self.assertIsNone(result['id'])
                self.assertEqual(result['error']['data']['kind'], 'INVALID_REQUEST')

    def test_36_capture_picks_keep_their_instance_pose_and_provenance(self):
        recipe = self.client.call('recipes.describe', {'id': 'fixture.instances'})
        created = self.run_job(self.submission(recipe={key: recipe[key] for key in ('id', 'version', 'source_sha256')}))
        self.assertEqual(created['state'], 'succeeded', created)
        value = created['outcome']['result']['value']
        handles = [item['handle'] for item in value['instances']]
        census = self.inspect(handles=handles, include_instances=True)
        self.assertEqual(census['instance_total'], 2)
        captured = self.run_job(self.submission('capture.render', parameters={
            'views': ['front'], 'resolution': 128, 'handles': handles, 'passes': ['object_id']}))
        self.assertEqual(captured['state'], 'succeeded', captured)
        records = captured['outcome']['result']['artifacts']
        raw = next(item for item in records if item['metadata'].get('role') == 'object_id' and
                   item['metadata'].get('representation') == 'raw')
        table_record = next(item for item in records if item['metadata'].get('role') == 'capture_table')
        chunk = self.client.call('artifacts.read', {'artifact_id': table_record['artifact_id']})
        table = json.loads(base64.b64decode(chunk['data']))
        self.assertEqual(len(table['objects']), 2)
        metadata = raw['metadata']
        matrix, scale = metadata['camera_matrix'], metadata['ortho_scale']
        chosen = table['objects'][0]
        point = [chosen['matrix_world'][axis][3] for axis in range(3)]
        relative = [point[axis] - matrix[axis][3] for axis in range(3)]
        horizontal = sum(matrix[axis][0] * relative[axis] for axis in range(3))
        vertical = sum(matrix[axis][1] * relative[axis] for axis in range(3))
        x, y = int((horizontal / scale + .5) * 128), int((.5 - vertical / scale) * 128)
        pick = self.client.call('capture.pick', {'artifact_id': raw['artifact_id'], 'x': x, 'y': y})
        self.assertEqual(pick['object']['instancer']['handle'], chosen['instancer']['handle'])
        self.assertEqual(pick['object']['source']['handle'], value['prototype']['handle'])
        self.assertTrue(pick['requires_live_revalidation'])
        recipe = self.client.call('recipes.describe', {'id': 'fixture.move_instance'})
        moved = self.run_job(self.submission(recipe={key: recipe[key] for key in ('id', 'version', 'source_sha256')},
            parameters={'handle': chosen['instancer']['handle'], 'offset': 9}))
        self.assertEqual(moved['state'], 'succeeded', moved)
        again = self.client.call('capture.pick', {'artifact_id': raw['artifact_id'], 'x': x, 'y': y})
        self.assertEqual(again, pick)
        self.assertGreater(self.inspect()['revision'], pick['provenance']['revision'])

    def test_37_utf8_summary_limits_preserve_valid_responses(self):
        params = self.submission()
        params['recipe']['id'] = '屋' * 100
        result = self.run_job(params)
        self.assertEqual(result['state'], 'failed')
        self.assertLessEqual(len(result['label'].encode('utf8')), 160)
        self.assertTrue(result['label'].startswith('屋'))
        cursor = self.client.call('bridge.status')['event_cursor']
        recipe = self.client.call('recipes.describe', {'id': 'fixture.unicode_progress'})
        result = self.run_job(self.submission(recipe={key: recipe[key] for key in ('id', 'version', 'source_sha256')}))
        self.assertEqual(result['state'], 'succeeded', result)
        events = self.client.call('events.poll', {'after': cursor})['events']
        phase = next(event['job']['phase'] for event in events if event['type'] == 'job.progress')
        self.assertTrue(phase.startswith('繰'))
        self.assertLessEqual(len(phase.encode('utf8')), 128)

    def test_11_cross_scene_native_context_and_window_restore(self):
        description = self.client.call("recipes.describe", {"id": "fixture.context_probe"})
        params = self.submission(recipe={key: description[key] for key in ("id", "version", "source_sha256")})
        params["context"] = {"scene": "BridgeAlternate", "view_layer": "ViewLayer"}
        alternative = self.client.call("scene.inspect", {"context": params["context"], "fingerprint": True})
        params["expected_revision"] = alternative["revision"]
        params["expected_fingerprint"] = alternative["fingerprint"]
        result = self.run_job(params)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["outcome"]["result"]["value"]["evaluated_scene"], "BridgeAlternate")
        self.assertEqual(self.client.call("bridge.status")["scene"], self.client.hello["scene"])

    def test_12_large_results_are_artifacts(self):
        descriptor = self.client.call('recipes.describe', {'id': 'fixture.large_result'})
        params = self.submission(recipe={key: descriptor[key] for key in ('id', 'version', 'source_sha256')})
        result = self.run_job(params)
        self.assertEqual(result['state'], 'succeeded', result)
        inline = result['outcome']['result']
        self.assertTrue(inline['externalized'])
        self.assertLess(len(json.dumps(result)), 2048)
        chunk = self.client.call('artifacts.read', {'artifact_id': inline['artifact']['artifact_id']})
        original = json.loads(base64.b64decode(chunk['data']))
        self.assertEqual(len(original['result']['value']['text']), 100000)

    def test_13_reconnect_can_cancel_a_running_main_thread_job(self):
        descriptor = self.client.call('recipes.describe', {'id': 'fixture.cooperative'})
        params = self.submission(recipe={key: descriptor[key] for key in ('id', 'version', 'source_sha256')})
        accepted = self.client.call('job.submit', params)
        time.sleep(0.05)
        started = time.perf_counter()
        with Client(self.descriptor, client_id='reconnecting-host') as reconnected:
            self.assertTrue(reconnected.hello['scene_state_cached'])
            status = reconnected.call('job.cancel', {'job_id': accepted['job_id']})
            self.assertTrue(status['cancel_requested'])
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(self.client.wait(accepted['job_id'])['state'], 'cancelled')


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
