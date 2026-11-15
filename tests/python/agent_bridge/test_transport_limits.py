# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Slow readers and client/output limits using real sockets and a compiled service."""

import hashlib
import json
import socket
import struct
import sys
import time
import unittest

import test_protocol as harness


class TransportLimits(unittest.TestCase):
    # Share the fixture lifecycle, without inheriting/rerunning its protocol cases.
    setUpClass = classmethod(harness.ProtocolTests.setUpClass.__func__)
    inspect = harness.ProtocolTests.inspect
    submission = harness.ProtocolTests.submission
    run_job = harness.ProtocolTests.run_job
    _receive = staticmethod(harness.ProtocolTests._receive)

    @classmethod
    def tearDownClass(cls):
        if cls.process.poll() is None:
            return harness.ProtocolTests.tearDownClass.__func__(cls)
        try:
            cls.client.close()
            if cls.process.returncode != 0 or cls.descriptor.exists():
                cls.log.seek(0)
                raise AssertionError(cls.log.read())
        finally:
            cls.log.close()
            cls.temporary.cleanup()

    def connect(self, name):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        connection.connect(self.credentials['endpoint'])
        request = self.frame('bridge.hello', {'token': self.credentials['token'], 'client_id': name,
                                             'protocol_major': 1})
        try:
            connection.sendall(request)
            length, = struct.unpack('!I', self._receive(connection, 4))
            response = json.loads(self._receive(connection, length))
            self.assertNotIn('error', response)
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def frame(method, params, sequence=1):
        payload = json.dumps({'jsonrpc': '2.0', 'id': sequence, 'method': method,
                              'params': params}, separators=(',', ':')).encode()
        return struct.pack('!I', len(payload)) + payload

    def artifact(self):
        code = ("from bl_agent import artifacts\n"
                "path = artifacts.output_path(job.job_id, 'pressure.bin')\n"
                "path.write_bytes(b'x' * 1048576)\n"
                "result = artifacts.register(job.job_id, path, 'application/octet-stream')\n")
        path = self.directory / 'pressure.py'
        path.write_text(code)
        result = self.run_job(self.submission('script.execute', path=path.name,
                                              source_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        self.assertEqual(result['state'], 'succeeded', result)
        return result['outcome']['result']['value']

    def fill(self, connection, record, groups):
        for index in range(groups):
            connection.sendall(b''.join(self.frame('artifacts.read', {'artifact_id': record['artifact_id']},
                                                   sequence=index * 8 + item) for item in range(8)))
            time.sleep(.1)  # Allow main-thread reads to accumulate behind the unread socket.

    def test_01_client_limit_leaves_existing_clients_responsive(self):
        connections = []
        try:
            for index in range(7):
                connections.append(self.connect('limit-' + str(index)))
            with self.assertRaises((ConnectionError, OSError)):
                self.connect('over-limit')
            self.assertEqual(self.client.call('bridge.status')['running'], True)
        finally:
            for connection in connections:
                connection.close()
        time.sleep(.05)

    def test_02_output_pressure_disconnects_only_the_slow_reader(self):
        record = self.artifact()
        with self.connect('slow-pressure') as connection:
            try:
                self.fill(connection, record, 4)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.assertTrue(self.client.call('bridge.status')['running'])
            total = 0
            while True:
                try:
                    data = connection.recv(1024 * 1024)
                except ConnectionResetError:
                    break
                if not data:
                    break
                total += len(data)
                self.assertLessEqual(total, 17 * 1024 * 1024)
            self.assertLess(total, 17 * 1024 * 1024)
        self.assertTrue(self.client.call('bridge.status')['running'])

    def test_03_stop_has_a_deadline_with_unread_output(self):
        record = self.artifact()
        with self.connect('slow-stop') as connection:
            self.fill(connection, record, 1)
            start = time.monotonic()
            self.client.call('bridge.stop')
            self.process.wait(timeout=5)
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual(self.process.returncode, 0)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *harness.remaining], verbosity=2)
