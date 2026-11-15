# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Persistent standard-library client. Can be used outside Blender without importing bpy."""

import json
import socket
import struct
import time
from pathlib import Path

MAX_FRAME = 8 * 1024 * 1024


class BridgeError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(error.get("message", str(error)))


class Client:
    def __init__(self, descriptor, *, client_id="local-cli", timeout=30):
        self.descriptor = json.loads(Path(descriptor).read_text())
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect(self.descriptor["endpoint"])
        self.sequence = 0
        self.sent_bytes = 0
        self.received_bytes = 0
        self._recipe_descriptions = {}
        self.hello = self.call("bridge.hello", {
            "token": self.descriptor["token"], "client_id": client_id, "protocol_major": 1,
        })

    def _read_exact(self, length):
        result = bytearray()
        while len(result) < length:
            chunk = self.socket.recv(length - len(result))
            if not chunk:
                raise ConnectionError("Bridge connection closed")
            result.extend(chunk)
        return result

    def call(self, method, params=None):
        self.sequence += 1
        request = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}}
        payload = json.dumps(request, separators=(",", ":"), allow_nan=False).encode()
        if len(payload) > MAX_FRAME:
            raise ValueError("Request exceeds frame limit")
        self.socket.sendall(struct.pack("!I", len(payload)) + payload)
        self.sent_bytes += len(payload) + 4
        length, = struct.unpack("!I", self._read_exact(4))
        if length > MAX_FRAME:
            raise ValueError("Response exceeds frame limit")
        result = json.loads(self._read_exact(length))
        self.received_bytes += length + 4
        if result.get("id") != self.sequence:
            raise RuntimeError("Mismatched response id")
        if "error" in result:
            raise BridgeError(result["error"])
        return result["result"]

    def wait(self, job_id, *, timeout=120, interval=0.02):
        deadline = time.monotonic() + timeout
        def active(status):
            return status['state'] in {'queued', 'running', 'preparing', 'validating', 'capturing', 'applying', 'discarding'} or (
                status['state'] == 'ready' and status.get('auto_apply', False))
        while time.monotonic() < deadline:
            if 'job.wait' in self.hello.get('capabilities', []):
                socket_timeout = self.socket.gettimeout()
                interval_limit = min(25, socket_timeout * .8) if socket_timeout is not None else 25
                duration = max(1, int(min(interval_limit, deadline - time.monotonic()) * 1000))
                result = self.call('job.wait', {'job_id': job_id, 'timeout_ms': duration})
                if not active(result):
                    return result
                continue
            result = self.call("job.status", {"job_id": job_id, "include_result": False})
            if not active(result):
                complete = self.call("job.status", {"job_id": job_id})
                if not active(complete):
                    return complete
            time.sleep(interval)
        raise TimeoutError(f"Job {job_id} is still running; query it rather than resubmitting")

    def describe_recipe(self, recipe_id):
        """Revalidate a cached schema by digest without retransmitting its full definition."""
        previous = self._recipe_descriptions.get(recipe_id)
        parameters = {'id': recipe_id}
        if previous:
            parameters['if_schema_sha256'] = previous['schema_sha256']
        result = self.call('recipes.describe', parameters)
        if result.get('not_modified'):
            return json.loads(json.dumps(previous))
        self._recipe_descriptions[recipe_id] = result
        return json.loads(json.dumps(result))

    def close(self):
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True, help='Private session.json descriptor')
    parser.add_argument('--params', type=Path, help='JSON parameter file, or - for stdin')
    parser.add_argument('--client-id', default='local-cli')
    parser.add_argument('--wait', action='store_true', help='Wait for a submitted job')
    parser.add_argument('method')
    args = parser.parse_args()
    params = json.loads(sys.stdin.read() if str(args.params) == '-' else args.params.read_text()) if args.params else {}
    with Client(args.session, client_id=args.client_id) as client:
        result = client.call(args.method, params)
        if args.wait:
            result = client.wait(result['job_id'])
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
