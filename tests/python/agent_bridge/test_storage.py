# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Filesystem/cache guarantees, using independent temporary stores and real files."""

import base64
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/modules'))
from bl_agent import artifacts
from bl_agent.storage import Store, StoreError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-store-')
        self.directory = Path(self.temporary.name)
        self.policy = {'file_bytes': 65536, 'job_bytes': 131072,
                       'session_bytes': 524288, 'workspace_bytes': 1048576}
        self.store = Store(self.directory / 'cache', self.policy)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def write(self, job, filename, data=b'abc'):
        path = self.store.allocate(job, filename)
        path.write_bytes(data)
        return path

    def test_unowned_root_and_job_directories_are_preserved(self):
        root = self.directory / 'user-data'
        root.mkdir()
        (root / 'notes').write_text('keep')
        with self.assertRaises(StoreError):
            Store(root, self.policy)
        job = self.store.root / 'not-owned'
        job.mkdir()
        (job / 'notes').write_text('keep')
        with self.assertRaises(StoreError):
            self.store.admit('not-owned')
        self.assertEqual((job / 'notes').read_text(), 'keep')

    def test_job_paths_reject_traversal_and_links(self):
        for name in ('../escape', '/tmp/escape', '.', 'a/b'):
            with self.assertRaises(StoreError):
                self.store.admit(name)
        path = self.write('one', 'file')
        path.unlink()
        path.symlink_to(self.directory / 'external')
        with self.assertRaises(StoreError):
            self.store.allocate('one', 'file')

    def test_per_file_limit_and_owned_abort(self):
        path = self.write('one', 'too-large', b'x' * 65537)
        with self.assertRaises(StoreError) as failure:
            self.store.check('one')
        self.assertEqual(failure.exception.kind, 'RESOURCE_LIMIT')
        removed = self.store.abort('one')
        self.assertEqual(removed['bytes'], 65537)
        self.assertFalse(path.exists())

    def test_aggregate_job_limit(self):
        for index in range(3):
            self.write('one', str(index), b'x' * 50000)
        with self.assertRaises(StoreError):
            self.store.check('one')
        self.store.abort('one')

    def test_session_admission_accounts_reservations(self):
        for index in range(3):
            self.store.admit('job-' + str(index))
        with self.assertRaises(StoreError):
            self.store.admit('job-4')
        self.store.seal('job-0')
        self.store.admit('job-4')

    def test_workspace_reservations_coordinate_independent_stores(self):
        policy = {**self.policy, 'session_bytes': 360000, 'workspace_bytes': 530000}
        a = Store(self.directory / 'a', policy)
        b = Store(self.directory / 'b', policy)
        try:
            a.admit('one')
            b.admit('one')
            with self.assertRaises(StoreError):
                b.admit('two')
            a.close()
            b.admit('two')
        finally:
            a.close()
            b.close()

    def test_cleanup_refuses_active_changed_or_unknown_files(self):
        path = self.write('one', 'file')
        with self.assertRaises(StoreError):
            self.store.release('one')
        self.store.seal('one')
        path.write_bytes(b'changed')
        with self.assertRaises(StoreError):
            self.store.release('one')
        self.assertEqual(path.read_bytes(), b'changed')
        other = self.write('two', 'file')
        self.store.seal('two')
        unknown = other.parent / 'user-note'
        unknown.write_text('keep')
        with self.assertRaises(StoreError):
            self.store.release('two')
        self.assertTrue(other.exists() and unknown.exists())
        path = self.write('three', 'file')
        self.store.seal('three')
        (path.parent / 'empty-user-folder').mkdir()
        with self.assertRaises(StoreError):
            self.store.release('three')
        self.assertTrue(path.exists())

    def test_metadata_index_cannot_escape_its_session_budget(self):
        artifacts.configure(self.directory / ('c' * 64), self.policy)
        try:
            path = artifacts.output_path('one', 'small.bin')
            path.write_bytes(b'x')
            with self.assertRaises(StoreError) as failure:
                for index in range(100):
                    artifacts.register('one', path, 'application/octet-stream', {'text': 'x' * 8000})
            self.assertEqual(failure.exception.kind, 'RESOURCE_LIMIT')
            self.assertLessEqual((artifacts._store.root / 'store.json').stat().st_size,
                                 artifacts._store.metadata_reserve() // 4)
        finally:
            artifacts.close()

    def test_cleanup_deletes_only_exact_owned_files(self):
        path = self.write('one', 'file')
        self.store.seal('one')
        outside = self.directory / 'user-note'
        outside.write_text('keep')
        self.assertEqual(self.store.release('one')['bytes'], 3)
        self.assertFalse(path.exists())
        self.assertEqual(outside.read_text(), 'keep')

    def test_persistent_index_has_private_permissions_and_reopens(self):
        self.write('one', 'file')
        self.store.seal('one')
        self.store.close()
        for name in ('store.json', 'lease.json'):
            self.assertEqual(stat.S_IMODE((self.store.root / name).stat().st_mode), 0o600)
        reopened = Store(self.store.root, self.policy)
        try:
            self.assertEqual(reopened.release_plan('one')['bytes'], 3)
        finally:
            reopened.close()

    def test_archived_release_does_not_claim_a_stopped_session(self):
        path = self.write('one', 'file')
        self.store.seal('one')
        with self.assertRaises(StoreError) as failure:
            Store.open_inactive(self.store.root, workspace=self.directory)
        self.assertEqual(failure.exception.kind, 'BUSY')
        self.store.close()
        lease = (self.store.root / 'lease.json').read_bytes()
        archive = Store.open_inactive(self.store.root, workspace=self.directory)
        self.assertEqual(archive.release_plan('one')['bytes'], 3)
        self.assertEqual((self.store.root / 'lease.json').read_bytes(), lease)
        archive.release('one')
        self.assertFalse(path.exists())
        self.assertEqual((self.store.root / 'lease.json').read_bytes(), lease)
        with self.assertRaises(StoreError):
            archive.admit('two')

    def test_archived_release_rechecks_a_newly_active_owner(self):
        path = self.write('one', 'file')
        self.store.seal('one')
        self.store.close()
        archive = Store.open_inactive(self.store.root, workspace=self.directory)
        active = Store(self.store.root, self.policy)
        try:
            with self.assertRaises(StoreError) as failure:
                archive.release('one')
            self.assertEqual(failure.exception.kind, 'BUSY')
            self.assertTrue(path.exists())
        finally:
            active.close()

    def test_streamed_artifact_reads_and_cross_session_reload(self):
        first = self.directory / ('a' * 64)
        second = self.directory / ('b' * 64)
        artifacts.configure(first, self.policy)
        path = artifacts.output_path('one', 'data.bin')
        data = bytes(range(256)) * 100
        path.write_bytes(data)
        record = artifacts.register('one', path, 'application/octet-stream')
        artifacts.seal('one')
        artifacts.close()
        artifacts.configure(second, self.policy)
        try:
            listed = artifacts.list_records(session_id='a' * 64)
            self.assertEqual(listed, [record])
            chunk = artifacts.read(record['artifact_id'], offset=100, limit=500)
            self.assertEqual(base64.b64decode(chunk['data']), data[100:600])
            path.write_bytes(b'modified')
            with self.assertRaises(StoreError):
                artifacts.read(record['artifact_id'])
        finally:
            artifacts.close()

    def test_artifacts_cannot_claim_another_jobs_files_or_be_resigned(self):
        artifacts.configure(self.directory / ('d' * 64), self.policy)
        try:
            path = artifacts.output_path('one', 'owned.bin')
            path.write_bytes(b'original')
            with self.assertRaises(StoreError):
                artifacts.register('two', path, 'application/octet-stream')
            artifacts.register('one', path, 'application/octet-stream')
            path.write_bytes(b'edited')
            with self.assertRaises(StoreError):
                artifacts.seal('one')
            self.assertEqual(path.read_bytes(), b'edited')
        finally:
            artifacts.close()

    def test_corrupt_inventory_cannot_release_another_job(self):
        path = self.write('one', 'owned')
        other = self.write('two', 'outside')
        self.store.seal('one')
        self.store.jobs['one']['inventory']['two/outside'] = self.store.jobs['one']['inventory']['one/owned']
        with self.assertRaises(StoreError):
            self.store.release('one')
        self.assertTrue(path.exists() and other.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
